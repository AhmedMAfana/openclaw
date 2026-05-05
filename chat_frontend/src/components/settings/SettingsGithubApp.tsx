import { useState, useEffect } from "react";
import { CheckCircleIcon, LockIcon, UnlockIcon } from "lucide-react";

const lc = "block text-sm font-medium text-foreground mb-1";

function LockedInput({
  type = "text",
  value,
  onChange,
  placeholder,
  locked,
  onToggleLock,
  rows,
}: {
  type?: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  locked: boolean;
  onToggleLock: () => void;
  rows?: number;
}) {
  const sharedClass = `w-full pl-3 pr-9 py-2 rounded-lg text-sm bg-secondary border text-foreground focus:outline-none focus:ring-1 focus:ring-ring transition-colors ${
    locked
      ? "border-border/40 text-muted-foreground cursor-not-allowed select-none"
      : "border-border"
  }`;
  const lockBtn = (
    <button
      type="button"
      onClick={onToggleLock}
      className="absolute top-2 right-0 flex items-center px-2.5 text-muted-foreground hover:text-foreground transition-colors"
      title={locked ? "Click to edit" : "Lock field"}
    >
      {locked ? <LockIcon className="size-3.5" /> : <UnlockIcon className="size-3.5" />}
    </button>
  );

  if (rows) {
    return (
      <div className="relative">
        <textarea
          rows={rows}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          readOnly={locked}
          placeholder={locked ? "" : placeholder}
          className={`${sharedClass} font-mono text-xs resize-none`}
        />
        {lockBtn}
      </div>
    );
  }
  return (
    <div className="relative">
      <input
        type={locked ? "password" : type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        readOnly={locked}
        placeholder={locked ? "" : placeholder}
        autoComplete="new-password"
        className={sharedClass}
      />
      {lockBtn}
    </div>
  );
}

function ConfiguredBadge() {
  return (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-green-500/15 text-green-400 border border-green-500/20">
      <CheckCircleIcon className="size-3" /> Configured
    </span>
  );
}

export function SettingsGithubApp() {
  const [configured, setConfigured] = useState(false);
  const [loading, setLoading] = useState(true);
  const [form, setForm] = useState({ app_id: "", installation_id: "", private_key_pem: "" });
  const [locked, setLocked] = useState({ app_id: false, installation_id: false, private_key_pem: false });
  const [saveState, setSaveState] = useState<"idle" | "saving" | "ok" | "error">("idle");
  const [saveMsg, setSaveMsg] = useState("");
  const [testState, setTestState] = useState<"idle" | "testing" | "ok" | "error">("idle");
  const [testMsg, setTestMsg] = useState("");

  useEffect(() => { load(); }, []);

  async function load() {
    setLoading(true);
    try {
      const res = await fetch("/api/settings/github-app", { credentials: "include" });
      if (res.ok) {
        const d = await res.json();
        if (d.configured) {
          setConfigured(true);
          setForm({ app_id: d.app_id, installation_id: d.installation_id, private_key_pem: d.private_key_pem });
          setLocked({ app_id: true, installation_id: true, private_key_pem: true });
        }
      }
    } finally { setLoading(false); }
  }

  async function save() {
    setSaveState("saving"); setSaveMsg("");
    try {
      const res = await fetch("/api/settings/github-app", {
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
      const res = await fetch("/api/settings/test/github-app", { method: "POST", credentials: "include" });
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
          <h2 className="text-sm font-semibold text-foreground">GitHub App</h2>
          {configured && <ConfiguredBadge />}
        </div>
        <p className="text-xs text-muted-foreground">
          Used to mint per-instance short-lived git tokens. Each container gets a scoped token for its repo only.
        </p>

        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className={lc}>App ID</label>
            <LockedInput
              value={form.app_id}
              onChange={(v) => setForm(f => ({ ...f, app_id: v }))}
              placeholder="123456"
              locked={locked.app_id}
              onToggleLock={() => setLocked(l => ({ ...l, app_id: !l.app_id }))}
            />
            <p className="mt-1 text-xs text-muted-foreground">GitHub → Settings → Developer settings → GitHub Apps</p>
          </div>
          <div>
            <label className={lc}>Installation ID</label>
            <LockedInput
              value={form.installation_id}
              onChange={(v) => setForm(f => ({ ...f, installation_id: v }))}
              placeholder="45678901"
              locked={locked.installation_id}
              onToggleLock={() => setLocked(l => ({ ...l, installation_id: !l.installation_id }))}
            />
            <p className="mt-1 text-xs text-muted-foreground">App → Install → URL contains the ID</p>
          </div>
        </div>

        <div>
          <label className={lc}>Private Key (PEM)</label>
          <LockedInput
            value={form.private_key_pem}
            onChange={(v) => setForm(f => ({ ...f, private_key_pem: v }))}
            placeholder="-----BEGIN RSA PRIVATE KEY-----\n..."
            locked={locked.private_key_pem}
            onToggleLock={() => setLocked(l => ({ ...l, private_key_pem: !l.private_key_pem }))}
            rows={6}
          />
          <p className="mt-1 text-xs text-muted-foreground">
            Generate in GitHub App settings → "Generate a private key". Paste the full .pem contents.
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
