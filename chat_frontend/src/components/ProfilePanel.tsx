/**
 * ProfilePanel — personal settings for every logged-in user.
 */
import { useState, useEffect } from "react";
import { XIcon, LockIcon, UnlockIcon, UserIcon, AlertTriangleIcon, ExternalLinkIcon, CheckCircleIcon, ServerIcon } from "lucide-react";
import { UserInstanceList } from "@/components/instance/UserInstanceList";

interface ProfilePanelProps {
  onClose: () => void;
  userId: number;
  onOpenChat: (chatId: number) => void;
  onTokenSaved?: () => void;
}

const lc = "block text-sm font-medium text-foreground mb-1";

function LockedInput({
  value,
  onChange,
  placeholder,
  locked,
  onToggleLock,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  locked: boolean;
  onToggleLock: () => void;
}) {
  return (
    <div className="relative">
      <input
        type={locked ? "password" : "text"}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        readOnly={locked}
        placeholder={locked ? "" : placeholder}
        autoComplete="new-password"
        className={`w-full pl-3 pr-9 py-2 rounded-lg text-sm bg-secondary border text-foreground focus:outline-none focus:ring-1 focus:ring-ring transition-colors font-mono ${
          locked ? "border-border/40 text-muted-foreground cursor-not-allowed" : "border-border"
        }`}
      />
      <button
        type="button"
        onClick={onToggleLock}
        className="absolute inset-y-0 right-0 flex items-center px-2.5 text-muted-foreground hover:text-foreground transition-colors"
        title={locked ? "Click to replace token" : "Lock field"}
      >
        {locked ? <LockIcon className="size-3.5" /> : <UnlockIcon className="size-3.5" />}
      </button>
    </div>
  );
}

export function ProfilePanel({ onClose, userId, onOpenChat, onTokenSaved }: ProfilePanelProps) {
  const [tab, setTab] = useState<"git" | "instances">("git");
  const [username, setUsername] = useState<string | null>(null);
  const [hasGitToken, setHasGitToken] = useState(false);
  const [gitToken, setGitToken] = useState("");
  const [tokenLocked, setTokenLocked] = useState(false);
  const [showGuide, setShowGuide] = useState(false);

  const [saveState, setSaveState] = useState<"idle" | "saving" | "ok" | "error">("idle");
  const [saveMsg, setSaveMsg] = useState("");
  const [testState, setTestState] = useState<"idle" | "testing" | "ok" | "error">("idle");
  const [testMsg, setTestMsg] = useState("");

  useEffect(() => {
    fetch("/api/me", { credentials: "include" })
      .then((r) => r.json())
      .then((d) => {
        setUsername(d.username ?? null);
        setHasGitToken(!!d.has_git_token);
        if (d.has_git_token) {
          setGitToken("••••••••••••••••••••");
          setTokenLocked(true);
        }
      })
      .catch(() => {});
  }, []);

  async function saveToken() {
    if (tokenLocked || !gitToken.trim()) return;
    setSaveState("saving"); setSaveMsg("");
    try {
      const res = await fetch("/api/me/git-token", {
        method: "PUT", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ git_token: gitToken.trim() }),
      });
      const d = await res.json();
      if (res.ok) {
        setSaveState("ok"); setSaveMsg("Token saved");
        setHasGitToken(d.has_git_token);
        if (d.has_git_token) { setGitToken("••••••••••••••••••••"); setTokenLocked(true); }
        onTokenSaved?.();
      } else {
        setSaveState("error"); setSaveMsg(d.detail || "Save failed");
      }
    } catch {
      setSaveState("error"); setSaveMsg("Request failed");
    }
    setTimeout(() => setSaveState("idle"), 5000);
  }

  async function testToken() {
    setTestState("testing"); setTestMsg("");
    try {
      const res = await fetch("/api/me/test-git-token", { method: "POST", credentials: "include" });
      const d = await res.json();
      setTestState(res.ok ? "ok" : "error");
      setTestMsg(d.message || d.detail || "");
    } catch {
      setTestState("error"); setTestMsg("Request failed");
    }
    setTimeout(() => setTestState("idle"), 6000);
  }

  const canSave = !tokenLocked && gitToken.trim().length > 0;

  return (
    <div className="flex-1 flex overflow-hidden">
      <div className="flex-1 flex flex-col overflow-hidden bg-background">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-border shrink-0">
          <div className="flex items-center gap-2.5">
            <UserIcon className="size-4 text-muted-foreground" />
            <h1 className="text-base font-semibold text-foreground">My Profile</h1>
          </div>
          <button onClick={onClose} className="p-1.5 rounded-lg text-muted-foreground hover:text-foreground hover:bg-accent transition-colors">
            <XIcon className="size-4" />
          </button>
        </div>

        {/* Tabs */}
        <div className="flex gap-1 px-6 pt-4 border-b border-border shrink-0">
          {([["git", UserIcon, "Git Token"], ["instances", ServerIcon, "My Instances"]] as const).map(([id, Icon, label]) => (
            <button
              key={id}
              onClick={() => setTab(id)}
              className={`flex items-center gap-1.5 px-3 py-2 text-sm font-medium border-b-2 transition-colors -mb-px ${
                tab === id
                  ? "border-primary text-foreground"
                  : "border-transparent text-muted-foreground hover:text-foreground"
              }`}
            >
              <Icon className="size-3.5" />{label}
            </button>
          ))}
        </div>

        <div className="flex-1 overflow-y-auto p-6">
          <div className="space-y-5 max-w-xl">

          {tab === "instances" && (
            <UserInstanceList
              userId={userId}
              onOpenChat={(chatId) => { onOpenChat(chatId); onClose(); }}
            />
          )}

          {tab === "git" && (<>

            {/* Account */}
            <div className="rounded-xl border border-border bg-card p-5 space-y-2">
              <h2 className="text-sm font-semibold text-foreground">Account</h2>
              <div className="flex items-center gap-3">
                <div className="size-9 rounded-full bg-accent flex items-center justify-center">
                  <UserIcon className="size-4 text-muted-foreground" />
                </div>
                <div>
                  <p className="text-sm font-medium text-foreground">{username || "—"}</p>
                  <p className="text-xs text-muted-foreground">Web user</p>
                </div>
              </div>
            </div>

            {/* Warning if no token */}
            {!hasGitToken && (
              <div className="flex items-start gap-3 rounded-xl border border-amber-500/30 bg-amber-500/10 p-4">
                <AlertTriangleIcon className="size-4 text-amber-400 shrink-0 mt-0.5" />
                <div>
                  <p className="text-sm font-medium text-amber-300">Git token not set</p>
                  <p className="mt-0.5 text-xs text-amber-400/80">
                    Git operations (push, pull, commit) inside your workspace will fail until you save a Personal Access Token below.
                  </p>
                </div>
              </div>
            )}
            {hasGitToken && (
              <div className="flex items-center gap-2 rounded-xl border border-green-500/20 bg-green-500/10 px-4 py-3">
                <CheckCircleIcon className="size-4 text-green-400 shrink-0" />
                <p className="text-sm text-green-300">Git token configured — workspace git operations are ready.</p>
              </div>
            )}

            {/* Personal git token */}
            <div className="rounded-xl border border-border bg-card p-5 space-y-4">
              <div className="flex items-start justify-between gap-4">
                <div>
                  <h2 className="text-sm font-semibold text-foreground">Personal GitHub Token</h2>
                  <p className="mt-1 text-xs text-muted-foreground">
                    A GitHub Classic PAT (Personal Access Token). Needs{" "}
                    <span className="font-mono bg-secondary px-1 rounded">repo</span> and{" "}
                    <span className="font-mono bg-secondary px-1 rounded">workflow</span> scopes.
                  </p>
                </div>
                <button
                  onClick={() => setShowGuide((v) => !v)}
                  className="shrink-0 text-xs text-primary hover:underline"
                >
                  {showGuide ? "Hide guide" : "How to get one?"}
                </button>
              </div>

              {/* Step-by-step guide */}
              {showGuide && (
                <div className="rounded-lg border border-border bg-secondary/50 p-4 space-y-3 text-xs text-muted-foreground">
                  <p className="font-semibold text-foreground text-sm">Getting a GitHub Classic PAT</p>
                  <ol className="space-y-2 list-decimal list-inside">
                    <li>Go to <span className="font-medium text-foreground">GitHub.com</span> → click your avatar (top-right) → <span className="font-medium text-foreground">Settings</span></li>
                    <li>Scroll to the bottom of the left sidebar → <span className="font-medium text-foreground">Developer settings</span></li>
                    <li>Open <span className="font-medium text-foreground">Personal access tokens</span> → <span className="font-medium text-foreground">Tokens (classic)</span></li>
                    <li>Click <span className="font-medium text-foreground">Generate new token</span> → <span className="font-medium text-foreground">Generate new token (classic)</span></li>
                    <li>
                      Give it a name (e.g. <span className="font-mono bg-background px-1 rounded">tagh-workspace</span>), set expiry, then tick:
                      <ul className="mt-1 ml-4 space-y-1 list-disc">
                        <li><span className="font-mono bg-background px-1 rounded">repo</span> — full repo access</li>
                        <li><span className="font-mono bg-background px-1 rounded">workflow</span> — GitHub Actions</li>
                      </ul>
                    </li>
                    <li>Click <span className="font-medium text-foreground">Generate token</span> and copy it — it starts with <span className="font-mono bg-background px-1 rounded">ghp_</span></li>
                  </ol>
                  <a
                    href="https://github.com/settings/tokens/new?scopes=repo,workflow&description=tagh-workspace"
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1.5 mt-1 px-3 py-1.5 rounded-lg bg-primary text-primary-foreground text-xs font-medium hover:bg-primary/90 transition-colors"
                  >
                    Open GitHub token page <ExternalLinkIcon className="size-3" />
                  </a>
                </div>
              )}

              <div>
                <label className={lc}>Token</label>
                <LockedInput
                  value={gitToken}
                  onChange={setGitToken}
                  placeholder="ghp_xxxxxxxxxxxxxxxxxxxx"
                  locked={tokenLocked}
                  onToggleLock={() => {
                    if (tokenLocked) {
                      setGitToken("");
                      setTokenLocked(false);
                    } else {
                      setTokenLocked(true);
                    }
                  }}
                />
                {hasGitToken && tokenLocked && (
                  <p className="mt-1 text-xs text-muted-foreground">
                    Click <LockIcon className="inline size-3" /> to replace with a new token.
                  </p>
                )}
              </div>

              <div className="flex items-center gap-3 pt-1 flex-wrap">
                <button
                  onClick={saveToken}
                  disabled={saveState === "saving" || !canSave}
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-primary hover:bg-primary/90 text-primary-foreground transition-colors disabled:opacity-40"
                >
                  {saveState === "saving" ? "Saving…" : "Save Token"}
                </button>
                <button
                  onClick={testToken}
                  disabled={testState === "testing" || !hasGitToken}
                  title={!hasGitToken ? "Save a token first" : ""}
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-secondary hover:bg-accent border border-border text-foreground transition-colors disabled:opacity-40"
                >
                  {testState === "testing" ? "Testing…" : "Test Connection"}
                </button>
              </div>

              {!hasGitToken && !canSave && (
                <p className="text-xs text-muted-foreground">Paste your token above then click Save.</p>
              )}
              {canSave && (
                <p className="text-xs text-amber-400">Click Save Token to store it, then Test Connection to verify.</p>
              )}
              {saveState === "ok" && <p className="text-xs text-green-400">✓ {saveMsg}</p>}
              {saveState === "error" && <p className="text-xs text-red-400">✗ {saveMsg}</p>}
              {testState === "ok" && <p className="text-xs text-green-400">✓ {testMsg}</p>}
              {testState === "error" && <p className="text-xs text-red-400">✗ {testMsg}</p>}
            </div>

          </>)}
          </div>
        </div>
      </div>
    </div>
  );
}
