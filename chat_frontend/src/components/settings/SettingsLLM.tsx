import { useState, useEffect, useRef } from "react";
import { RefreshCwIcon, ExternalLinkIcon, CheckCircleIcon, AlertCircleIcon, CopyIcon, LoaderIcon } from "lucide-react";

const ic = "w-full px-3 py-2 rounded-lg text-sm bg-secondary border border-border text-foreground focus:outline-none focus:ring-1 focus:ring-ring";
const lc = "block text-sm font-medium text-foreground mb-1";

export function SettingsLLM() {
  const [coderTurns, setCoderTurns] = useState(50);
  const [reviewerTurns, setReviewerTurns] = useState(20);
  const [claudeAuth, setClaudeAuth] = useState<{ loggedIn: boolean; authMethod?: string; error?: string } | null>(null);
  const [oauthUrl, setOauthUrl] = useState<string | null>(null);
  const [authCode, setAuthCode] = useState("");
  const [codeState, setCodeState] = useState<"idle"|"submitting"|"verifying"|"error">("idle");
  const [codeMsg, setCodeMsg] = useState("");
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [saveState, setSaveState] = useState<"idle"|"saving"|"ok"|"error">("idle");
  const [saveMsg, setSaveMsg] = useState("");
  const [testState, setTestState] = useState<"idle"|"testing"|"ok"|"error">("idle");
  const [testMsg, setTestMsg] = useState("");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => { loadAll(); }, []);

  async function loadAll() {
    setLoading(true);
    try {
      const [cfgRes, authRes] = await Promise.all([
        fetch("/api/settings/config/llm", { credentials: "include" }),
        fetch("/api/settings/claude-auth-status", { credentials: "include" }),
      ]);
      if (cfgRes.ok) {
        const d = await cfgRes.json();
        if (d.coder_max_turns) setCoderTurns(d.coder_max_turns);
        if (d.reviewer_max_turns) setReviewerTurns(d.reviewer_max_turns);
      }
      if (authRes.ok) setClaudeAuth(await authRes.json());
    } finally { setLoading(false); }
  }

  async function save() {
    setSaveState("saving"); setSaveMsg("");
    try {
      const res = await fetch("/api/settings/config/llm", {
        method: "PUT", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ type: "claude", coder_max_turns: coderTurns, reviewer_max_turns: reviewerTurns }),
      });
      const d = await res.json();
      if (res.ok && d.status === "ok") { setSaveState("ok"); setSaveMsg(d.message || "Saved"); }
      else { setSaveState("error"); setSaveMsg(d.detail || d.message || "Save failed"); }
    } catch { setSaveState("error"); setSaveMsg("Request failed"); }
    setTimeout(() => setSaveState("idle"), 3000);
  }

  async function testConn() {
    setTestState("testing"); setTestMsg("");
    try {
      const res = await fetch("/api/settings/test/llm", {
        method: "POST", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ type: "claude" }),
      });
      const d = await res.json();
      setTestState(d.status === "ok" ? "ok" : "error");
      setTestMsg(d.message || "");
    } catch { setTestState("error"); setTestMsg("Request failed"); }
    setTimeout(() => setTestState("idle"), 5000);
  }

  async function triggerLogin() {
    if (pollRef.current) clearTimeout(pollRef.current);
    setOauthUrl(null);
    setAuthCode("");
    setCodeState("idle");
    setCodeMsg("");
    const res = await fetch("/api/settings/claude-auth-login", { method: "POST", credentials: "include" });
    const d = await res.json();
    if (d.url) setOauthUrl(d.url);
  }

  async function submitCode() {
    const code = authCode.trim();
    if (!code) return;
    setCodeState("submitting");
    try {
      const res = await fetch("/api/settings/claude-auth-submit-code", {
        method: "POST", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        setCodeState("error"); setCodeMsg(d.detail || "Failed to submit code"); return;
      }
      setCodeState("verifying"); setCodeMsg("Verifying…");
      // Poll until loggedIn or 30 s
      const start = Date.now();
      const poll = async () => {
        if (Date.now() - start > 30_000) { setCodeState("error"); setCodeMsg("Timed out — try again"); return; }
        const r = await fetch("/api/settings/claude-auth-status", { credentials: "include" });
        const s = await r.json();
        if (s.loggedIn) { setClaudeAuth(s); setOauthUrl(null); setCodeState("idle"); }
        else pollRef.current = setTimeout(poll, 2000);
      };
      pollRef.current = setTimeout(poll, 2000);
    } catch { setCodeState("error"); setCodeMsg("Request failed"); }
  }

  // Clean up polling on unmount
  useEffect(() => () => { if (pollRef.current) clearTimeout(pollRef.current); }, []);

  if (loading) return <div className="text-sm text-muted-foreground">Loading…</div>;

  return (
    <div className="space-y-5 max-w-xl">
      <div className="rounded-xl border border-border bg-card p-5 space-y-4">
        <h2 className="text-sm font-semibold text-foreground">Claude Settings</h2>
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className={lc}>Coder Max Turns</label>
            <input type="number" min={1} max={200} value={coderTurns}
              onChange={(e) => setCoderTurns(Number(e.target.value))} className={ic} />
            <p className="mt-1 text-xs text-muted-foreground">Agentic turns for coding tasks (1–200)</p>
          </div>
          <div>
            <label className={lc}>Reviewer Max Turns</label>
            <input type="number" min={1} max={100} value={reviewerTurns}
              onChange={(e) => setReviewerTurns(Number(e.target.value))} className={ic} />
            <p className="mt-1 text-xs text-muted-foreground">Turns for review tasks (1–100)</p>
          </div>
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

      <div className="rounded-xl border border-border bg-card p-5 space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-foreground">Claude Authentication</h2>
          <button
            onClick={async () => {
              setRefreshing(true);
              try {
                const r = await fetch("/api/settings/claude-auth-status", { credentials: "include" });
                if (r.ok) setClaudeAuth(await r.json());
              } finally { setRefreshing(false); }
            }}
            disabled={refreshing}
            className="p-1.5 rounded-lg text-muted-foreground hover:text-foreground hover:bg-accent transition-colors disabled:opacity-50">
            <RefreshCwIcon className={`size-3.5 ${refreshing ? "animate-spin" : ""}`} />
          </button>
        </div>
        {claudeAuth ? (
          claudeAuth.loggedIn ? (
            <div className="flex items-center gap-2">
              <CheckCircleIcon className="size-4 text-green-400 shrink-0" />
              <span className="text-sm text-foreground">Authenticated {claudeAuth.authMethod ? `via ${claudeAuth.authMethod}` : ""}</span>
            </div>
          ) : (
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <AlertCircleIcon className="size-4 text-yellow-400 shrink-0" />
                <span className="text-sm text-foreground">Not authenticated</span>
              </div>
              {claudeAuth.error && <p className="text-xs text-muted-foreground">{claudeAuth.error}</p>}
              <button onClick={triggerLogin}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-secondary hover:bg-accent border border-border text-foreground transition-colors">
                Authenticate with Claude
              </button>
            </div>
          )
        ) : <p className="text-sm text-muted-foreground">Loading…</p>}
        {oauthUrl && (
          <div className="rounded-lg bg-secondary border border-border p-4 space-y-4">
            {/* Step 1 */}
            <div className="space-y-1.5">
              <p className="text-xs font-medium text-foreground">Step 1 — Open this URL and sign in:</p>
              <div className="flex items-start gap-2">
                <a href={oauthUrl} target="_blank" rel="noopener noreferrer"
                  className="flex-1 text-xs text-primary hover:underline break-all flex items-start gap-1">
                  {oauthUrl}<ExternalLinkIcon className="size-3 shrink-0 mt-0.5 ml-0.5" />
                </a>
                <button onClick={() => navigator.clipboard.writeText(oauthUrl)}
                  className="p-1 rounded text-muted-foreground hover:text-foreground hover:bg-accent transition-colors shrink-0"
                  title="Copy URL">
                  <CopyIcon className="size-3.5" />
                </button>
              </div>
            </div>
            {/* Step 2 */}
            <div className="space-y-2 border-t border-border pt-3">
              <p className="text-xs font-medium text-foreground">
                Step 2 — After signing in, copy the code shown on the page and paste it here:
              </p>
              <input
                type="text"
                value={authCode}
                onChange={e => setAuthCode(e.target.value)}
                onKeyDown={e => e.key === "Enter" && submitCode()}
                placeholder="Paste authorization code…"
                disabled={codeState === "submitting" || codeState === "verifying"}
                className="w-full px-3 py-2 rounded-lg text-sm bg-background border border-border text-foreground focus:outline-none focus:ring-1 focus:ring-ring disabled:opacity-50 font-mono"
              />
              <div className="flex items-center gap-3">
                <button onClick={submitCode}
                  disabled={!authCode.trim() || codeState === "submitting" || codeState === "verifying"}
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-primary hover:bg-primary/90 text-primary-foreground transition-colors disabled:opacity-50">
                  {codeState === "submitting" ? "Submitting…" : codeState === "verifying" ? "Verifying…" : "Submit Code"}
                </button>
                {codeState === "verifying" && <LoaderIcon className="size-3.5 animate-spin text-muted-foreground" />}
                {codeState === "error" && <p className="text-xs text-red-400">{codeMsg}</p>}
                {codeState === "verifying" && <p className="text-xs text-muted-foreground">{codeMsg}</p>}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
