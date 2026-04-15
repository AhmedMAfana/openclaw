/**
 * AccessPanel — admin-only dialog to manage user-project access grants.
 *
 * Tabs:
 *   Summary  — all grants (user × project × role)
 *   Grant    — create a new grant
 */
import { useState, useEffect } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { ShieldIcon, XIcon, TrashIcon } from "lucide-react";

// ── Types ─────────────────────────────────────────────────────────────────────

interface Grant {
  id: number;
  user_id: number;
  username: string;
  project_id: number;
  project_name: string;
  role: string;
  granted_by: number | null;
}

interface User {
  id: number;
  username: string;
}

interface Project {
  id: number;
  name: string;
}

const ROLE_COLORS: Record<string, string> = {
  developer: "bg-blue-500/15 text-blue-400 border border-blue-500/30",
  viewer:    "bg-zinc-500/15 text-zinc-400 border border-zinc-500/30",
  deployer:  "bg-yellow-500/15 text-yellow-400 border border-yellow-500/30",
  all:       "bg-purple-500/15 text-purple-400 border border-purple-500/30",
};

const ROLES = ["developer", "viewer", "deployer", "all"];

// ── Component ─────────────────────────────────────────────────────────────────

interface AccessPanelProps {
  open: boolean;
  onClose: () => void;
}

export function AccessPanel({ open, onClose }: AccessPanelProps) {
  const [tab, setTab] = useState<"summary" | "grant">("summary");
  const [grants, setGrants] = useState<Grant[]>([]);
  const [users, setUsers] = useState<User[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Grant form state
  const [grantUserId, setGrantUserId] = useState<string>("");
  const [grantProjectId, setGrantProjectId] = useState<string>("");
  const [grantRole, setGrantRole] = useState("developer");
  const [grantLoading, setGrantLoading] = useState(false);
  const [grantError, setGrantError] = useState<string | null>(null);
  const [grantSuccess, setGrantSuccess] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    loadAll();
  }, [open]);

  async function loadAll() {
    setLoading(true);
    setError(null);
    try {
      const [grantsRes, usersRes, projectsRes] = await Promise.all([
        fetch("/api/access/summary", { credentials: "include" }),
        fetch("/api/access/users-list", { credentials: "include" }),
        fetch("/api/access/projects-list", { credentials: "include" }),
      ]);
      if (grantsRes.ok) setGrants(await grantsRes.json());
      if (usersRes.ok) {
        const d = await usersRes.json();
        setUsers(d.users ?? []);
      }
      if (projectsRes.ok) {
        const d = await projectsRes.json();
        setProjects(d.projects ?? []);
      }
    } catch (e) {
      setError("Failed to load access data");
    } finally {
      setLoading(false);
    }
  }

  async function revokeGrant(id: number) {
    const res = await fetch(`/api/access/grants/${id}`, {
      method: "DELETE",
      credentials: "include",
    });
    if (res.ok) {
      setGrants((prev) => prev.filter((g) => g.id !== id));
    }
  }

  async function submitGrant() {
    if (!grantUserId || !grantProjectId) {
      setGrantError("Select a user and project");
      return;
    }
    setGrantLoading(true);
    setGrantError(null);
    setGrantSuccess(null);
    try {
      const res = await fetch("/api/access/grants", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          user_id: Number(grantUserId),
          project_id: Number(grantProjectId),
          role: grantRole,
        }),
      });
      if (res.ok) {
        setGrantSuccess(`Access granted.`);
        setGrantUserId("");
        setGrantProjectId("");
        setGrantRole("developer");
        await loadAll();
      } else {
        const d = await res.json().catch(() => ({}));
        setGrantError(d.detail ?? `Error ${res.status}`);
      }
    } catch {
      setGrantError("Network error");
    } finally {
      setGrantLoading(false);
    }
  }

  return (
    <Dialog.Root open={open} onOpenChange={(o) => { if (!o) onClose(); }}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0" />
        <Dialog.Content className="fixed left-1/2 top-1/2 z-50 w-full max-w-2xl -translate-x-1/2 -translate-y-1/2 rounded-2xl border border-border bg-card shadow-2xl data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95 flex flex-col max-h-[85vh]">
          {/* Header */}
          <div className="flex items-center gap-3 px-6 pt-5 pb-4 border-b border-border shrink-0">
            <ShieldIcon className="size-5 text-primary" />
            <Dialog.Title className="text-base font-semibold text-foreground flex-1">
              Access Management
            </Dialog.Title>
            <Dialog.Close asChild>
              <button className="p-1.5 rounded-lg hover:bg-accent transition-colors text-muted-foreground hover:text-foreground">
                <XIcon className="size-4" />
              </button>
            </Dialog.Close>
          </div>

          {/* Tabs */}
          <div className="flex gap-1 px-6 pt-3 shrink-0">
            {(["summary", "grant"] as const).map((t) => (
              <button
                key={t}
                onClick={() => setTab(t)}
                className={`px-4 py-1.5 rounded-lg text-sm font-medium transition-colors capitalize ${
                  tab === t
                    ? "bg-primary text-primary-foreground"
                    : "text-muted-foreground hover:bg-accent hover:text-foreground"
                }`}
              >
                {t === "summary" ? "All Grants" : "Grant Access"}
              </button>
            ))}
          </div>

          {/* Body */}
          <div className="flex-1 overflow-y-auto px-6 py-4">
            {loading && <p className="text-sm text-muted-foreground">Loading...</p>}
            {error && <p className="text-sm text-destructive">{error}</p>}

            {/* ── Summary tab ── */}
            {tab === "summary" && !loading && (
              <>
                {grants.length === 0 ? (
                  <p className="text-sm text-muted-foreground py-4 text-center">
                    No grants yet. Use "Grant Access" to assign users to projects.
                  </p>
                ) : (
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="text-left text-xs text-muted-foreground border-b border-border">
                        <th className="pb-2 font-medium">User</th>
                        <th className="pb-2 font-medium">Project</th>
                        <th className="pb-2 font-medium">Role</th>
                        <th className="pb-2" />
                      </tr>
                    </thead>
                    <tbody>
                      {grants.map((g) => (
                        <tr key={g.id} className="border-b border-border/50 hover:bg-accent/30 transition-colors">
                          <td className="py-2.5 pr-4 text-foreground font-medium">{g.username}</td>
                          <td className="py-2.5 pr-4 text-muted-foreground">{g.project_name}</td>
                          <td className="py-2.5 pr-4">
                            <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${ROLE_COLORS[g.role] ?? ""}`}>
                              {g.role}
                            </span>
                          </td>
                          <td className="py-2.5 text-right">
                            <button
                              onClick={() => revokeGrant(g.id)}
                              className="p-1 rounded hover:bg-destructive/20 text-muted-foreground hover:text-destructive transition-colors"
                              title="Revoke"
                            >
                              <TrashIcon className="size-3.5" />
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </>
            )}

            {/* ── Grant tab ── */}
            {tab === "grant" && (
              <div className="flex flex-col gap-4 max-w-sm">
                {grantError && (
                  <p className="text-sm text-destructive bg-destructive/10 px-3 py-2 rounded-lg">{grantError}</p>
                )}
                {grantSuccess && (
                  <p className="text-sm text-green-400 bg-green-500/10 px-3 py-2 rounded-lg">{grantSuccess}</p>
                )}

                <label className="flex flex-col gap-1.5">
                  <span className="text-xs font-medium text-muted-foreground uppercase tracking-wide">User</span>
                  <select
                    value={grantUserId}
                    onChange={(e) => setGrantUserId(e.target.value)}
                    className="px-3 py-2 rounded-lg text-sm bg-secondary border border-border text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
                  >
                    <option value="">Select user…</option>
                    {users.map((u) => (
                      <option key={u.id} value={u.id}>{u.username}</option>
                    ))}
                  </select>
                </label>

                <label className="flex flex-col gap-1.5">
                  <span className="text-xs font-medium text-muted-foreground uppercase tracking-wide">Project</span>
                  <select
                    value={grantProjectId}
                    onChange={(e) => setGrantProjectId(e.target.value)}
                    className="px-3 py-2 rounded-lg text-sm bg-secondary border border-border text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
                  >
                    <option value="">Select project…</option>
                    {projects.map((p) => (
                      <option key={p.id} value={p.id}>{p.name}</option>
                    ))}
                  </select>
                </label>

                <label className="flex flex-col gap-1.5">
                  <span className="text-xs font-medium text-muted-foreground uppercase tracking-wide">Role</span>
                  <select
                    value={grantRole}
                    onChange={(e) => setGrantRole(e.target.value)}
                    className="px-3 py-2 rounded-lg text-sm bg-secondary border border-border text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
                  >
                    {ROLES.map((r) => (
                      <option key={r} value={r}>{r}</option>
                    ))}
                  </select>
                  <p className="text-xs text-muted-foreground mt-0.5">
                    {grantRole === "developer" && "Can trigger coding tasks, add projects, QA"}
                    {grantRole === "viewer" && "Read-only: list projects and tasks"}
                    {grantRole === "deployer" && "Ops: bootstrap, docker up/down, relink"}
                    {grantRole === "all" && "Full access to assigned projects (no system admin)"}
                  </p>
                </label>

                <button
                  onClick={submitGrant}
                  disabled={grantLoading}
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-primary hover:bg-primary/90 text-primary-foreground transition-colors disabled:opacity-50 disabled:cursor-not-allowed w-fit"
                >
                  {grantLoading ? "Granting…" : "Grant Access"}
                </button>
              </div>
            )}
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
