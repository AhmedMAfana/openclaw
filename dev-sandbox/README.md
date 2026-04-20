# dev-sandbox — local VPS simulation for host-mode development

This directory lets you develop and QA OpenClow's host-mode flow locally, with
no Digital Ocean VPS needed. The idea: user apps live at the same filesystem
level as OpenClow (under `dev-sandbox/sample-apps/`), a small FastAPI
"local-VPS" supervisor keeps them running, and OpenClow (in Docker) reaches
them via `host.docker.internal:<port>`.

## Layout

```
dev-sandbox/
├── local_vps.py          # supervisor + admin API (all stdlib + FastAPI)
├── local_vps.yml         # processes to run
├── seed_sim_projects.py  # insert mode="host" rows into OpenClow's DB
├── logs/                 # per-app log files (git-ignored)
├── env/                  # per-app env files (git-ignored)
├── secrets.example.env   # tracked template — copy to env/ and fill in
└── sample-apps/
    └── sim-fastapi/      # real FastAPI starter; README is what the agent reads
```

## Reserved ports

| Port | Role |
|------|------|
| 8000  | OpenClow API (Docker)  |
| 8101  | sim-fastapi            |
| 8102  | sim-next (reserved)    |
| 8103  | sim-laravel (reserved) |
| 8120  | local-VPS admin API    |

## Setup

```sh
make sim-install       # install sim-fastapi deps + init git repo
docker compose up -d   # OpenClow itself
make sim-up            # start sample apps + supervisor
make sim-status        # verify
open http://localhost:8120/apps
make sim-seed          # inserts mode="host" rows into OpenClow's DB
```

Then open the web chat, and you should see the `sim-fastapi` project. Trigger
an `/addproject` or task against it — the agent will use `host_*` MCP tools
instead of Docker.

## How this maps to production

The only behavioral difference between this sandbox and a real VPS is the value
of `host.projects_base` (in the dashboard settings) and the tunnel hostname
(`host.docker.internal` locally vs `localhost` in production same-box deploys).
Every other path is identical: real `git pull`, real `pip install`, real HTTP
health checks.
