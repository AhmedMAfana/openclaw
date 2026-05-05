/**
 * UserInstanceList — reusable list of a user's active instances.
 * Used in:
 *   - ProfilePanel "My Instances" tab
 *   - InstanceLimitModal (cap exceeded overlay)
 *
 * Fetches from GET /api/users/{userId}/instances.
 * Terminate calls POST /api/users/{userId}/instances/{id}/terminate.
 */
import { useState, useEffect, useCallback } from "react";
import { ExternalLinkIcon, XCircleIcon, RefreshCwIcon, MessageSquareIcon } from "lucide-react";

type InstanceStatus = "provisioning" | "running" | "idle" | "terminating" | "destroyed" | "failed";

interface UserInstance {
  id: string;
  slug: string;
  chat_session_id: number;
  project_id: number | null;
  status: InstanceStatus;
  web_hostname: string | null;
  started_at: string | null;
  last_activity_at: string;
  expires_at: string;
}

const STATUS_PILL: Record<InstanceStatus, string> = {
  provisioning: "bg-blue-500/15 text-blue-400 border border-blue-500/20",
  running:      "bg-green-500/15 text-green-400 border border-green-500/20",
  idle:         "bg-amber-500/15 text-amber-400 border border-amber-500/20",
  terminating:  "bg-zinc-500/15 text-zinc-400 border border-zinc-500/20",
  destroyed:    "bg-zinc-500/15 text-zinc-500 border border-zinc-500/20",
  failed:       "bg-red-500/15 text-red-400 border border-red-500/20",
};

function fmtRelative(iso: string | null | undefined): string {
  if (!iso) return "—";
  const ms = Date.now() - new Date(iso).getTime();
  if (Number.isNaN(ms)) return "—";
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return iso.slice(0, 10);
}

function StatusPill({ status }: { status: InstanceStatus }) {
  return (
    <span className={`inline-flex px-2 py-0.5 rounded-full text-[10px] font-medium uppercase tracking-wide ${STATUS_PILL[status]}`}>
      {status}
    </span>
  );
}

interface Props {
  userId: number;
  /** Called with the chat_session_id when user clicks Open Chat */
  onOpenChat: (chatId: number) => void;
  /** Called after a successful terminate so parent can react (e.g. close modal) */
  onTerminated?: () => void;
}

export function UserInstanceList({ userId, onOpenChat, onTerminated }: Props) {
  const [instances, setInstances] = useState<UserInstance[]>([]);
  const [loading, setLoading] = useState(true);
  const [terminating, setTerminating] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const res = await fetch(`/api/users/${userId}/instances`, { credentials: "include" });
      if (res.ok) {
        const d = await res.json();
        setInstances(d.instances ?? []);
      } else {
        setError("Failed to load instances");
      }
    } catch {
      setError("Network error");
    } finally {
      setLoading(false);
    }
  }, [userId]);

  useEffect(() => { load(); }, [load]);

  async function terminate(inst: UserInstance) {
    setTerminating((s) => new Set(s).add(inst.id));
    try {
      const res = await fetch(`/api/users/${userId}/instances/${inst.id}/terminate`, {
        method: "POST", credentials: "include",
      });
      if (res.ok) {
        setInstances((prev) => prev.filter((i) => i.id !== inst.id));
        onTerminated?.();
      } else {
        const d = await res.json().catch(() => ({}));
        setError(d.detail || "Terminate failed");
      }
    } catch {
      setError("Network error");
    } finally {
      setTerminating((s) => { const n = new Set(s); n.delete(inst.id); return n; });
    }
  }

  if (loading) return (
    <div className="flex items-center justify-center py-8 text-sm text-muted-foreground">
      Loading…
    </div>
  );

  if (error) return (
    <div className="flex items-center justify-between py-4 px-1">
      <p className="text-sm text-red-400">{error}</p>
      <button onClick={load} className="text-xs text-muted-foreground hover:text-foreground flex items-center gap-1">
        <RefreshCwIcon className="size-3" /> Retry
      </button>
    </div>
  );

  if (instances.length === 0) return (
    <div className="py-8 text-center text-sm text-muted-foreground">
      No active instances.
    </div>
  );

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between mb-1">
        <span className="text-xs text-muted-foreground">{instances.length} active instance{instances.length !== 1 ? "s" : ""}</span>
        <button onClick={load} className="text-xs text-muted-foreground hover:text-foreground flex items-center gap-1 transition-colors">
          <RefreshCwIcon className="size-3" /> Refresh
        </button>
      </div>

      {instances.map((inst) => (
        <div key={inst.id} className="rounded-xl border border-border bg-card p-4 space-y-3">
          <div className="flex items-start justify-between gap-3">
            <div className="flex items-center gap-2 flex-wrap">
              <StatusPill status={inst.status} />
              <span className="text-xs font-mono text-muted-foreground">{inst.slug}</span>
            </div>
            {inst.web_hostname && (
              <a
                href={`https://${inst.web_hostname}`}
                target="_blank"
                rel="noopener noreferrer"
                className="shrink-0 flex items-center gap-1 text-xs text-primary hover:underline"
              >
                Open app <ExternalLinkIcon className="size-3" />
              </a>
            )}
          </div>

          <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-muted-foreground">
            <span>Started: <span className="text-foreground">{fmtRelative(inst.started_at)}</span></span>
            <span>Active: <span className="text-foreground">{fmtRelative(inst.last_activity_at)}</span></span>
            <span>Expires: <span className="text-foreground">{fmtRelative(inst.expires_at)}</span></span>
          </div>

          <div className="flex items-center gap-2 pt-1">
            <button
              onClick={() => onOpenChat(inst.chat_session_id)}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium bg-primary hover:bg-primary/90 text-primary-foreground transition-colors"
            >
              <MessageSquareIcon className="size-3" /> Open Chat
            </button>
            <button
              onClick={() => terminate(inst)}
              disabled={terminating.has(inst.id) || inst.status === "terminating"}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium border border-red-500/30 text-red-400 hover:bg-red-500/10 transition-colors disabled:opacity-40"
            >
              <XCircleIcon className="size-3" />
              {terminating.has(inst.id) ? "Terminating…" : "Terminate"}
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
