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

export function SettingsChat() {
  const [provider, setProvider] = useState("telegram");

  // Track which fields are already configured in DB
  const [configured, setConfigured] = useState<Record<string, boolean>>({});

  // New-value inputs (always start empty — only filled if user wants to replace)
  const [newToken, setNewToken] = useState("");
  const [newBotToken, setNewBotToken] = useState("");
  const [newAppToken, setNewAppToken] = useState("");
  const [newSigningSecret, setNewSigningSecret] = useState("");
  const [defaultChannel, setDefaultChannel] = useState("");

  const [channels, setChannels] = useState<Array<{ id: string; name: string }>>([]);
  const [saveState, setSaveState] = useState<"idle" | "saving" | "ok" | "error">("idle");
  const [saveMsg, setSaveMsg] = useState("");
  const [testState, setTestState] = useState<"idle" | "testing" | "ok" | "error">("idle");
  const [testMsg, setTestMsg] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => { loadConfig(); }, []);

  useEffect(() => {
    if (provider === "slack") loadChannels();
  }, [provider]);

  async function loadConfig() {
    setLoading(true);
    try {
      const res = await fetch("/api/settings/config/chat", { credentials: "include" });
      if (res.ok) {
        const d = await res.json();
        if (d.configured) {
          setProvider(d.type || "telegram");
          // Mark fields as configured if they have masked values
          setConfigured({
            token: !!d.token,
            bot_token: !!d.bot_token,
            app_token: !!d.app_token,
            signing_secret: !!d.signing_secret,
          });
          if (d.default_channel) setDefaultChannel(d.default_channel);
        }
      }
    } finally {
      setLoading(false);
    }
  }

  async function loadChannels() {
    const res = await fetch("/api/settings/slack/channels", { credentials: "include" });
    if (res.ok) {
      const d = await res.json();
      if (d.ok) setChannels(d.channels || []);
    }
  }

  function buildPayload(): Record<string, unknown> {
    const p: Record<string, unknown> = { type: provider };
    if (provider === "telegram") {
      if (newToken.trim()) p.token = newToken.trim();
    } else {
      if (newBotToken.trim()) p.bot_token = newBotToken.trim();
      if (newAppToken.trim()) p.app_token = newAppToken.trim();
      if (newSigningSecret.trim()) p.signing_secret = newSigningSecret.trim();
      if (defaultChannel) p.default_channel = defaultChannel;
    }
    return p;
  }

  async function save() {
    setSaveState("saving"); setSaveMsg("");
    try {
      const res = await fetch("/api/settings/config/chat", {
        method: "PUT", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(buildPayload()),
      });
      const d = await res.json();
      if (res.ok && d.status === "ok") {
        setSaveState("ok"); setSaveMsg(d.message || "Saved");
        // Clear new-value inputs and reload to update configured badges
        setNewToken(""); setNewBotToken(""); setNewAppToken(""); setNewSigningSecret("");
        loadConfig();
      } else {
        setSaveState("error"); setSaveMsg(d.detail || d.message || "Save failed");
      }
    } catch { setSaveState("error"); setSaveMsg("Request failed"); }
    setTimeout(() => setSaveState("idle"), 3000);
  }

  async function testConn() {
    setTestState("testing"); setTestMsg("");
    try {
      const res = await fetch("/api/settings/test/chat", {
        method: "POST", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(buildPayload()),
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
        <h2 className="text-sm font-semibold text-foreground">Chat Platform</h2>

        <div>
          <label className={lc}>Provider</label>
          <select value={provider} onChange={(e) => { setProvider(e.target.value); setNewToken(""); setNewBotToken(""); setNewAppToken(""); setNewSigningSecret(""); }} className={ic}>
            <option value="telegram">Telegram</option>
            <option value="slack">Slack</option>
          </select>
        </div>

        {provider === "telegram" && (
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <label className={lc}>Bot Token</label>
              {configured.token && <ConfiguredBadge />}
            </div>
            <input type="password" placeholder={configured.token ? "Leave blank to keep existing token" : "From @BotFather"}
              value={newToken} onChange={(e) => setNewToken(e.target.value)} className={ic} autoComplete="new-password" />
            {!configured.token && <p className="text-xs text-muted-foreground">Get your bot token from @BotFather on Telegram</p>}
          </div>
        )}

        {provider === "slack" && (
          <div className="space-y-4">
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <label className={lc}>Bot Token <span className="text-red-400">*</span></label>
                {configured.bot_token && <ConfiguredBadge />}
              </div>
              <input type="password" placeholder={configured.bot_token ? "Leave blank to keep existing" : "xoxb-..."}
                value={newBotToken} onChange={(e) => setNewBotToken(e.target.value)} className={ic} autoComplete="new-password" />
            </div>
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <label className={lc}>App Token <span className="text-red-400">*</span></label>
                {configured.app_token && <ConfiguredBadge />}
              </div>
              <input type="password" placeholder={configured.app_token ? "Leave blank to keep existing" : "xapp-..."}
                value={newAppToken} onChange={(e) => setNewAppToken(e.target.value)} className={ic} autoComplete="new-password" />
            </div>
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <label className={lc}>Signing Secret <span className="text-red-400">*</span></label>
                {configured.signing_secret && <ConfiguredBadge />}
              </div>
              <input type="password" placeholder={configured.signing_secret ? "Leave blank to keep existing" : ""}
                value={newSigningSecret} onChange={(e) => setNewSigningSecret(e.target.value)} className={ic} autoComplete="new-password" />
            </div>
            <div>
              <label className={lc}>Default Channel</label>
              {channels.length > 0 ? (
                <select value={defaultChannel} onChange={(e) => setDefaultChannel(e.target.value)} className={ic}>
                  <option value="">None</option>
                  {channels.map((ch) => <option key={ch.id} value={ch.id}>#{ch.name}</option>)}
                </select>
              ) : (
                <input type="text" placeholder="Channel ID" value={defaultChannel}
                  onChange={(e) => setDefaultChannel(e.target.value)} className={ic} />
              )}
            </div>
          </div>
        )}

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
