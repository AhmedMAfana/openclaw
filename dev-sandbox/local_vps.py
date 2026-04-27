"""local_vps.py — tiny FastAPI-based local "VPS" for developing host-mode
flows without a real Digital Ocean box.

It has two roles in one process:

1. A supervisor that starts/stops declared "already-running" apps on local
   ports (reading local_vps.yml).
2. A small FastAPI admin API on :8120 that lets the developer (and the
   TAGH Dev agent) list apps, check health, restart, and follow logs — the
   same operations the real VPS exposes implicitly via host_run_command.

Everything is REAL: real subprocess.Popen, real git pulls in the sample app
dirs, real npm/pip/composer, real HTTP health checks. Only SSH and hostname
metadata are faked, and we don't need those for the simulation.

Run:
    python dev-sandbox/local_vps.py up
    python dev-sandbox/local_vps.py status
    python dev-sandbox/local_vps.py logs sim-fastapi
    python dev-sandbox/local_vps.py down
    python dev-sandbox/local_vps.py admin     # just the admin API, no supervision
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None

try:
    import httpx  # type: ignore
except ImportError:
    httpx = None

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "local_vps.yml"
REGISTRY_FILE = HERE / "registry.json"
LOGS_DIR = HERE / "logs"
ADMIN_PORT = 8120

# ---------------------------------------------------------------------------
# Process registry
# ---------------------------------------------------------------------------


@dataclass
class ProcState:
    name: str
    cwd: str
    cmd: list[str]
    port: int
    health_url: str
    pid: int | None = None
    status: str = "stopped"  # stopped | starting | healthy | unhealthy | crashed
    started_at: float | None = None
    log_path: str = ""


def _read_config() -> list[dict[str, Any]]:
    if not CONFIG_FILE.exists():
        print(f"[local_vps] {CONFIG_FILE} missing — run `make sim-install` first",
              file=sys.stderr)
        sys.exit(1)
    if yaml is None:
        # Very small YAML-less fallback for the repo-provided default file
        text = CONFIG_FILE.read_text(encoding="utf-8")
        # Extremely small parser that only supports the shape our default file uses.
        # Prefer installing PyYAML for real use: `pip install pyyaml`.
        import re as _re
        procs: list[dict[str, Any]] = []
        cur: Optional[dict[str, Any]] = None
        for line in text.splitlines():
            if line.startswith("- name:"):
                if cur:
                    procs.append(cur)
                cur = {"name": line.split(":", 1)[1].strip()}
            elif cur is not None:
                m = _re.match(r"\s{4,}(\w+):\s*(.*)", line)
                if m:
                    k, v = m.group(1), m.group(2).strip()
                    if v.startswith("[") and v.endswith("]"):
                        cur[k] = [x.strip().strip('"') for x in v[1:-1].split(",") if x.strip()]
                    else:
                        cur[k] = v.strip('"')
        if cur:
            procs.append(cur)
        return procs
    data = yaml.safe_load(CONFIG_FILE.read_text())
    return list(data.get("processes", []))


def _load_registry() -> dict[str, dict]:
    if REGISTRY_FILE.exists():
        try:
            return json.loads(REGISTRY_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_registry(reg: dict[str, dict]) -> None:
    REGISTRY_FILE.write_text(json.dumps(reg, indent=2))


def _port_in_use(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Start / stop processes
# ---------------------------------------------------------------------------


def _ensure_logs_dir() -> None:
    LOGS_DIR.mkdir(exist_ok=True)


def _spawn(proc_cfg: dict) -> ProcState:
    _ensure_logs_dir()
    log_path = LOGS_DIR / f"{proc_cfg['name']}.log"
    raw_cwd = proc_cfg["cwd"]
    # Resolve relative paths against dev-sandbox/, not the caller's cwd
    cwd = raw_cwd if os.path.isabs(raw_cwd) else os.path.abspath(os.path.join(HERE, raw_cwd))
    cmd = proc_cfg["cmd"] if isinstance(proc_cfg["cmd"], list) else proc_cfg["cmd"].split()
    env = os.environ.copy()
    # Merge env overrides
    for k, v in (proc_cfg.get("env") or {}).items():
        env[k] = str(v)

    if _port_in_use(int(proc_cfg["port"])):
        return ProcState(
            name=proc_cfg["name"], cwd=cwd, cmd=cmd,
            port=int(proc_cfg["port"]), health_url=proc_cfg.get("health_url", ""),
            status="port_conflict", log_path=str(log_path),
        )

    f = open(log_path, "ab", buffering=0)
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env,
        stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    return ProcState(
        name=proc_cfg["name"], cwd=cwd, cmd=cmd,
        port=int(proc_cfg["port"]), health_url=proc_cfg.get("health_url", ""),
        pid=proc.pid, status="starting", started_at=time.time(),
        log_path=str(log_path),
    )


def _kill_by_registry(state: dict) -> None:
    pid = state.get("pid")
    if not pid:
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except ProcessLookupError:
        return
    # Give it 10s, then SIGKILL
    for _ in range(20):
        try:
            os.kill(pid, 0)
            time.sleep(0.5)
        except ProcessLookupError:
            return
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except ProcessLookupError:
        pass


# ---------------------------------------------------------------------------
# Health poll
# ---------------------------------------------------------------------------


async def _probe(url: str, timeout: int = 3) -> str:
    if not url or httpx is None:
        return "unknown"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url)
        if 200 <= r.status_code < 400:
            return "healthy"
        return "unhealthy"
    except Exception:
        return "unhealthy"


async def _health_loop() -> None:
    while True:
        reg = _load_registry()
        changed = False
        for name, state in reg.items():
            if not state.get("pid"):
                continue
            url = state.get("health_url", "")
            new = await _probe(url)
            if new != "unknown" and new != state.get("status"):
                state["status"] = new
                changed = True
        if changed:
            _save_registry(reg)
        await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# Admin HTTP API
# ---------------------------------------------------------------------------


def _build_app():
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import PlainTextResponse, RedirectResponse

    app = FastAPI(title="local-vps", version="0.1")

    @app.get("/apps")
    def list_apps():
        reg = _load_registry()
        return {"apps": list(reg.values())}

    @app.get("/apps/{name}")
    def get_app(name: str):
        reg = _load_registry()
        state = reg.get(name)
        if not state:
            raise HTTPException(404, "unknown app")
        return state

    @app.get("/apps/{name}/open")
    def open_app(name: str):
        reg = _load_registry()
        state = reg.get(name)
        if not state:
            raise HTTPException(404, "unknown app")
        return RedirectResponse(url=f"http://localhost:{state['port']}/")

    @app.post("/apps/{name}/restart")
    def restart_app(name: str):
        procs = {p["name"]: p for p in _read_config()}
        if name not in procs:
            raise HTTPException(404, "unknown app")
        reg = _load_registry()
        if name in reg:
            _kill_by_registry(reg[name])
        new_state = _spawn(procs[name])
        reg[name] = asdict(new_state)
        _save_registry(reg)
        return reg[name]

    @app.post("/apps/{name}/stop")
    def stop_app(name: str):
        reg = _load_registry()
        state = reg.get(name)
        if not state:
            raise HTTPException(404, "unknown app")
        _kill_by_registry(state)
        state["pid"] = None
        state["status"] = "stopped"
        reg[name] = state
        _save_registry(reg)
        return state

    @app.get("/apps/{name}/logs", response_class=PlainTextResponse)
    def tail_logs(name: str, tail: int = 200):
        reg = _load_registry()
        state = reg.get(name)
        if not state:
            raise HTTPException(404, "unknown app")
        log_path = state.get("log_path") or str(LOGS_DIR / f"{name}.log")
        if not os.path.isfile(log_path):
            return ""
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 64 * 1024))
            raw = f.read().decode("utf-8", errors="replace")
        lines = raw.splitlines()[-tail:]
        return "\n".join(lines)

    @app.get("/health")
    def health():
        return {"ok": True, "apps": len(_load_registry())}

    return app


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def cmd_up(args) -> int:
    procs = _read_config()
    reg = _load_registry()
    for cfg in procs:
        name = cfg["name"]
        if name in reg and reg[name].get("pid"):
            try:
                os.kill(reg[name]["pid"], 0)
                print(f"[local_vps] {name} already running (pid {reg[name]['pid']})")
                continue
            except ProcessLookupError:
                pass
        state = _spawn(cfg)
        reg[name] = asdict(state)
        print(f"[local_vps] started {name} pid={state.pid} port={state.port}")
    _save_registry(reg)
    return 0


def cmd_down(args) -> int:
    reg = _load_registry()
    for name, state in reg.items():
        if state.get("pid"):
            _kill_by_registry(state)
            print(f"[local_vps] stopped {name}")
        state["pid"] = None
        state["status"] = "stopped"
    _save_registry(reg)
    return 0


def cmd_status(args) -> int:
    reg = _load_registry()
    if not reg:
        print("(no apps registered — run `up`)")
        return 0
    print(f"{'NAME':20} {'PORT':6} {'PID':8} {'STATUS':12}")
    for name, s in reg.items():
        print(f"{name:20} {s.get('port',''):<6} {str(s.get('pid') or ''):8} {s.get('status',''):12}")
    return 0


def cmd_logs(args) -> int:
    reg = _load_registry()
    state = reg.get(args.name)
    if not state:
        print(f"unknown app {args.name}", file=sys.stderr)
        return 1
    log_path = state.get("log_path") or str(LOGS_DIR / f"{args.name}.log")
    if not os.path.isfile(log_path):
        print("(no log yet)")
        return 0
    subprocess.call(["tail", "-n", "200", "-F" if args.follow else "", log_path])
    return 0


def cmd_admin(args) -> int:
    """Run only the FastAPI admin API (plus health loop). Useful when
    processes were started by a previous `up` and we just want the API."""
    import uvicorn  # type: ignore
    app = _build_app()

    async def _startup():
        asyncio.create_task(_health_loop())

    app.add_event_handler("startup", _startup)
    uvicorn.run(app, host="0.0.0.0", port=ADMIN_PORT, log_level="warning")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="local-vps supervisor + admin API")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("up", help="start all processes declared in local_vps.yml").set_defaults(fn=cmd_up)
    sub.add_parser("down", help="stop all processes").set_defaults(fn=cmd_down)
    sub.add_parser("status", help="show registry status").set_defaults(fn=cmd_status)
    p_logs = sub.add_parser("logs", help="tail an app log")
    p_logs.add_argument("name")
    p_logs.add_argument("-f", "--follow", action="store_true")
    p_logs.set_defaults(fn=cmd_logs)
    sub.add_parser("admin", help="run only the admin FastAPI on :8120").set_defaults(fn=cmd_admin)

    args = parser.parse_args()
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
