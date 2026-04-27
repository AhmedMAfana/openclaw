import { useState, useEffect } from "react";
import { CheckCircleIcon } from "lucide-react";

const ic = "w-full px-3 py-2 rounded-lg text-sm bg-secondary border border-border text-foreground focus:outline-none focus:ring-1 focus:ring-ring";
const lc = "block text-sm font-medium text-foreground mb-1";

function ConfiguredBadge() {
  return (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-green-500/15 text-green-400 border border-green-500/20">
      <CheckCircleIcon className="size-3" /> Configured
    </span>
  );
}

export function SettingsGit() {
  const [configured, setConfigured] = useState(false);
  const [newToken, setNewToken] = useState("");
  const [saveState, setSaveState] = useState<"idle"|"saving"|"ok"|"error">("idle");
  const [saveMsg, setSaveMsg] = useState("");
  const [testState, setTestState] = useState<"idle"|"testing"|"ok"|"error">("idle");
  const [testMsg, setTestMsg] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => { loadConfig(); }, []);

  async function loadConfig() {
    setLoading(true);
    try {
      const res = await fetch("/api/settings/config/git", { credentials: "include" });
      if (res.ok) {
        const d = await res.json();
        if (d.configured) setConfigured(!!d.token);
      }
    } finally { setLoading(false); }
  }

  async function save() {
    setSaveState("saving"); setSaveMsg("");
    const payload: Record<string, unknown> = { type: "github" };
    if (newToken.trim()) payload.token = newToken.trim();
    try {
      const res = await fetch("/api/settings/config/git", {
        method: "PUT", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const d = await res.json();
      if (res.ok && d.status === "ok") {
        setSaveState("ok"); setSaveMsg(d.message || "Saved");
        setNewToken("");
        loadConfig();
      } else { setSaveState("error"); setSaveMsg(d.detail || d.message || "Save failed"); }
    } catch { setSaveState("error"); setSaveMsg("Request failed"); }
    setTimeout(() => setSaveState("idle"), 3000);
  }

  async function testConn() {
    setTestState("testing"); setTestMsg("");
    try {
      const res = await fetch("/api/settings/test/git", {
        method: "POST", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ type: "github" }),
      });
      const d = await res.json();
      setTestState(d.status === "ok" ? "ok" : "error"); setTestMsg(d.message || "");
    } catch { setTestState("error"); setTestMsg("Request failed"); }
    setTimeout(() => setTestState("idle"), 5000);
  }

  if (loading) return <div className="text-sm text-muted-foreground">Loading…</div>;

  return (
    <div className="space-y-5 max-w-xl">
      <div className="rounded-xl border border-border bg-card p-5 space-y-4">
        <h2 className="text-sm font-semibold text-foreground">Git Provider</h2>
        <div>
          <label className={lc}>Provider</label>
          <select className={ic} defaultValue="github">
            <option value="github">GitHub</option>
            <option value="gitlab" disabled>GitLab (coming soon)</option>
          </select>
        </div>
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <label className={lc}>Personal Access Token</label>
            {configured && <ConfiguredBadge />}
          </div>
          <input type="password"
            placeholder={configured ? "Leave blank to keep existing token" : "ghp_..."}
            value={newToken} onChange={(e) => setNewToken(e.target.value)} className={ic} autoComplete="new-password" />
          {!configured && <p className="text-xs text-muted-foreground">Requires: repo, workflow, read:org scopes</p>}
        </div>
        <div className="flex items-center gap-3 pt-1">
          <button onClick={save} disabled={saveState === "saving"}
            className="px-4 py-2 rounded-lg text-sm font-medium bg-primary hover:bg-primary/90 text-primary-foreground transition-colors disabled:opacity-50">
            {saveState === "saving" ? "Saving…" : "Save Configuration"}
          </button>
          <button onClick={testConn} disabled={testState === "testing"}
            className="px-4 py-2 rounded-lg text-sm font-medium bg-secondary hover:bg-accent border border-border text-foreground transition-colors disabled:opacity-50">
            {testState === "testing" ? "Testing…" : "Test Connection"}
          </button>
        </div>
        {saveState === "ok" && <p className="text-xs text-green-400">{saveMsg}</p>}
        {saveState === "error" && <p className="text-xs text-red-400">{saveMsg}</p>}
        {testState === "ok" && <p className="text-xs text-green-400">{testMsg || "Connected"}</p>}
        {testState === "error" && <p className="text-xs text-red-400">{testMsg || "Failed"}</p>}
      </div>
    </div>
  );
}
