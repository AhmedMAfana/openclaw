import { useEffect, useState } from "react";
import { ServerIcon } from "lucide-react";

const ic = "w-full px-3 py-2 rounded-lg text-sm bg-secondary border border-border text-foreground focus:outline-none focus:ring-1 focus:ring-ring";
const lc = "block text-sm font-medium text-foreground mb-1";

type HostSettings = {
  projects_base: string;
  mode_default: "docker" | "host";
  auto_clone: boolean;
};

export function SettingsHost() {
  const [cfg, setCfg] = useState<HostSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [saveState, setSaveState] = useState<"idle" | "saving" | "ok" | "error">("idle");
  const [saveMsg, setSaveMsg] = useState("");

  useEffect(() => {
    load();
  }, []);

  async function load() {
    setLoading(true);
    try {
      const res = await fetch("/api/settings/host", { credentials: "include" });
      if (res.ok) {
        const d = (await res.json()) as HostSettings;
        setCfg(d);
      }
    } finally {
      setLoading(false);
    }
  }

  async function save() {
    if (!cfg) return;
    setSaveState("saving");
    setSaveMsg("");
    try {
      const res = await fetch("/api/settings/host", {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(cfg),
      });
      const d = await res.json();
      if (res.ok && d.status === "ok") {
        setSaveState("ok");
        setSaveMsg("Saved");
      } else {
        setSaveState("error");
        setSaveMsg(d.detail || d.message || "Save failed");
      }
    } catch {
      setSaveState("error");
      setSaveMsg("Request failed");
    }
    setTimeout(() => setSaveState("idle"), 3000);
  }

  if (loading || !cfg) {
    return <div className="text-sm text-muted-foreground">Loading…</div>;
  }

  return (
    <div className="space-y-5 max-w-xl">
      <div className="rounded-xl border border-border bg-card p-5 space-y-4">
        <div className="flex items-center gap-2">
          <ServerIcon className="size-4 text-muted-foreground" />
          <h2 className="text-sm font-semibold text-foreground">Host Mode</h2>
        </div>
        <p className="text-xs text-muted-foreground">
          Host mode manages user apps that are already running on this VPS as plain
          directories. Projects live at{" "}
          <code className="text-foreground">&lt;base&gt;/&lt;repo-name&gt;</code>.
        </p>

        <div>
          <label className={lc}>Projects base directory</label>
          <input
            className={ic}
            value={cfg.projects_base}
            placeholder="/srv/projects"
            onChange={(e) => setCfg({ ...cfg, projects_base: e.target.value })}
          />
          <p className="mt-1 text-xs text-muted-foreground">
            Absolute path. In the local simulation this is <code>/sandbox/projects</code>.
          </p>
        </div>

        <div>
          <label className={lc}>Default mode for new projects</label>
          <select
            className={ic}
            value={cfg.mode_default}
            onChange={(e) =>
              setCfg({ ...cfg, mode_default: e.target.value as HostSettings["mode_default"] })
            }
          >
            <option value="docker">docker (containerized stack)</option>
            <option value="host">host (already-running on VPS)</option>
          </select>
        </div>

        <div className="flex items-center gap-2">
          <input
            id="host-auto-clone"
            type="checkbox"
            checked={cfg.auto_clone}
            onChange={(e) => setCfg({ ...cfg, auto_clone: e.target.checked })}
          />
          <label htmlFor="host-auto-clone" className="text-sm text-foreground">
            Agent auto-clones missing repos into the base directory
          </label>
        </div>

        <div className="flex items-center gap-3 pt-1">
          <button
            className="px-4 py-2 rounded-lg text-sm bg-primary text-primary-foreground disabled:opacity-50"
            disabled={saveState === "saving"}
            onClick={save}
          >
            {saveState === "saving" ? "Saving…" : "Save"}
          </button>
          {saveState === "ok" && <span className="text-xs text-green-400">{saveMsg}</span>}
          {saveState === "error" && <span className="text-xs text-red-400">{saveMsg}</span>}
        </div>
      </div>
    </div>
  );
}
