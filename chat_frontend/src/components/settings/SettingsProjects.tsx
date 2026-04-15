import { useState, useEffect } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { PlusIcon, TrashIcon } from "lucide-react";

interface Project {
  id: number;
  name: string;
  github_repo: string;
  default_branch: string;
  tech_stack: string | null;
  is_dockerized: boolean;
}

const ic = "w-full px-3 py-2 rounded-lg text-sm bg-secondary border border-border text-foreground focus:outline-none focus:ring-1 focus:ring-ring";
const lc = "block text-sm font-medium text-foreground mb-1";

const EMPTY = { github_repo: "", name: "", default_branch: "main", tech_stack: "", description: "", is_dockerized: false, docker_compose_file: "docker-compose.yml", app_container_name: "", app_port: "" };

export function SettingsProjects() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);
  const [addOpen, setAddOpen] = useState(false);
  const [form, setForm] = useState(EMPTY);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<Project | null>(null);

  useEffect(() => { load(); }, []);

  async function load() {
    setLoading(true);
    const res = await fetch("/api/settings/projects", { credentials: "include" });
    if (res.ok) setProjects(await res.json());
    setLoading(false);
  }

  async function add() {
    if (!form.github_repo || !form.name) { setFormError("Repository and name are required"); return; }
    setSubmitting(true); setFormError("");
    const payload: Record<string, unknown> = {
      github_repo: form.github_repo, name: form.name,
      default_branch: form.default_branch || "main",
      tech_stack: form.tech_stack || null,
      description: form.description || null,
      is_dockerized: form.is_dockerized,
    };
    if (form.is_dockerized) {
      if (form.docker_compose_file) payload.docker_compose_file = form.docker_compose_file;
      if (form.app_container_name) payload.app_container_name = form.app_container_name;
      if (form.app_port) payload.app_port = Number(form.app_port);
    }
    try {
      const res = await fetch("/api/settings/projects", {
        method: "POST", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (res.ok) { const created = await res.json(); setProjects((p) => [...p, created]); setAddOpen(false); setForm(EMPTY); }
      else { const d = await res.json(); setFormError(d.detail || "Failed"); }
    } finally { setSubmitting(false); }
  }

  async function del(p: Project) {
    const res = await fetch(`/api/settings/projects/${p.id}`, { method: "DELETE", credentials: "include" });
    if (res.ok) setProjects((prev) => prev.filter((x) => x.id !== p.id));
    setDeleteTarget(null);
  }

  return (
    <div className="space-y-4 max-w-4xl">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">{projects.length} active project{projects.length !== 1 ? "s" : ""}</p>
        <button onClick={() => { setForm(EMPTY); setFormError(""); setAddOpen(true); }}
          className="flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium bg-primary hover:bg-primary/90 text-primary-foreground transition-colors">
          <PlusIcon className="size-4" /> Add Project
        </button>
      </div>

      {loading ? <div className="text-sm text-muted-foreground py-4">Loading…</div> : projects.length === 0 ? (
        <div className="rounded-xl border border-border bg-card p-8 text-center text-sm text-muted-foreground">No projects yet.</div>
      ) : (
        <div className="rounded-xl border border-border bg-card overflow-hidden">
          <table className="w-full text-sm">
            <thead><tr className="border-b border-border">
              {["Name","Repository","Branch","Stack","Docker",""].map((h) => (
                <th key={h} className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider">{h}</th>
              ))}
            </tr></thead>
            <tbody className="divide-y divide-border">
              {projects.map((p) => (
                <tr key={p.id} className="hover:bg-accent/30 transition-colors">
                  <td className="px-4 py-3 font-medium text-foreground">{p.name}</td>
                  <td className="px-4 py-3 text-muted-foreground font-mono text-xs">{p.github_repo}</td>
                  <td className="px-4 py-3 text-muted-foreground">{p.default_branch}</td>
                  <td className="px-4 py-3 text-muted-foreground text-xs">{p.tech_stack || "—"}</td>
                  <td className="px-4 py-3 text-xs">{p.is_dockerized ? <span className="text-blue-400">Yes</span> : <span className="text-muted-foreground">No</span>}</td>
                  <td className="px-4 py-3 text-right">
                    <button onClick={() => setDeleteTarget(p)} className="p-1.5 rounded-lg text-muted-foreground hover:text-destructive hover:bg-destructive/10 transition-colors">
                      <TrashIcon className="size-4" />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <Dialog.Root open={addOpen} onOpenChange={setAddOpen}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm" />
          <Dialog.Content className="fixed left-1/2 top-1/2 z-50 w-full max-w-lg -translate-x-1/2 -translate-y-1/2 rounded-2xl border border-border bg-card p-6 shadow-2xl max-h-[90vh] overflow-y-auto">
            <Dialog.Title className="text-base font-semibold text-foreground mb-4">Add Project</Dialog.Title>
            <div className="space-y-3">
              <div className="grid grid-cols-2 gap-3">
                <div><label className={lc}>Repository <span className="text-red-400">*</span></label>
                  <input type="text" placeholder="owner/repo" value={form.github_repo} onChange={(e) => setForm((f) => ({...f, github_repo: e.target.value}))} className={ic} /></div>
                <div><label className={lc}>Project Name <span className="text-red-400">*</span></label>
                  <input type="text" placeholder="My App" value={form.name} onChange={(e) => setForm((f) => ({...f, name: e.target.value}))} className={ic} /></div>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div><label className={lc}>Default Branch</label>
                  <input type="text" placeholder="main" value={form.default_branch} onChange={(e) => setForm((f) => ({...f, default_branch: e.target.value}))} className={ic} /></div>
                <div><label className={lc}>Tech Stack</label>
                  <input type="text" placeholder="Laravel 11, Vue 3" value={form.tech_stack} onChange={(e) => setForm((f) => ({...f, tech_stack: e.target.value}))} className={ic} /></div>
              </div>
              <div><label className={lc}>Description</label>
                <textarea rows={2} placeholder="Optional" value={form.description} onChange={(e) => setForm((f) => ({...f, description: e.target.value}))} className={`${ic} resize-none`} /></div>
              <label className="flex items-center gap-2.5 cursor-pointer">
                <input type="checkbox" checked={form.is_dockerized} onChange={(e) => setForm((f) => ({...f, is_dockerized: e.target.checked}))} className="size-4 rounded" />
                <span className="text-sm text-foreground">Dockerized</span>
              </label>
              {form.is_dockerized && (
                <div className="pl-6 space-y-3 border-l-2 border-border">
                  <div><label className={lc}>Compose File</label>
                    <input type="text" placeholder="docker-compose.yml" value={form.docker_compose_file} onChange={(e) => setForm((f) => ({...f, docker_compose_file: e.target.value}))} className={ic} /></div>
                  <div className="grid grid-cols-2 gap-3">
                    <div><label className={lc}>App Container</label>
                      <input type="text" placeholder="app" value={form.app_container_name} onChange={(e) => setForm((f) => ({...f, app_container_name: e.target.value}))} className={ic} /></div>
                    <div><label className={lc}>App Port</label>
                      <input type="number" placeholder="8000" value={form.app_port} onChange={(e) => setForm((f) => ({...f, app_port: e.target.value}))} className={ic} /></div>
                  </div>
                </div>
              )}
              {formError && <p className="text-xs text-red-400">{formError}</p>}
            </div>
            <div className="mt-5 flex gap-3 justify-end">
              <Dialog.Close asChild>
                <button className="px-4 py-2 rounded-lg text-sm font-medium border border-border bg-background hover:bg-accent text-foreground transition-colors">Cancel</button>
              </Dialog.Close>
              <button onClick={add} disabled={submitting}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-primary hover:bg-primary/90 text-primary-foreground transition-colors disabled:opacity-50">
                {submitting ? "Adding…" : "Add Project"}
              </button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      <Dialog.Root open={!!deleteTarget} onOpenChange={(o) => { if (!o) setDeleteTarget(null); }}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm" />
          <Dialog.Content className="fixed left-1/2 top-1/2 z-50 w-full max-w-sm -translate-x-1/2 -translate-y-1/2 rounded-2xl border border-border bg-card p-6 shadow-2xl">
            <div className="flex items-start gap-4">
              <div className="flex size-10 shrink-0 items-center justify-center rounded-full bg-destructive/10"><TrashIcon className="size-5 text-destructive" /></div>
              <div>
                <Dialog.Title className="text-base font-semibold text-foreground">Delete project?</Dialog.Title>
                <Dialog.Description className="mt-1 text-sm text-muted-foreground">"{deleteTarget?.name}" will be deactivated.</Dialog.Description>
              </div>
            </div>
            <div className="mt-6 flex gap-3 justify-end">
              <Dialog.Close asChild>
                <button className="px-4 py-2 rounded-lg text-sm font-medium border border-border bg-background hover:bg-accent text-foreground transition-colors">Cancel</button>
              </Dialog.Close>
              <button onClick={() => deleteTarget && del(deleteTarget)}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-destructive hover:bg-destructive/90 text-destructive-foreground transition-colors">Delete</button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </div>
  );
}
