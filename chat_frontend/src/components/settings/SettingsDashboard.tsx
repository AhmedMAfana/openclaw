import { useState, useEffect, useCallback } from "react";
import { BrainIcon, MessageSquareIcon, GitBranchIcon, DatabaseIcon, ServerIcon, CheckCircleIcon, AlertCircleIcon, ArrowRightIcon, RefreshCwIcon } from "lucide-react";
import type { SettingsPage } from "@/components/SettingsPanel";

interface Props {
  onNavigate: (page: SettingsPage) => void;
}

interface SetupStatus {
  is_complete: boolean;
  configured: string[];
  missing: string[];
  project_count: number;
  user_count: number;
}

interface BotStatus {
  running: boolean | null;
  health: string;
  provider: string;
}

function StatusBadge({ ok }: { ok: boolean }) {
  return ok ? (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-green-500/15 text-green-400">
      <CheckCircleIcon className="size-3" /> Configured
    </span>
  ) : (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-yellow-500/15 text-yellow-400">
      <AlertCircleIcon className="size-3" /> Not set
    </span>
  );
}

function TestButton({ category, label }: { category: string; label: string }) {
  const [status, setStatus] = useState<"idle" | "testing" | "ok" | "error">("idle");
  const [msg, setMsg] = useState("");

  async function runTest() {
    setStatus("testing");
    setMsg("");
    try {
      const res = await fetch(`/api/settings/test/${category}`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      const data = await res.json();
      setStatus(data.status === "ok" ? "ok" : "error");
      setMsg(data.message || "");
    } catch {
      setStatus("error");
      setMsg("Request failed");
    }
    setTimeout(() => setStatus("idle"), 4000);
  }

  return (
    <div className="flex items-center gap-2">
      <button
        onClick={runTest}
        disabled={status === "testing"}
        className="px-3 py-1.5 rounded-lg text-xs font-medium bg-secondary hover:bg-accent border border-border text-foreground transition-colors disabled:opacity-50"
      >
        {status === "testing" ? "Testing…" : `Test ${label}`}
      </button>
      {status === "ok" && <span className="text-xs text-green-400">{msg || "Connected"}</span>}
      {status === "error" && <span className="text-xs text-red-400">{msg || "Failed"}</span>}
    </div>
  );
}

export function SettingsDashboard({ onNavigate }: Props) {
  const [setup, setSetup] = useState<SetupStatus | null>(null);
  const [bot, setBot] = useState<BotStatus | null>(null);
  const [loading, setLoading] = useState(true);

  const loadData = useCallback(async () => {
    try {
      const [setupRes, botRes] = await Promise.all([
        fetch("/api/settings/setup-status", { credentials: "include" }),
        fetch("/api/settings/bot-status", { credentials: "include" }),
      ]);
      if (setupRes.ok) setSetup(await setupRes.json());
      if (botRes.ok) setBot(await botRes.json());
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadData();
    const interval = setInterval(async () => {
      const res = await fetch("/api/settings/bot-status", { credentials: "include" });
      if (res.ok) setBot(await res.json());
    }, 30000);
    return () => clearInterval(interval);
  }, [loadData]);

  const isConfigured = (key: string) => setup?.configured.includes(key) ?? false;

  const providerCards = [
    { key: "llm", page: "llm" as SettingsPage, icon: <BrainIcon className="size-5" />, label: "LLM / AI", desc: "Claude, OpenAI" },
    { key: "chat", page: "chat" as SettingsPage, icon: <MessageSquareIcon className="size-5" />, label: "Chat Platform", desc: "Telegram, Slack" },
    { key: "git", page: "git" as SettingsPage, icon: <GitBranchIcon className="size-5" />, label: "Git Provider", desc: "GitHub, GitLab" },
  ];

  if (loading) {
    return <div className="flex items-center justify-center h-40 text-muted-foreground text-sm">Loading…</div>;
  }

  return (
    <div className="space-y-6 max-w-3xl">
      {bot && (
        <div className={`flex items-center gap-3 px-4 py-3 rounded-xl border text-sm ${
          bot.health === "healthy"
            ? "bg-green-500/10 border-green-500/20 text-green-400"
            : bot.health === "not_found"
            ? "bg-secondary border-border text-muted-foreground"
            : "bg-yellow-500/10 border-yellow-500/20 text-yellow-400"
        }`}>
          <span className={`size-2 rounded-full shrink-0 ${bot.health === "healthy" ? "bg-green-400" : bot.health === "not_found" ? "bg-zinc-500" : "bg-yellow-400"}`} />
          <span>
            {bot.health === "healthy" ? `Bot running · ${bot.provider}` : bot.health === "not_found" ? "Bot not started" : `Bot status: ${bot.health}`}
          </span>
          <button onClick={loadData} className="ml-auto p-1 rounded hover:bg-white/10 transition-colors">
            <RefreshCwIcon className="size-3.5" />
          </button>
        </div>
      )}

      {setup && !setup.is_complete && (
        <div className="px-4 py-3 rounded-xl border border-yellow-500/20 bg-yellow-500/10 text-sm text-yellow-400">
          Setup incomplete — missing: {setup.missing.join(", ")}
        </div>
      )}

      <div>
        <h2 className="text-sm font-medium text-muted-foreground mb-3">Providers</h2>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          {providerCards.map((card) => (
            <div key={card.key} className="rounded-xl border border-border bg-card p-4 flex flex-col gap-3">
              <div className="flex items-center gap-2.5">
                <div className="size-8 rounded-lg bg-secondary flex items-center justify-center text-muted-foreground shrink-0">
                  {card.icon}
                </div>
                <div>
                  <p className="text-sm font-medium text-foreground">{card.label}</p>
                  <p className="text-xs text-muted-foreground">{card.desc}</p>
                </div>
              </div>
              <StatusBadge ok={isConfigured(card.key)} />
              <button
                onClick={() => onNavigate(card.page)}
                className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors"
              >
                Configure <ArrowRightIcon className="size-3" />
              </button>
            </div>
          ))}
        </div>
      </div>

      <div>
        <h2 className="text-sm font-medium text-muted-foreground mb-3">Infrastructure</h2>
        <div className="rounded-xl border border-border bg-card divide-y divide-border">
          <div className="flex items-center justify-between px-4 py-3">
            <div className="flex items-center gap-2.5">
              <DatabaseIcon className="size-4 text-muted-foreground" />
              <span className="text-sm text-foreground">Database</span>
            </div>
            <TestButton category="database" label="DB" />
          </div>
          <div className="flex items-center justify-between px-4 py-3">
            <div className="flex items-center gap-2.5">
              <ServerIcon className="size-4 text-muted-foreground" />
              <span className="text-sm text-foreground">Redis</span>
            </div>
            <TestButton category="redis" label="Redis" />
          </div>
        </div>
      </div>

      {setup && (
        <div>
          <h2 className="text-sm font-medium text-muted-foreground mb-3">Management</h2>
          <div className="grid grid-cols-2 gap-3">
            <button onClick={() => onNavigate("projects")} className="rounded-xl border border-border bg-card px-4 py-4 text-left hover:bg-accent/30 transition-colors">
              <p className="text-2xl font-semibold text-foreground">{setup.project_count}</p>
              <p className="text-sm text-muted-foreground mt-0.5">Active Projects</p>
            </button>
            <button onClick={() => onNavigate("users")} className="rounded-xl border border-border bg-card px-4 py-4 text-left hover:bg-accent/30 transition-colors">
              <p className="text-2xl font-semibold text-foreground">{setup.user_count}</p>
              <p className="text-sm text-muted-foreground mt-0.5">Authorized Users</p>
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
