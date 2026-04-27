import { useState, useEffect } from "react";
import { EyeIcon, EyeOffIcon } from "lucide-react";

const ic = "w-full px-3 py-2 rounded-lg text-sm bg-secondary border border-border text-foreground focus:outline-none focus:ring-1 focus:ring-ring";
const lc = "block text-sm font-medium text-foreground mb-1";

export function SettingsSystem() {
  const [info, setInfo] = useState<{ database_url: string; redis_url: string; workspace_base_path: string; log_level: string; activity_log: boolean } | null>(null);
  const [loading, setLoading] = useState(true);
  const [devPw, setDevPw] = useState("");
  const [showPw, setShowPw] = useState(false);
  const [pwState, setPwState] = useState<"idle"|"saving"|"ok"|"error">("idle");
  const [pwMsg, setPwMsg] = useState("");

  useEffect(() => {
    fetch("/api/settings/system-info", { credentials: "include" })
      .then((r) => r.ok ? r.json() : null)
      .then((d) => { if (d) setInfo(d); })
      .finally(() => setLoading(false));
  }, []);

  async function setPassword(pw: string) {
    setPwState("saving"); setPwMsg("");
    try {
      const res = await fetch("/api/settings/dev-password", {
        method: "PUT", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: pw }),
      });
      const d = await res.json();
      if (res.ok && d.status === "ok") { setPwState("ok"); setPwMsg(d.message || (pw ? "Password set" : "Password cleared")); if (pw) setDevPw(""); }
      else { setPwState("error"); setPwMsg(d.detail || "Failed"); }
    } catch { setPwState("error"); setPwMsg("Request failed"); }
    setTimeout(() => setPwState("idle"), 3000);
  }

  if (loading) return <div className="text-sm text-muted-foreground">Loading…</div>;

  return (
    <div className="space-y-6 max-w-xl">
      <div className="rounded-xl border border-border bg-card p-5 space-y-4">
        <div>
          <h2 className="text-sm font-semibold text-foreground">Environment</h2>
          <p className="text-xs text-muted-foreground mt-0.5">Read-only — configure via environment variables</p>
        </div>
        {info ? (
          <div className="space-y-3">
            {[
              { label: "Database URL", value: info.database_url },
              { label: "Redis URL", value: info.redis_url },
              { label: "Workspace Path", value: info.workspace_base_path },
            ].map(({ label, value }) => (
              <div key={label}>
                <label className={lc}>{label}</label>
                <input readOnly value={value || "—"} className={`${ic} opacity-60 cursor-default`} />
              </div>
            ))}
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className={lc}>Log Level</label>
                <input readOnly value={info.log_level || "—"} className={`${ic} opacity-60 cursor-default`} />
              </div>
              <div>
                <label className={lc}>Activity Log</label>
                <input readOnly value={info.activity_log ? "Enabled" : "Disabled"} className={`${ic} opacity-60 cursor-default`} />
              </div>
            </div>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">Could not load system info</p>
        )}
      </div>

      <div className="rounded-xl border border-border bg-card p-5 space-y-4">
        <h2 className="text-sm font-semibold text-foreground">Dev Mode Password</h2>
        <p className="text-xs text-muted-foreground">When set, Slack users must send this password to unlock the bot in development mode.</p>
        <div>
          <label className={lc}>New Password</label>
          <div className="relative">
            <input type={showPw ? "text" : "password"} value={devPw}
              onChange={(e) => setDevPw(e.target.value)}
              placeholder="Enter password or leave blank to clear"
              className={`${ic} pr-10`} />
            <button type="button" onClick={() => setShowPw((v) => !v)}
              className="absolute right-2.5 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground">
              {showPw ? <EyeOffIcon className="size-4" /> : <EyeIcon className="size-4" />}
            </button>
          </div>
        </div>
        <div className="flex items-center gap-3">
          <button onClick={() => setPassword(devPw)} disabled={!devPw || pwState === "saving"}
            className="px-4 py-2 rounded-lg text-sm font-medium bg-primary hover:bg-primary/90 text-primary-foreground transition-colors disabled:opacity-50">
            {pwState === "saving" ? "Saving…" : "Set Password"}
          </button>
          <button onClick={() => setPassword("")} disabled={pwState === "saving"}
            className="px-4 py-2 rounded-lg text-sm font-medium bg-secondary hover:bg-accent border border-border text-foreground transition-colors disabled:opacity-50">
            Clear Password
          </button>
        </div>
        {pwState === "ok" && <p className="text-xs text-green-400">{pwMsg}</p>}
        {pwState === "error" && <p className="text-xs text-red-400">{pwMsg}</p>}
      </div>
    </div>
  );
}
