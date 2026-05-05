import { useState, useEffect } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { PlusIcon, TrashIcon, PencilIcon } from "lucide-react";

interface Project {
  id: number;
  name: string;
  github_repo: string;
  default_branch: string;
  tech_stack: string | null;
  is_dockerized: boolean;
  mode?: string;
  project_dir?: string | null;
  start_command?: string | null;
  stop_command?: string | null;
  health_url?: string | null;
  process_manager?: string | null;
  public_url?: string | null;
  tunnel_enabled?: boolean;
  app_port?: number | null;
}

const ic =
  "w-full px-3 py-2 rounded-lg text-sm bg-secondary border border-border text-foreground focus:outline-none focus:ring-1 focus:ring-ring";
const lc = "block text-sm font-medium text-foreground mb-1";

const EMPTY = {
  github_repo: "",
  name: "",
  default_branch: "main",
  description: "",
  mode: "docker",
  is_dockerized: false,
  docker_compose_file: "docker-compose.yml",
  app_container_name: "",
  app_port: "",
};

export function SettingsProjects({ onProjectsChanged }: { onProjectsChanged?: () => void } = {}) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);
  const [addOpen, setAddOpen] = useState(false);
  const [form, setForm] = useState(EMPTY);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<Project | null>(null);
  const [editTarget, setEditTarget] = useState<Project | null>(null);
  const [editDraft, setEditDraft] = useState<Partial<Project>>({});
  const [editSaving, setEditSaving] = useState(false);
  const [editError, setEditError] = useState("");
  // Connected GitHub repos — loaded lazily when the Add modal opens.
  // Keeps the Settings page snappy and avoids a 2 s GitHub round-trip on
  // first render. `null` = not loaded yet, `[]` = loaded but empty.
  type GhRepo = { full_name: string; default_branch: string; private: boolean; description: string | null };
  const [ghRepos, setGhRepos] = useState<GhRepo[] | null>(null);
  const [ghLoading, setGhLoading] = useState(false);
  const [ghError, setGhError] = useState("");
  const [ghQuery, setGhQuery] = useState("");
  const [showRepoDropdown, setShowRepoDropdown] = useState(false);

  useEffect(() => {
    load();
  }, []);

  async function loadGhRepos() {
    if (ghRepos !== null || ghLoading) return;
    setGhLoading(true);
    setGhError("");
    try {
      const res = await fetch("/api/settings/projects/github-repos", { credentials: "include" });
      if (res.ok) {
        const d = await res.json();
        setGhRepos(d.repos || []);
      } else {
        const d = await res.json().catch(() => ({}));
        setGhError(d.detail || `Failed to load repos (${res.status})`);
      }
    } catch (e) {
      setGhError(String(e));
    } finally {
      setGhLoading(false);
    }
  }

  useEffect(() => {
    if (addOpen) loadGhRepos();
  }, [addOpen]);

  // Branches for the Edit modal — fetched per-project on open. Lets the
  // user point a chat at a working branch instead of a broken main
  // (e.g. tagh-test where main has a Vite config bug; a feature branch
  // has the fix). One round-trip to GitHub per Edit-open; not cached
  // because branches change frequently outside our system.
  type GhBranch = { name: string; is_default: boolean; protected: boolean };
  // Branches for Add Project — keyed by repo name (no project row exists
  // yet). Re-fetched whenever the user picks a different repo.
  const [addBranches, setAddBranches] = useState<GhBranch[] | null>(null);
  const [addBranchesLoading, setAddBranchesLoading] = useState(false);
  const [addBranchesError, setAddBranchesError] = useState("");
  useEffect(() => {
    setAddBranches(null);
    setAddBranchesError("");
    if (!addOpen || !form.github_repo || !form.github_repo.includes("/")) return;
    const repo = form.github_repo;
    setAddBranchesLoading(true);
    (async () => {
      try {
        const res = await fetch(
          `/api/settings/projects/branches?repo=${encodeURIComponent(repo)}`,
          { credentials: "include" },
        );
        if (res.ok) {
          const d = await res.json();
          if (form.github_repo === repo) setAddBranches(d.branches || []);
        } else {
          const d = await res.json().catch(() => ({}));
          if (form.github_repo === repo) {
            setAddBranchesError(d.detail || `Failed to load branches (${res.status})`);
          }
        }
      } catch (e) {
        if (form.github_repo === repo) setAddBranchesError(String(e));
      } finally {
        if (form.github_repo === repo) setAddBranchesLoading(false);
      }
    })();
  }, [addOpen, form.github_repo]);

  const [editBranches, setEditBranches] = useState<GhBranch[] | null>(null);
  const [editBranchesLoading, setEditBranchesLoading] = useState(false);
  const [editBranchesError, setEditBranchesError] = useState("");
  useEffect(() => {
    setEditBranches(null);
    setEditBranchesError("");
    if (!editTarget) return;
    setEditBranchesLoading(true);
    (async () => {
      try {
        const res = await fetch(
          `/api/settings/projects/${editTarget.id}/branches`,
          { credentials: "include" },
        );
        if (res.ok) {
          const d = await res.json();
          setEditBranches(d.branches || []);
        } else {
          const d = await res.json().catch(() => ({}));
          setEditBranchesError(d.detail || `Failed to load branches (${res.status})`);
        }
      } catch (e) {
        setEditBranchesError(String(e));
      } finally {
        setEditBranchesLoading(false);
      }
    })();
  }, [editTarget?.id]);

  async function load() {
    setLoading(true);
    const res = await fetch("/api/settings/projects", { credentials: "include" });
    if (res.ok) setProjects(await res.json());
    setLoading(false);
  }

  async function add() {
    if (!form.github_repo || !form.name) {
      setFormError("Repository and name are required");
      return;
    }
    setSubmitting(true);
    setFormError("");
    const payload: Record<string, unknown> = {
      github_repo: form.github_repo,
      name: form.name,
      default_branch: form.default_branch || "main",
      description: form.description || null,
      mode: form.mode,
      is_dockerized: form.is_dockerized,
    };
    if (form.is_dockerized) {
      if (form.docker_compose_file) payload.docker_compose_file = form.docker_compose_file;
      if (form.app_container_name) payload.app_container_name = form.app_container_name;
      if (form.app_port) payload.app_port = Number(form.app_port);
    }
    try {
      const res = await fetch("/api/settings/projects", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (res.ok) {
        const created = await res.json();
        setProjects((p) => [...p, created]);
        setAddOpen(false);
        setForm(EMPTY);
        onProjectsChanged?.();
      } else {
        const d = await res.json();
        setFormError(d.detail || "Failed");
      }
    } finally {
      setSubmitting(false);
    }
  }

  async function del(p: Project) {
    const res = await fetch(`/api/settings/projects/${p.id}`, {
      method: "DELETE",
      credentials: "include",
    });
    if (res.ok) {
      setProjects((prev) => prev.filter((x) => x.id !== p.id));
      onProjectsChanged?.();
    }
    setDeleteTarget(null);
  }

  function openEdit(p: Project) {
    setEditTarget(p);
    setEditDraft({
      default_branch: p.default_branch ?? "main",
      mode: p.mode ?? "docker",
      project_dir: p.project_dir ?? "",
      start_command: p.start_command ?? "",
      stop_command: p.stop_command ?? "",
      health_url: p.health_url ?? "",
      process_manager: p.process_manager ?? "",
      public_url: p.public_url ?? "",
      tunnel_enabled: p.tunnel_enabled ?? true,
      app_port: p.app_port ?? null,
    });
    setEditError("");
  }

  async function saveEdit() {
    if (!editTarget) return;
    setEditSaving(true);
    setEditError("");
    try {
      // Normalize empty strings to null so the backend sees "cleared" not "empty string"
      const payload: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(editDraft)) {
        if (typeof v === "string" && v.trim() === "") payload[k] = null;
        else payload[k] = v;
      }
      const res = await fetch(`/api/settings/projects/${editTarget.id}`, {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (res.ok) {
        const updated = await res.json();
        setProjects((prev) => prev.map((x) => (x.id === updated.id ? updated : x)));
        setEditTarget(null);
      } else {
        const d = await res.json();
        setEditError(d.detail || "Failed to save");
      }
    } catch {
      setEditError("Request failed");
    } finally {
      setEditSaving(false);
    }
  }

  return (
    <div className="space-y-4 max-w-5xl">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          {projects.length} active project{projects.length !== 1 ? "s" : ""}
        </p>
        <button
          onClick={() => {
            setForm(EMPTY);
            setFormError("");
            setAddOpen(true);
          }}
          className="flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium bg-primary hover:bg-primary/90 text-primary-foreground transition-colors"
        >
          <PlusIcon className="size-4" /> Add Project
        </button>
      </div>

      {loading ? (
        <div className="text-sm text-muted-foreground py-4">Loading…</div>
      ) : projects.length === 0 ? (
        <div className="rounded-xl border border-border bg-card p-8 text-center text-sm text-muted-foreground">
          No projects yet.
        </div>
      ) : (
        <div className="rounded-xl border border-border bg-card overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border">
                {["Name", "Repository", "Mode", "Public URL / Tunnel", "Stack", ""].map((h) => (
                  <th
                    key={h}
                    className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider"
                  >
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {projects.map((p) => (
                <tr key={p.id} className="hover:bg-accent/30 transition-colors">
                  <td className="px-4 py-3 font-medium text-foreground">{p.name}</td>
                  <td className="px-4 py-3 text-muted-foreground font-mono text-xs">
                    {p.github_repo}
                  </td>
                  <td className="px-4 py-3 text-xs">
                    {p.mode === "host" ? (
                      <span className="px-2 py-0.5 rounded-full bg-green-500/15 text-green-400 border border-green-500/20">
                        host
                      </span>
                    ) : (
                      <span className="px-2 py-0.5 rounded-full bg-blue-500/15 text-blue-400 border border-blue-500/20">
                        docker
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-xs">
                    {p.public_url && p.tunnel_enabled === false ? (
                      <span className="text-foreground font-mono">{p.public_url}</span>
                    ) : p.public_url ? (
                      <span className="text-muted-foreground">
                        {p.public_url} <span className="text-xs">(+ tunnel)</span>
                      </span>
                    ) : (
                      <span className="text-muted-foreground italic">tunnel only</span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-muted-foreground text-xs">
                    {p.tech_stack || "—"}
                  </td>
                  <td className="px-4 py-3 text-right whitespace-nowrap">
                    <button
                      onClick={() => openEdit(p)}
                      className="p-1.5 rounded-lg text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
                      title="Edit"
                    >
                      <PencilIcon className="size-4" />
                    </button>
                    <button
                      onClick={() => setDeleteTarget(p)}
                      className="p-1.5 rounded-lg text-muted-foreground hover:text-destructive hover:bg-destructive/10 transition-colors"
                      title="Delete"
                    >
                      <TrashIcon className="size-4" />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* ── Add dialog ───────────────────────────────────────────────── */}
      <Dialog.Root open={addOpen} onOpenChange={setAddOpen}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm" />
          <Dialog.Content className="fixed left-1/2 top-1/2 z-50 w-full max-w-lg -translate-x-1/2 -translate-y-1/2 rounded-2xl border border-border bg-card p-6 shadow-2xl max-h-[90vh] overflow-y-auto">
            <Dialog.Title className="text-base font-semibold text-foreground mb-4">
              Add Project
            </Dialog.Title>
            <div className="space-y-3">
              {/* Repository combobox */}
              <div>
                <label className={lc}>
                  Repository <span className="text-red-400">*</span>
                </label>
                <div className="relative">
                  {ghLoading ? (
                    <div className={`${ic} text-muted-foreground`}>Loading repos…</div>
                  ) : (
                    <input
                      type="text"
                      placeholder={
                        ghRepos && ghRepos.length
                          ? `Search ${ghRepos.length} repos…`
                          : "owner/repo"
                      }
                      value={form.github_repo}
                      onFocus={() => setShowRepoDropdown(true)}
                      onBlur={() => setTimeout(() => setShowRepoDropdown(false), 150)}
                      onChange={(e) => {
                        const v = e.target.value;
                        setGhQuery(v);
                        setForm((f) => ({ ...f, github_repo: v }));
                        setShowRepoDropdown(true);
                      }}
                      className={ic}
                    />
                  )}
                  {showRepoDropdown && !ghLoading && (ghRepos?.length ?? 0) > 0 && (
                    <ul className="absolute z-50 mt-1 w-full max-h-52 overflow-y-auto rounded-lg border border-border bg-card shadow-lg text-sm">
                      {(ghRepos || [])
                        .filter(
                          (r) =>
                            !ghQuery ||
                            r.full_name.toLowerCase().includes(ghQuery.toLowerCase()),
                        )
                        .slice(0, 50)
                        .map((r) => (
                          <li
                            key={r.full_name}
                            onMouseDown={() => {
                              setForm((f) => ({
                                ...f,
                                github_repo: r.full_name,
                                default_branch: r.default_branch || f.default_branch,
                                name: f.name || r.full_name.split("/")[1] || "",
                              }));
                              setGhQuery(r.full_name);
                              setShowRepoDropdown(false);
                            }}
                            className="flex items-center gap-2 px-3 py-2 cursor-pointer hover:bg-accent transition-colors"
                          >
                            {r.private && (
                              <span className="text-xs text-muted-foreground">🔒</span>
                            )}
                            <span className="font-mono text-foreground">{r.full_name}</span>
                            {r.description && (
                              <span className="ml-auto text-xs text-muted-foreground truncate max-w-[160px]">
                                {r.description}
                              </span>
                            )}
                          </li>
                        ))}
                    </ul>
                  )}
                </div>
                {ghError && (
                  <div className="text-xs text-amber-500 mt-1">
                    {ghError} — type the repo manually
                  </div>
                )}
              </div>

              {/* Project Name */}
              <div>
                <label className={lc}>
                  Project Name <span className="text-red-400">*</span>
                </label>
                <input
                  type="text"
                  placeholder="My App"
                  value={form.name}
                  onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
                  className={ic}
                />
              </div>

              {/* Mode + Default Branch row */}
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className={lc}>Mode</label>
                  <select
                    className={ic}
                    value={form.mode}
                    onChange={(e) => setForm((f) => ({ ...f, mode: e.target.value }))}
                  >
                    <option value="docker">docker</option>
                    <option value="host">host (running on VPS)</option>
                    <option value="container">container (per-chat)</option>
                  </select>
                </div>
                <div>
                  <label className={lc}>Default Branch</label>
                  {addBranchesLoading ? (
                    <div className={`${ic} text-muted-foreground`}>Loading branches…</div>
                  ) : (
                    <select
                      className={ic}
                      value={form.default_branch}
                      onChange={(e) => setForm((f) => ({ ...f, default_branch: e.target.value }))}
                    >
                      {(addBranches && addBranches.length > 0
                        ? addBranches.map((b) => b.name)
                        : [form.default_branch || "main"]
                      ).map((b) => (
                        <option key={b} value={b}>
                          {b}
                        </option>
                      ))}
                    </select>
                  )}
                  {addBranchesError && (
                    <div className="text-xs text-amber-500 mt-1">
                      {addBranchesError}
                    </div>
                  )}
                </div>
              </div>

              <div>
                <label className={lc}>Description</label>
                <textarea
                  rows={2}
                  placeholder="Optional"
                  value={form.description}
                  onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
                  className={`${ic} resize-none`}
                />
              </div>
              <label className="flex items-center gap-2.5 cursor-pointer">
                <input
                  type="checkbox"
                  checked={form.is_dockerized}
                  onChange={(e) => setForm((f) => ({ ...f, is_dockerized: e.target.checked }))}
                  className="size-4 rounded"
                />
                <span className="text-sm text-foreground">Dockerized</span>
              </label>
              {form.is_dockerized && (
                <div className="pl-6 space-y-3 border-l-2 border-border">
                  <div>
                    <label className={lc}>Compose File</label>
                    <input
                      type="text"
                      placeholder="docker-compose.yml"
                      value={form.docker_compose_file}
                      onChange={(e) =>
                        setForm((f) => ({ ...f, docker_compose_file: e.target.value }))
                      }
                      className={ic}
                    />
                  </div>
                  <div className="grid grid-cols-2 gap-3">
                    <div>
                      <label className={lc}>App Container</label>
                      <input
                        type="text"
                        placeholder="app"
                        value={form.app_container_name}
                        onChange={(e) =>
                          setForm((f) => ({ ...f, app_container_name: e.target.value }))
                        }
                        className={ic}
                      />
                    </div>
                    <div>
                      <label className={lc}>App Port</label>
                      <input
                        type="number"
                        placeholder="8000"
                        value={form.app_port}
                        onChange={(e) => setForm((f) => ({ ...f, app_port: e.target.value }))}
                        className={ic}
                      />
                    </div>
                  </div>
                </div>
              )}
              {formError && <p className="text-xs text-red-400">{formError}</p>}
            </div>
            <div className="mt-5 flex gap-3 justify-end">
              <Dialog.Close asChild>
                <button className="px-4 py-2 rounded-lg text-sm font-medium border border-border bg-background hover:bg-accent text-foreground transition-colors">
                  Cancel
                </button>
              </Dialog.Close>
              <button
                onClick={add}
                disabled={submitting}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-primary hover:bg-primary/90 text-primary-foreground transition-colors disabled:opacity-50"
              >
                {submitting ? "Adding…" : "Add Project"}
              </button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      {/* ── Edit dialog (host mode + public URL) ───────────────────────── */}
      <Dialog.Root open={!!editTarget} onOpenChange={(o) => !o && setEditTarget(null)}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm" />
          <Dialog.Content className="fixed left-1/2 top-1/2 z-50 w-full max-w-xl -translate-x-1/2 -translate-y-1/2 rounded-2xl border border-border bg-card p-6 shadow-2xl max-h-[90vh] overflow-y-auto">
            <Dialog.Title className="text-base font-semibold text-foreground mb-1">
              Edit {editTarget?.name}
            </Dialog.Title>
            <Dialog.Description className="text-xs text-muted-foreground mb-4">
              Host-mode settings and public URL. Fields apply only when mode = host.
            </Dialog.Description>
            <div className="space-y-3">
              <div>
                <label className={lc}>
                  Default Branch{" "}
                  <span className="text-xs text-muted-foreground font-normal">
                    (every new chat clones from here)
                  </span>
                </label>
                {editBranchesLoading ? (
                  <div className={`${ic} text-muted-foreground`}>Loading branches…</div>
                ) : (
                  <>
                    <input
                      type="text"
                      list={`branch-list-${editTarget?.id}`}
                      placeholder={
                        editBranches && editBranches.length
                          ? `Search ${editBranches.length} branches…`
                          : "main"
                      }
                      value={editDraft.default_branch ?? ""}
                      onChange={(e) =>
                        setEditDraft((d) => ({ ...d, default_branch: e.target.value }))
                      }
                      className={ic}
                    />
                    <datalist id={`branch-list-${editTarget?.id}`}>
                      {(editBranches || []).map((b) => (
                        <option key={b.name} value={b.name}>
                          {b.is_default ? "★ default" : ""}
                          {b.protected ? " 🔒 protected" : ""}
                        </option>
                      ))}
                    </datalist>
                  </>
                )}
                {editBranchesError && (
                  <div className="text-xs text-amber-500 mt-1">
                    {editBranchesError} — type the branch manually
                  </div>
                )}
              </div>

              <div>
                <label className={lc}>Mode</label>
                <select
                  className={ic}
                  value={editDraft.mode ?? "docker"}
                  onChange={(e) => setEditDraft((d) => ({ ...d, mode: e.target.value }))}
                >
                  <option value="docker">docker (containerized)</option>
                  <option value="host">host (already-running on VPS)</option>
                </select>
              </div>

              <div>
                <label className={lc}>Public URL (your owned domain, e.g. https://tagh.example.com)</label>
                <input
                  type="text"
                  placeholder="https://app.yourdomain.com"
                  value={editDraft.public_url ?? ""}
                  onChange={(e) => setEditDraft((d) => ({ ...d, public_url: e.target.value }))}
                  className={ic}
                />
                <p className="mt-1 text-xs text-muted-foreground">
                  Used when the tunnel is disabled. nginx on the VPS proxies this domain to the app port.
                </p>
              </div>

              <label className="flex items-start gap-2.5 cursor-pointer">
                <input
                  type="checkbox"
                  checked={editDraft.tunnel_enabled !== false}
                  onChange={(e) =>
                    setEditDraft((d) => ({ ...d, tunnel_enabled: e.target.checked }))
                  }
                  className="size-4 rounded mt-0.5"
                />
                <span className="text-sm">
                  <span className="text-foreground">Enable cloudflared tunnel</span>
                  <br />
                  <span className="text-xs text-muted-foreground">
                    Turn this OFF if you have your own domain + nginx — the Public URL above is used as-is.
                  </span>
                </span>
              </label>

              {editDraft.mode === "host" && (
                <div className="pl-3 space-y-3 border-l-2 border-border">
                  <div>
                    <label className={lc}>Project directory on the host</label>
                    <input
                      type="text"
                      placeholder="/srv/projects/my-app"
                      value={editDraft.project_dir ?? ""}
                      onChange={(e) =>
                        setEditDraft((d) => ({ ...d, project_dir: e.target.value }))
                      }
                      className={ic}
                    />
                  </div>

                  <div className="grid grid-cols-2 gap-3">
                    <div>
                      <label className={lc}>App port</label>
                      <input
                        type="number"
                        placeholder="8000"
                        value={editDraft.app_port ?? ""}
                        onChange={(e) =>
                          setEditDraft((d) => ({
                            ...d,
                            app_port: e.target.value ? Number(e.target.value) : null,
                          }))
                        }
                        className={ic}
                      />
                    </div>
                    <div>
                      <label className={lc}>Process manager</label>
                      <select
                        className={ic}
                        value={editDraft.process_manager ?? "manual"}
                        onChange={(e) =>
                          setEditDraft((d) => ({ ...d, process_manager: e.target.value }))
                        }
                      >
                        <option value="manual">manual</option>
                        <option value="pm2">pm2</option>
                        <option value="systemd">systemd</option>
                        <option value="supervisor">supervisor</option>
                      </select>
                    </div>
                  </div>

                  <div>
                    <label className={lc}>Start command</label>
                    <input
                      type="text"
                      placeholder="php artisan serve --host=0.0.0.0 --port=8000"
                      value={editDraft.start_command ?? ""}
                      onChange={(e) =>
                        setEditDraft((d) => ({ ...d, start_command: e.target.value }))
                      }
                      className={ic}
                    />
                  </div>

                  <div>
                    <label className={lc}>Stop command (optional)</label>
                    <input
                      type="text"
                      placeholder="pm2 stop ecosystem"
                      value={editDraft.stop_command ?? ""}
                      onChange={(e) =>
                        setEditDraft((d) => ({ ...d, stop_command: e.target.value }))
                      }
                      className={ic}
                    />
                  </div>

                  <div>
                    <label className={lc}>Health URL (for internal health checks)</label>
                    <input
                      type="text"
                      placeholder="http://localhost:8000/ or http://host.docker.internal:8000/"
                      value={editDraft.health_url ?? ""}
                      onChange={(e) =>
                        setEditDraft((d) => ({ ...d, health_url: e.target.value }))
                      }
                      className={ic}
                    />
                  </div>
                </div>
              )}

              {editError && <p className="text-xs text-red-400">{editError}</p>}
            </div>

            <div className="mt-5 flex gap-3 justify-end">
              <Dialog.Close asChild>
                <button className="px-4 py-2 rounded-lg text-sm font-medium border border-border bg-background hover:bg-accent text-foreground transition-colors">
                  Cancel
                </button>
              </Dialog.Close>
              <button
                onClick={saveEdit}
                disabled={editSaving}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-primary hover:bg-primary/90 text-primary-foreground transition-colors disabled:opacity-50"
              >
                {editSaving ? "Saving…" : "Save"}
              </button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      {/* ── Delete confirm ─────────────────────────────────────────────── */}
      <Dialog.Root
        open={!!deleteTarget}
        onOpenChange={(o) => {
          if (!o) setDeleteTarget(null);
        }}
      >
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm" />
          <Dialog.Content className="fixed left-1/2 top-1/2 z-50 w-full max-w-sm -translate-x-1/2 -translate-y-1/2 rounded-2xl border border-border bg-card p-6 shadow-2xl">
            <div className="flex items-start gap-4">
              <div className="flex size-10 shrink-0 items-center justify-center rounded-full bg-destructive/10">
                <TrashIcon className="size-5 text-destructive" />
              </div>
              <div>
                <Dialog.Title className="text-base font-semibold text-foreground">
                  Delete project?
                </Dialog.Title>
                <Dialog.Description className="mt-1 text-sm text-muted-foreground">
                  "{deleteTarget?.name}" will be deactivated.
                </Dialog.Description>
              </div>
            </div>
            <div className="mt-6 flex gap-3 justify-end">
              <Dialog.Close asChild>
                <button className="px-4 py-2 rounded-lg text-sm font-medium border border-border bg-background hover:bg-accent text-foreground transition-colors">
                  Cancel
                </button>
              </Dialog.Close>
              <button
                onClick={() => deleteTarget && del(deleteTarget)}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-destructive hover:bg-destructive/90 text-destructive-foreground transition-colors"
              >
                Delete
              </button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </div>
  );
}
