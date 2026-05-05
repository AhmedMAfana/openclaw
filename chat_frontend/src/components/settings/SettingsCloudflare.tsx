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

export function SettingsCloudflare() {
  const [configured, setConfigured] = useState(false);
  const [loading, setLoading] = useState(true);
  const [form, setForm] = useState({ account_id: "", zone_id: "", zone_domain: "", api_token: "" });
  const [saveState, setSaveState] = useState<"idle" | "saving" | "ok" | "error">("idle");
  const [saveMsg, setSaveMsg] = useState("");
  const [testState, setTestState] = useState<"idle" | "testing" | "ok" | "error">("idle");
  const [testMsg, setTestMsg] = useState("");

  useEffect(() => { load(); }, []);

  async function load() {
    setLoading(true);
    try {
      const res = await fetch("/api/settings/cloudflare", { credentials: "include" });
      if (res.ok) {
        const d = await res.json();
        if (d.configured) {
          setConfigured(true);
          setForm({ account_id: d.account_id, zone_id: d.zone_id, zone_domain: d.zone_domain, api_token: d.api_token });
        }
      }
    } finally { setLoading(false); }
  }

  async function save() {
    setSaveState("saving"); setSaveMsg("");
    try {
      const res = await fetch("/api/settings/cloudflare", {
        method: "PUT", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(form),
      });
      const d = await res.json();
      if (res.ok) { setSaveState("ok"); setSaveMsg(d.message || "Saved"); setConfigured(true); load(); }
      else { setSaveState("error"); setSaveMsg(d.detail || "Save failed"); }
    } catch { setSaveState("error"); setSaveMsg("Request failed"); }
    setTimeout(() => setSaveState("idle"), 4000);
  }

  async function test() {
    setTestState("testing"); setTestMsg("");
    try {
      const res = await fetch("/api/settings/test/cloudflare", { method: "POST", credentials: "include" });
      const d = await res.json();
      setTestState(res.ok ? "ok" : "error"); setTestMsg(d.message || d.detail || "");
    } catch { setTestState("error"); setTestMsg("Request failed"); }
    setTimeout(() => setTestState("idle"), 5000);
  }

  if (loading) return <div className="text-sm text-muted-foreground">Loading…</div>;

  return (
    <div className="space-y-5 max-w-xl">
      <div className="rounded-xl border border-border bg-card p-5 space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-foreground">Cloudflare Tunnel</h2>
          {configured && <ConfiguredBadge />}
        </div>
        <p className="text-xs text-muted-foreground">
          Required for per-chat container mode. Each chat gets its own Cloudflare named tunnel under your zone.
        </p>

        <div>
          <label className={lc}>Account ID</label>
          <input type="text" className={ic} placeholder="daeb4dec9d9185ce8c1f569205354650"
            value={form.account_id} onChange={(e) => setForm(f => ({ ...f, account_id: e.target.value }))} />
          <p className="mt-1 text-xs text-muted-foreground">Cloudflare dashboard → any zone → right sidebar</p>
        </div>

        <div>
          <label className={lc}>Zone ID</label>
          <input type="text" className={ic} placeholder="fbfef8c847b58212a729455cd91aaf8a"
            value={form.zone_id} onChange={(e) => setForm(f => ({ ...f, zone_id: e.target.value }))} />
        </div>

        <div>
          <label className={lc}>Zone Domain</label>
          <input type="text" className={ic} placeholder="apps.example.com"
            value={form.zone_domain} onChange={(e) => setForm(f => ({ ...f, zone_domain: e.target.value }))} />
          <p className="mt-1 text-xs text-muted-foreground">Subdomain where tunnels are reachable — e.g. chats.tagh.co.uk</p>
        </div>

        <div>
          <label className={lc}>API Token</label>
          <input type="password" className={ic} autoComplete="new-password"
            placeholder={configured ? "Leave blank to keep existing token" : "cfut_..."}
            value={form.api_token} onChange={(e) => setForm(f => ({ ...f, api_token: e.target.value }))} />
          <p className="mt-1 text-xs text-muted-foreground">
            Needs: Account.Cloudflare-Tunnel:Edit · Zone.DNS:Edit · Zone.Zone:Read
          </p>
        </div>

        <div className="flex items-center gap-3 pt-1">
          <button onClick={save} disabled={saveState === "saving"}
            className="px-4 py-2 rounded-lg text-sm font-medium bg-primary hover:bg-primary/90 text-primary-foreground transition-colors disabled:opacity-50">
            {saveState === "saving" ? "Saving…" : "Save & Validate"}
          </button>
          {configured && (
            <button onClick={test} disabled={testState === "testing"}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-secondary hover:bg-accent border border-border text-foreground transition-colors disabled:opacity-50">
              {testState === "testing" ? "Testing…" : "Test Connection"}
            </button>
          )}
        </div>
        {saveState === "ok" && <p className="text-xs text-green-400">{saveMsg}</p>}
        {saveState === "error" && <p className="text-xs text-red-400">{saveMsg}</p>}
        {testState === "ok" && <p className="text-xs text-green-400">{testMsg}</p>}
        {testState === "error" && <p className="text-xs text-red-400">{testMsg}</p>}
      </div>
    </div>
  );
}
