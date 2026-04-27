/**
 * SettingsInstances — admin surface for the per-chat instances introduced
 * by spec 001 + 003.
 *
 * Two views, gated by local state:
 *   1. List + filters + bulk Force Terminate + status counts header.
 *   2. Detail view (timeline, tunnel, recent logs, audit, action toolbar).
 *
 * All data flows through the JSON endpoints under /api/admin/instances.
 * Live updates ride the existing /api/activity/stream SSE endpoint.
 *
 * Principle IV: heartbeat_secret / db_password are never returned by the
 * backend; this component never asks for them.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import {
  RefreshCwIcon,
  ServerCogIcon,
  ExternalLinkIcon,
  AlertTriangleIcon,
  ArrowLeftIcon,
} from "lucide-react";

type InstanceStatus =
  | "provisioning"
  | "running"
  | "idle"
  | "terminating"
  | "destroyed"
  | "failed";

interface UserRef {
  id: number | null;
  name: string;
  deleted: boolean;
}
interface ProjectRef {
  id: number | null;
  name: string;
  deleted: boolean;
}
interface ChatRef {
  id: number | null;
  deleted: boolean;
  link: string | null;
}
interface InstanceListRow {
  slug: string;
  status: InstanceStatus;
  status_age_seconds: number;
  user: UserRef;
  project: ProjectRef;
  preview_url: string | null;
  created_at: string;
  last_activity_at: string | null;
  expires_at: string | null;
  upstream_health: "live" | "degraded" | "unreachable" | null;
}
interface StatusCounts {
  running: number;
  idle: number;
  provisioning: number;
  terminating: number;
  failed_24h: number;
  total_active: number;
  capacity: { used: number; cap: number | null };
}
interface ListResponse {
  items: InstanceListRow[];
  total: number;
  summary: StatusCounts;
}

const ALL_STATUSES: InstanceStatus[] = [
  "provisioning",
  "running",
  "idle",
  "terminating",
  "failed",
  "destroyed",
];
const DEFAULT_STATUSES: InstanceStatus[] = [
  "provisioning",
  "running",
  "idle",
  "terminating",
];
const BULK_CAP = 50;

const STATUS_PILL: Record<InstanceStatus, string> = {
  provisioning: "bg-blue-500/15 text-blue-400 border border-blue-500/20",
  running: "bg-green-500/15 text-green-400 border border-green-500/20",
  idle: "bg-amber-500/15 text-amber-400 border border-amber-500/20",
  terminating: "bg-zinc-500/15 text-zinc-400 border border-zinc-500/20",
  destroyed: "bg-zinc-500/15 text-zinc-500 border border-zinc-500/20",
  failed: "bg-red-500/15 text-red-400 border border-red-500/20",
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
    <span
      className={`inline-flex px-2 py-0.5 rounded-full text-xs font-medium uppercase tracking-wide ${STATUS_PILL[status]}`}
    >
      {status}
    </span>
  );
}

function CountBadge({ label, value, color }: { label: string; value: number; color: string }) {
  return (
    <div className="rounded-xl border border-border bg-card p-4">
      <div className="text-xs uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className={`text-2xl font-semibold mt-1 ${color}`}>{value}</div>
    </div>
  );
}

interface SettingsInstancesProps {
  initialSlug?: string | null;
}

export function SettingsInstances({ initialSlug = null }: SettingsInstancesProps) {
  const [activeSlug, setActiveSlug] = useState<string | null>(initialSlug);
  if (activeSlug) {
    return (
      <InstanceDetailView slug={activeSlug} onBack={() => setActiveSlug(null)} />
    );
  }
  return <InstanceListView onOpenDetail={setActiveSlug} />;
}

// ---------------------------------------------------------------------------
// List view
// ---------------------------------------------------------------------------

function InstanceListView({
  onOpenDetail,
}: {
  onOpenDetail: (slug: string) => void;
}) {
  const [items, setItems] = useState<InstanceListRow[]>([]);
  const [summary, setSummary] = useState<StatusCounts | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<Set<InstanceStatus>>(new Set(DEFAULT_STATUSES));
  const [userIdFilter, setUserIdFilter] = useState("");
  const [projectIdFilter, setProjectIdFilter] = useState("");
  const [q, setQ] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);
  const [confirm, setConfirm] = useState<{ kind: "single" | "bulk"; slugs: string[] } | null>(null);

  const params = useMemo(() => {
    const p = new URLSearchParams();
    statusFilter.forEach((s) => p.append("status", s));
    if (userIdFilter) p.append("user_id", userIdFilter);
    if (projectIdFilter) p.append("project_id", projectIdFilter);
    if (q) p.append("q", q);
    return p.toString();
  }, [statusFilter, userIdFilter, projectIdFilter, q]);

  async function load() {
    try {
      const res = await fetch(`/api/admin/instances?${params}`, { credentials: "include" });
      if (!res.ok) {
        if (res.status === 403) setError("Admin role required.");
        else setError(`HTTP ${res.status}`);
        return;
      }
      const data: ListResponse = await res.json();
      setItems(data.items);
      setSummary(data.summary);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // SSE — refresh on any instance event.
    const stream = new EventSource(
      "/api/activity/stream?type=instance_status,instance_action,instance_summary,instance_upstream",
    );
    stream.onmessage = () => {
      load();
    };
    stream.onerror = () => {
      // EventSource auto-reconnects with backoff. No-op.
    };
    return () => stream.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params]);

  const recentFailures = useMemo(
    () => items.filter((i) => i.status === "failed").slice(0, 5),
    [items],
  );

  function toggleSelect(slug: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(slug)) next.delete(slug);
      else if (next.size < BULK_CAP) next.add(slug);
      return next;
    });
  }

  function toggleStatus(s: InstanceStatus) {
    setStatusFilter((prev) => {
      const next = new Set(prev);
      if (next.has(s)) next.delete(s);
      else next.add(s);
      return next;
    });
  }

  function applyPreset(preset: "stuck" | "failed_today" | "idle_expiring") {
    if (preset === "stuck") setStatusFilter(new Set(["provisioning"]));
    else if (preset === "failed_today") setStatusFilter(new Set(["failed"]));
    else if (preset === "idle_expiring") setStatusFilter(new Set(["idle"]));
  }

  async function doSingleTerminate(slug: string) {
    const res = await fetch(`/api/admin/instances/${slug}/terminate`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: true }),
    });
    if (!res.ok) {
      const text = await res.text();
      alert(`Force terminate failed: ${text}`);
      return;
    }
    await load();
  }

  async function doBulkTerminate(slugs: string[]) {
    setBulkBusy(true);
    try {
      const res = await fetch(`/api/admin/instances/bulk-terminate`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ slugs, confirm: true }),
      });
      if (!res.ok) {
        const text = await res.text();
        alert(`Bulk terminate failed: ${text}`);
        return;
      }
      const data: { results: Array<{ slug: string; outcome: string; blocked?: boolean }> } =
        await res.json();
      const queued = data.results.filter((r) => r.outcome === "queued").length;
      const ended = data.results.filter((r) => r.blocked).length;
      const missing = data.results.filter((r) => r.outcome === "not_found").length;
      alert(`${queued} queued · ${ended} already ended · ${missing} not found`);
      setSelected(new Set());
      await load();
    } finally {
      setBulkBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      {/* Title + refresh */}
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm text-muted-foreground">
            Per-chat container instances — live state from spec 001 + 003.
          </p>
        </div>
        <button
          onClick={load}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm bg-secondary text-foreground hover:bg-accent border border-border"
        >
          <RefreshCwIcon className="size-4" /> Refresh
        </button>
      </div>

      {/* Counts */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <CountBadge label="Running"      value={summary?.running ?? 0}      color="text-green-400" />
        <CountBadge label="Idle"         value={summary?.idle ?? 0}         color="text-amber-400" />
        <CountBadge label="Provisioning" value={summary?.provisioning ?? 0} color="text-blue-400"  />
        <CountBadge label="Terminating"  value={summary?.terminating ?? 0}  color="text-zinc-400"  />
        <CountBadge label="Failed (24h)" value={summary?.failed_24h ?? 0}   color="text-red-400"   />
      </div>

      {/* Recent failures strip (US5) */}
      {recentFailures.length > 0 && (
        <div className="rounded-xl border border-red-500/30 bg-red-500/5 p-4">
          <div className="text-xs font-semibold uppercase tracking-wide text-red-400 mb-2">
            Recent failures
          </div>
          <ul className="space-y-1 text-sm">
            {recentFailures.map((f) => (
              <li key={f.slug}>
                <button
                  onClick={() => onOpenDetail(f.slug)}
                  className="font-mono text-xs text-red-300 hover:underline"
                >
                  {f.slug}
                </button>
                <span className="text-muted-foreground ml-2">
                  — {f.project.name} · {fmtRelative(f.created_at)}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Filters */}
      <div className="rounded-xl border border-border bg-card p-4 space-y-3">
        <div className="flex flex-wrap items-end gap-3">
          <div className="flex-1 min-w-[180px]">
            <label className="block text-xs text-muted-foreground mb-1">Search slug</label>
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="inst-…"
              className="w-full px-3 py-2 rounded-lg text-sm bg-secondary border border-border focus:outline-none focus:ring-1 focus:ring-ring"
            />
          </div>
          <div>
            <label className="block text-xs text-muted-foreground mb-1">User ID</label>
            <input
              value={userIdFilter}
              onChange={(e) => setUserIdFilter(e.target.value.replace(/\D/g, ""))}
              className="w-24 px-3 py-2 rounded-lg text-sm bg-secondary border border-border focus:outline-none focus:ring-1 focus:ring-ring"
            />
          </div>
          <div>
            <label className="block text-xs text-muted-foreground mb-1">Project ID</label>
            <input
              value={projectIdFilter}
              onChange={(e) => setProjectIdFilter(e.target.value.replace(/\D/g, ""))}
              className="w-24 px-3 py-2 rounded-lg text-sm bg-secondary border border-border focus:outline-none focus:ring-1 focus:ring-ring"
            />
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          {ALL_STATUSES.map((s) => (
            <button
              key={s}
              onClick={() => toggleStatus(s)}
              className={`px-2 py-0.5 rounded-full text-xs uppercase tracking-wide ${
                statusFilter.has(s)
                  ? STATUS_PILL[s]
                  : "border border-border text-muted-foreground hover:text-foreground"
              }`}
            >
              {s}
            </button>
          ))}
          <span className="mx-2 text-muted-foreground/40">|</span>
          <button onClick={() => applyPreset("stuck")} className="px-2 py-0.5 rounded-full text-xs bg-secondary text-foreground hover:bg-accent">
            Stuck (provisioning)
          </button>
          <button onClick={() => applyPreset("failed_today")} className="px-2 py-0.5 rounded-full text-xs bg-secondary text-foreground hover:bg-accent">
            Failed
          </button>
          <button onClick={() => applyPreset("idle_expiring")} className="px-2 py-0.5 rounded-full text-xs bg-secondary text-foreground hover:bg-accent">
            Idle
          </button>
        </div>
      </div>

      {/* Bulk action bar */}
      {selected.size > 0 && (
        <div className="rounded-xl border border-amber-500/30 bg-amber-500/5 p-3 flex items-center justify-between">
          <div className="text-sm">
            <span className="font-semibold">{selected.size}</span> selected
            <span className="text-muted-foreground"> (max {BULK_CAP})</span>
          </div>
          <div className="flex gap-2">
            <button
              onClick={() => setSelected(new Set())}
              className="px-3 py-1.5 rounded-lg text-sm text-muted-foreground hover:text-foreground"
            >
              Clear
            </button>
            <button
              onClick={() => setConfirm({ kind: "bulk", slugs: Array.from(selected) })}
              disabled={bulkBusy}
              className="px-3 py-1.5 rounded-lg text-sm bg-red-500/20 text-red-300 hover:bg-red-500/30 border border-red-500/30 disabled:opacity-50"
            >
              Force Terminate Selected
            </button>
          </div>
        </div>
      )}

      {/* Table */}
      {loading ? (
        <div className="text-sm text-muted-foreground py-4">Loading…</div>
      ) : error ? (
        <div className="rounded-xl border border-red-500/30 bg-red-500/5 p-6 text-sm text-red-400">{error}</div>
      ) : items.length === 0 ? (
        <div className="rounded-xl border border-border bg-card p-8 text-center text-sm text-muted-foreground">
          No instances match these filters.
        </div>
      ) : (
        <div className="rounded-xl border border-border bg-card overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border">
                <th className="px-3 py-3 w-8">
                  <input
                    type="checkbox"
                    checked={selected.size > 0 && selected.size === items.filter((i) => !["destroyed", "terminating"].includes(i.status)).length}
                    onChange={(e) => {
                      if (e.target.checked) {
                        const next = new Set<string>();
                        for (const it of items) {
                          if (next.size >= BULK_CAP) break;
                          if (!["destroyed", "terminating"].includes(it.status)) next.add(it.slug);
                        }
                        setSelected(next);
                      } else setSelected(new Set());
                    }}
                  />
                </th>
                {["Slug", "Status", "User", "Project", "Last activity", "Expires", ""].map((h) => (
                  <th key={h} className="px-3 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {items.map((i) => {
                const stuck =
                  (i.status === "provisioning" && i.status_age_seconds > 600) ||
                  (i.status === "terminating" && i.status_age_seconds > 300);
                const actionable = !["destroyed", "terminating"].includes(i.status);
                return (
                  <tr key={i.slug} className="hover:bg-accent/30 transition-colors">
                    <td className="px-3 py-3">
                      <input
                        type="checkbox"
                        checked={selected.has(i.slug)}
                        disabled={!actionable}
                        onChange={() => toggleSelect(i.slug)}
                      />
                    </td>
                    <td className="px-3 py-3 font-mono text-xs text-foreground">
                      <button onClick={() => onOpenDetail(i.slug)} className="hover:underline">
                        {i.slug}
                      </button>
                      {stuck && (
                        <span className="ml-2 px-1.5 py-0.5 rounded text-[10px] bg-amber-500/15 text-amber-400 border border-amber-500/20">
                          STUCK
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-3">
                      <StatusPill status={i.status} />
                    </td>
                    <td className="px-3 py-3 text-muted-foreground text-xs">
                      {i.user.deleted ? "(deleted)" : `${i.user.name}${i.user.id ? " #" + i.user.id : ""}`}
                    </td>
                    <td className="px-3 py-3 text-muted-foreground text-xs">
                      {i.project.deleted ? "(deleted)" : i.project.name}
                    </td>
                    <td className="px-3 py-3 text-muted-foreground text-xs" title={i.last_activity_at ?? undefined}>
                      {fmtRelative(i.last_activity_at)}
                    </td>
                    <td className="px-3 py-3 text-muted-foreground text-xs" title={i.expires_at ?? undefined}>
                      {fmtRelative(i.expires_at)}
                    </td>
                    <td className="px-3 py-3 text-right whitespace-nowrap">
                      {i.preview_url && (
                        <a
                          href={i.preview_url}
                          target="_blank"
                          rel="noreferrer"
                          className="inline-flex items-center gap-1 px-2 py-1 rounded text-xs text-blue-400 hover:underline mr-2"
                          title="Open preview"
                        >
                          <ExternalLinkIcon className="size-3" /> Preview
                        </a>
                      )}
                      <button
                        onClick={() => onOpenDetail(i.slug)}
                        className="px-2 py-1 rounded text-xs text-foreground hover:bg-accent mr-2"
                      >
                        Details
                      </button>
                      <button
                        onClick={() => setConfirm({ kind: "single", slugs: [i.slug] })}
                        disabled={!actionable}
                        className="px-2 py-1 rounded text-xs text-red-400 hover:bg-red-500/10 disabled:opacity-40 disabled:hover:bg-transparent"
                      >
                        Force Terminate
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* Confirm dialog (single + bulk) */}
      {confirm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" onClick={() => setConfirm(null)}>
          <div className="w-full max-w-md rounded-2xl border border-border bg-card p-6" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-start gap-3">
              <AlertTriangleIcon className="size-5 text-red-400 shrink-0 mt-0.5" />
              <div className="flex-1">
                <h3 className="font-semibold text-foreground">Force terminate {confirm.slugs.length === 1 ? "instance" : `${confirm.slugs.length} instances`}?</h3>
                <p className="text-sm text-muted-foreground mt-1">
                  This kills the compose stack and Cloudflare tunnel. The chat is left intact and can request a fresh instance later.
                </p>
                <ul className="mt-3 max-h-40 overflow-y-auto text-xs font-mono text-muted-foreground space-y-0.5">
                  {confirm.slugs.slice(0, 8).map((s) => <li key={s}>{s}</li>)}
                  {confirm.slugs.length > 8 && <li>… and {confirm.slugs.length - 8} more</li>}
                </ul>
              </div>
            </div>
            <div className="mt-5 flex justify-end gap-2">
              <button onClick={() => setConfirm(null)} className="px-3 py-1.5 rounded-lg text-sm text-muted-foreground hover:text-foreground">Cancel</button>
              <button
                onClick={async () => {
                  const slugs = confirm.slugs;
                  setConfirm(null);
                  if (confirm.kind === "single") await doSingleTerminate(slugs[0]);
                  else await doBulkTerminate(slugs);
                }}
                className="px-3 py-1.5 rounded-lg text-sm bg-red-500/20 text-red-300 hover:bg-red-500/30 border border-red-500/30"
              >
                Force Terminate
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Detail view
// ---------------------------------------------------------------------------

interface InstanceDetail {
  slug: string;
  status: InstanceStatus;
  status_age_seconds: number;
  user: UserRef;
  project: ProjectRef;
  chat: ChatRef;
  preview_url: string | null;
  created_at: string;
  started_at: string | null;
  last_activity_at: string | null;
  expires_at: string | null;
  grace_notification_at: string | null;
  terminated_at: string | null;
  terminated_reason: string | null;
  compose_project: string | null;
  workspace_path: string | null;
  session_branch: string | null;
  image_digest: string | null;
  resource_profile: string | null;
  transitions: Array<{ at: string; status: InstanceStatus; note: string | null }>;
  tunnel: { url: string | null; health: "live" | "degraded" | "unreachable" | null; degradation_history: unknown[] };
  failure: { code: string; message: string | null } | null;
  available_actions: Array<
    "force_terminate" | "reprovision" | "rotate_git_token" | "extend_expiry" | "open_preview" | "open_in_chat"
  >;
}

interface LogLine {
  ts: string;
  level: string;
  message: string;
  context: Record<string, unknown>;
}

interface AuditEntry {
  actor: string;
  action: string;
  command: string;
  exit_code: number | null;
  output_summary: string | null;
  risk_level: "normal" | "elevated" | "dangerous";
  blocked: boolean;
  metadata: Record<string, unknown> | null;
  created_at: string;
}

function InstanceDetailView({ slug, onBack }: { slug: string; onBack: () => void }) {
  const [detail, setDetail] = useState<InstanceDetail | null>(null);
  const [logs, setLogs] = useState<LogLine[]>([]);
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [logLevel, setLogLevel] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [actionBusy, setActionBusy] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<null | { action: string; label: string; body?: Record<string, unknown> }>(null);
  const newSlugRef = useRef<string | null>(null);

  async function loadAll() {
    try {
      const [d, l, a] = await Promise.all([
        fetch(`/api/admin/instances/${slug}`, { credentials: "include" }),
        fetch(`/api/admin/instances/${slug}/logs?limit=50${logLevel ? `&level=${logLevel}` : ""}`, { credentials: "include" }),
        fetch(`/api/admin/instances/${slug}/audit?limit=20`, { credentials: "include" }),
      ]);
      if (d.status === 404) { setError("Instance not found."); return; }
      if (d.status === 403) { setError("Admin role required."); return; }
      if (!d.ok) { setError(`HTTP ${d.status}`); return; }
      setDetail(await d.json());
      setLogs(l.ok ? (await l.json()).items : []);
      setAudit(a.ok ? (await a.json()).items : []);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  useEffect(() => {
    loadAll();
    const stream = new EventSource(
      `/api/activity/stream?type=instance_status,instance_action,instance_upstream&slug=${slug}`,
    );
    stream.onmessage = () => loadAll();
    return () => stream.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug, logLevel]);

  async function runAction(action: string, body?: Record<string, unknown>) {
    setActionBusy(action);
    try {
      const path =
        action === "force_terminate" ? "terminate" :
        action === "reprovision" ? "reprovision" :
        action === "rotate_git_token" ? "rotate-token" :
        "extend-expiry";
      const res = await fetch(`/api/admin/instances/${slug}/${path}`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body ?? {}),
      });
      if (!res.ok) {
        const t = await res.text();
        alert(`${action} failed: ${t}`);
        return;
      }
      const data: Record<string, unknown> = await res.json();
      if (action === "reprovision" && typeof data.new_slug === "string") {
        newSlugRef.current = data.new_slug;
      }
      await loadAll();
    } finally {
      setActionBusy(null);
      if (newSlugRef.current) {
        const ns = newSlugRef.current;
        newSlugRef.current = null;
        // Switch to the freshly provisioned instance.
        setTimeout(() => {
          // Instead of full reload, swap detail; but since this component is
          // keyed on `slug` from the parent, easiest is to call onBack and
          // let the parent re-route via initialSlug. We just notify.
          alert(`Reprovisioned → ${ns}. Returning to list.`);
          onBack();
        }, 200);
      }
    }
  }

  if (error) {
    return (
      <div className="space-y-4">
        <button onClick={onBack} className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
          <ArrowLeftIcon className="size-4" /> Back to list
        </button>
        <div className="rounded-xl border border-red-500/30 bg-red-500/5 p-6 text-sm text-red-400">{error}</div>
      </div>
    );
  }
  if (!detail) {
    return <div className="text-sm text-muted-foreground py-4">Loading…</div>;
  }

  const actions = detail.available_actions;

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-start justify-between gap-3">
        <div>
          <button onClick={onBack} className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground mb-2">
            <ArrowLeftIcon className="size-4" /> Back to list
          </button>
          <h2 className="text-xl font-semibold font-mono text-foreground flex items-center gap-3">
            {detail.slug}
            <StatusPill status={detail.status} />
          </h2>
          <div className="text-xs text-muted-foreground mt-1">
            {detail.user.deleted ? "(user deleted)" : `${detail.user.name}${detail.user.id ? " #" + detail.user.id : ""}`}
            {" · "}
            {detail.project.deleted ? "(project deleted)" : detail.project.name}
            {" · "}
            {detail.status_age_seconds}s in this state
          </div>
        </div>
        <div className="flex flex-wrap gap-2 justify-end">
          {actions.includes("open_preview") && detail.preview_url && (
            <a
              href={detail.preview_url}
              target="_blank" rel="noreferrer"
              className="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg text-xs bg-secondary text-foreground hover:bg-accent border border-border"
            >
              <ExternalLinkIcon className="size-3.5" /> Preview
            </a>
          )}
          {actions.includes("open_in_chat") && detail.chat.link && (
            <a
              href={detail.chat.link}
              className="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg text-xs bg-secondary text-foreground hover:bg-accent border border-border"
            >
              Open in Chat
            </a>
          )}
          {actions.includes("rotate_git_token") && (
            <button
              onClick={() => setConfirm({ action: "rotate_git_token", label: "Rotate Git Token" })}
              disabled={actionBusy !== null}
              className="px-3 py-1.5 rounded-lg text-xs bg-secondary text-foreground hover:bg-accent border border-border disabled:opacity-50"
            >
              Rotate Git Token
            </button>
          )}
          {actions.includes("extend_expiry") && (
            <div className="inline-flex items-center gap-1 px-2 py-1 rounded-lg bg-secondary border border-border">
              <span className="text-xs text-muted-foreground">Extend:</span>
              {[1, 4, 24].map((h) => (
                <button
                  key={h}
                  onClick={() => setConfirm({ action: "extend_expiry", label: `Extend +${h}h`, body: { extend_hours: h } })}
                  disabled={actionBusy !== null}
                  className="px-2 py-0.5 rounded text-xs text-foreground hover:bg-accent disabled:opacity-50"
                >
                  +{h}h
                </button>
              ))}
            </div>
          )}
          {actions.includes("reprovision") && (
            <button
              onClick={() => setConfirm({ action: "reprovision", label: "Reprovision", body: { confirm: true } })}
              disabled={actionBusy !== null}
              className="px-3 py-1.5 rounded-lg text-xs bg-red-500/20 text-red-300 hover:bg-red-500/30 border border-red-500/30 disabled:opacity-50"
            >
              Reprovision
            </button>
          )}
          {actions.includes("force_terminate") && (
            <button
              onClick={() => setConfirm({ action: "force_terminate", label: "Force Terminate", body: { confirm: true } })}
              disabled={actionBusy !== null}
              className="px-3 py-1.5 rounded-lg text-xs bg-red-500/20 text-red-300 hover:bg-red-500/30 border border-red-500/30 disabled:opacity-50"
            >
              Force Terminate
            </button>
          )}
        </div>
      </div>

      {/* Failure banner */}
      {detail.failure && (
        <div className="rounded-xl border border-red-500/30 bg-red-500/5 p-4">
          <div className="text-xs font-semibold uppercase tracking-wide text-red-400 mb-1">Failure</div>
          <div className="text-sm">
            <span className="font-mono text-red-300 mr-2">{detail.failure.code}</span>
            <span className="text-foreground">{detail.failure.message}</span>
          </div>
        </div>
      )}

      {/* Tunnel section */}
      <div className="rounded-xl border border-border bg-card p-4">
        <div className="flex items-center justify-between mb-2">
          <h3 className="text-sm font-semibold text-foreground">Tunnel</h3>
          <span className={`text-xs px-2 py-0.5 rounded-full ${
            detail.tunnel.health === "live"        ? "bg-green-500/15 text-green-400 border border-green-500/20" :
            detail.tunnel.health === "degraded"    ? "bg-amber-500/15 text-amber-400 border border-amber-500/20" :
            detail.tunnel.health === "unreachable" ? "bg-red-500/15 text-red-400 border border-red-500/20" :
            "bg-secondary text-muted-foreground border border-border"
          }`}>
            {detail.tunnel.health || "unknown"}
          </span>
        </div>
        <div className="text-sm">
          {detail.tunnel.url ? (
            <a href={detail.tunnel.url} target="_blank" rel="noreferrer" className="text-blue-400 hover:underline break-all">{detail.tunnel.url}</a>
          ) : (
            <span className="text-muted-foreground">— no tunnel —</span>
          )}
        </div>
      </div>

      {/* Two columns: Timeline + Diagnostics */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="rounded-xl border border-border bg-card p-4">
          <h3 className="text-sm font-semibold text-foreground mb-3">Timeline</h3>
          {detail.transitions.length === 0 ? (
            <div className="text-xs text-muted-foreground">No transitions recorded.</div>
          ) : (
            <ol className="space-y-2 text-sm">
              {detail.transitions.map((t, i) => (
                <li key={i} className="flex items-baseline gap-2">
                  <span className="text-xs text-muted-foreground w-44 shrink-0">{t.at}</span>
                  <StatusPill status={t.status} />
                  <span className="text-xs text-muted-foreground">{t.note ?? ""}</span>
                </li>
              ))}
            </ol>
          )}
        </div>
        <div className="rounded-xl border border-border bg-card p-4">
          <h3 className="text-sm font-semibold text-foreground mb-3">Diagnostics</h3>
          <dl className="text-sm space-y-1.5 grid grid-cols-3 gap-y-1.5 gap-x-2">
            <DT k="Compose project">{detail.compose_project ?? "—"}</DT>
            <DT k="Workspace">{detail.workspace_path ?? "—"}</DT>
            <DT k="Session branch">{detail.session_branch ?? "—"}</DT>
            <DT k="Image digest">{detail.image_digest ?? "—"}</DT>
            <DT k="Profile">{detail.resource_profile ?? "—"}</DT>
            <DT k="Created">{detail.created_at} ({fmtRelative(detail.created_at)})</DT>
            <DT k="Started">{detail.started_at ?? "—"}</DT>
            <DT k="Last activity">{detail.last_activity_at ?? "—"}</DT>
            <DT k="Expires">{detail.expires_at ?? "—"}</DT>
            <DT k="Terminated">{detail.terminated_at ?? "—"}{detail.terminated_reason ? ` · ${detail.terminated_reason}` : ""}</DT>
          </dl>
        </div>
      </div>

      {/* Logs */}
      <div className="rounded-xl border border-border bg-card p-4">
        <div className="flex items-center justify-between mb-2">
          <h3 className="text-sm font-semibold text-foreground">Recent worker log lines</h3>
          <select
            value={logLevel}
            onChange={(e) => setLogLevel(e.target.value)}
            className="text-xs px-2 py-1 rounded bg-secondary border border-border text-foreground"
          >
            <option value="">all levels</option>
            <option value="error">error</option>
            <option value="warning">warning</option>
            <option value="info">info</option>
            <option value="debug">debug</option>
          </select>
        </div>
        {logs.length === 0 ? (
          <div className="text-xs text-muted-foreground">No log lines for this instance yet.</div>
        ) : (
          <div className="font-mono text-xs space-y-1 max-h-96 overflow-y-auto">
            {logs.map((l, i) => (
              <div key={i}>
                <span className="text-muted-foreground">{l.ts}</span>{" "}
                <span className="text-foreground">[{l.level}]</span>{" "}
                <span className="text-foreground">{l.message}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Audit */}
      <div className="rounded-xl border border-border bg-card p-4">
        <h3 className="text-sm font-semibold text-foreground mb-3">Recent audit</h3>
        {audit.length === 0 ? (
          <div className="text-xs text-muted-foreground">No audit entries yet.</div>
        ) : (
          <ul className="space-y-2 text-sm">
            {audit.map((e, i) => (
              <li key={i} className={`border-l-2 pl-2 ${
                e.risk_level === "dangerous" ? "border-red-400" :
                e.risk_level === "elevated"  ? "border-amber-400" :
                "border-border"
              }`}>
                <div className="text-xs text-muted-foreground">{e.created_at} · {e.actor}</div>
                <div className="text-sm">
                  <span className="font-medium">{e.action}</span>
                  {e.blocked && <span className="text-xs text-muted-foreground ml-2">(blocked — already-ended)</span>}
                </div>
                <div className="text-xs font-mono text-muted-foreground">{e.command}</div>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Confirm dialog */}
      {confirm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" onClick={() => setConfirm(null)}>
          <div className="w-full max-w-md rounded-2xl border border-border bg-card p-6" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-start gap-3">
              <ServerCogIcon className="size-5 text-amber-400 shrink-0 mt-0.5" />
              <div className="flex-1">
                <h3 className="font-semibold text-foreground">{confirm.label}?</h3>
                <p className="text-sm text-muted-foreground mt-1">
                  Target: <span className="font-mono text-xs">{detail.slug}</span>
                </p>
              </div>
            </div>
            <div className="mt-5 flex justify-end gap-2">
              <button onClick={() => setConfirm(null)} className="px-3 py-1.5 rounded-lg text-sm text-muted-foreground hover:text-foreground">
                Cancel
              </button>
              <button
                onClick={async () => {
                  const c = confirm;
                  setConfirm(null);
                  await runAction(c.action, c.body);
                }}
                className="px-3 py-1.5 rounded-lg text-sm bg-red-500/20 text-red-300 hover:bg-red-500/30 border border-red-500/30"
              >
                Confirm
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function DT({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <>
      <dt className="text-muted-foreground col-span-1">{k}</dt>
      <dd className="col-span-2 text-foreground text-xs break-all">{children}</dd>
    </>
  );
}
