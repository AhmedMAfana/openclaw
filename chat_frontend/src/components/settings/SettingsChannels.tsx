import { useState, useEffect } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { PlusIcon, TrashIcon } from "lucide-react";

interface Binding { channel_id: string; channel_name?: string; project_id: number; project_name: string; provider_type: string; }
interface Project { id: number; name: string; }
interface SlackChannel { id: string; name: string; }

const ic = "w-full px-3 py-2 rounded-lg text-sm bg-secondary border border-border text-foreground focus:outline-none focus:ring-1 focus:ring-ring";
const lc = "block text-sm font-medium text-foreground mb-1";

export function SettingsChannels() {
  const [bindings, setBindings] = useState<Binding[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);
  const [addOpen, setAddOpen] = useState(false);
  const [channelId, setChannelId] = useState("");
  const [channelName, setChannelName] = useState("");
  const [providerType, setProviderType] = useState("slack");
  const [projectId, setProjectId] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState("");
  const [unlinkTarget, setUnlinkTarget] = useState<Binding | null>(null);
  const [slackChannels, setSlackChannels] = useState<SlackChannel[]>([]);
  const [slackChannelsLoading, setSlackChannelsLoading] = useState(false);

  useEffect(() => { loadAll(); }, []);

  async function loadAll() {
    setLoading(true);
    const [br, pr] = await Promise.all([
      fetch("/api/settings/channel-bindings", { credentials: "include" }),
      fetch("/api/settings/projects", { credentials: "include" }),
    ]);
    if (br.ok) setBindings(await br.json());
    if (pr.ok) setProjects(await pr.json());
    setLoading(false);
  }

  async function loadSlackChannels() {
    setSlackChannelsLoading(true);
    try {
      const res = await fetch("/api/settings/slack/channels", { credentials: "include" });
      if (res.ok) {
        const d = await res.json();
        if (d.ok) setSlackChannels(d.channels || []);
      }
    } finally { setSlackChannelsLoading(false); }
  }

  function openAddDialog() {
    setFormError(""); setChannelId(""); setChannelName(""); setProjectId(projects[0]?.id.toString() ?? "");
    setAddOpen(true);
    if (providerType === "slack" && slackChannels.length === 0) loadSlackChannels();
  }

  function handleProviderChange(newProvider: string) {
    setProviderType(newProvider);
    setChannelId(""); setChannelName("");
    if (newProvider === "slack" && slackChannels.length === 0) loadSlackChannels();
  }

  function handleSlackChannelSelect(id: string) {
    setChannelId(id);
    const ch = slackChannels.find((c) => c.id === id);
    if (ch) setChannelName(ch.name);
  }

  async function link() {
    if (!channelId || !projectId) { setFormError("Channel ID and project are required"); return; }
    setSubmitting(true); setFormError("");
    try {
      const res = await fetch("/api/settings/channels", {
        method: "POST", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ channel_id: channelId, channel_name: channelName || channelId, project_id: Number(projectId), provider_type: providerType }),
      });
      if (res.ok) {
        const d = await res.json();
        if (d.binding) setBindings((p) => [...p, d.binding]); else await loadAll();
        setAddOpen(false); setChannelId(""); setChannelName(""); setProjectId("");
      } else { const d = await res.json(); setFormError(d.detail || "Failed"); }
    } finally { setSubmitting(false); }
  }

  async function unlink(b: Binding) {
    const res = await fetch(`/api/settings/channels/${b.provider_type}/${b.channel_id}`, { method: "DELETE", credentials: "include" });
    if (res.status < 300) setBindings((p) => p.filter((x) => !(x.channel_id === b.channel_id && x.provider_type === b.provider_type)));
    setUnlinkTarget(null);
  }

  return (
    <div className="space-y-4 max-w-3xl">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">{bindings.length} binding{bindings.length !== 1 ? "s" : ""}</p>
        <button onClick={openAddDialog}
          className="flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium bg-primary hover:bg-primary/90 text-primary-foreground transition-colors">
          <PlusIcon className="size-4" /> Link Channel
        </button>
      </div>

      {loading ? <div className="text-sm text-muted-foreground py-4">Loading…</div> : bindings.length === 0 ? (
        <div className="rounded-xl border border-border bg-card p-8 text-center text-sm text-muted-foreground">No channel bindings yet.</div>
      ) : (
        <div className="rounded-xl border border-border bg-card overflow-hidden">
          <table className="w-full text-sm">
            <thead><tr className="border-b border-border">
              {["Provider","Channel","Project",""].map((h) => (
                <th key={h} className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider">{h}</th>
              ))}
            </tr></thead>
            <tbody className="divide-y divide-border">
              {bindings.map((b) => (
                <tr key={`${b.provider_type}-${b.channel_id}`} className="hover:bg-accent/30 transition-colors">
                  <td className="px-4 py-3">
                    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${b.provider_type === "slack" ? "bg-purple-500/15 text-purple-400" : "bg-blue-500/15 text-blue-400"}`}>
                      {b.provider_type}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    <span className="text-foreground">{b.channel_name || b.channel_id}</span>
                    {b.channel_name && b.channel_name !== b.channel_id && <span className="text-xs text-muted-foreground ml-2 font-mono">{b.channel_id}</span>}
                  </td>
                  <td className="px-4 py-3 text-foreground">{b.project_name}</td>
                  <td className="px-4 py-3 text-right">
                    <button onClick={() => setUnlinkTarget(b)} className="p-1.5 rounded-lg text-muted-foreground hover:text-destructive hover:bg-destructive/10 transition-colors">
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
          <Dialog.Content className="fixed left-1/2 top-1/2 z-50 w-full max-w-md -translate-x-1/2 -translate-y-1/2 rounded-2xl border border-border bg-card p-6 shadow-2xl">
            <Dialog.Title className="text-base font-semibold text-foreground mb-4">Link Channel to Project</Dialog.Title>
            <div className="space-y-3">
              <div><label className={lc}>Provider</label>
                <select value={providerType} onChange={(e) => handleProviderChange(e.target.value)} className={ic}>
                  <option value="slack">Slack</option>
                  <option value="telegram">Telegram</option>
                </select></div>

              {providerType === "slack" ? (
                <div>
                  <label className={lc}>Channel <span className="text-red-400">*</span></label>
                  {slackChannelsLoading ? (
                    <div className={`${ic} text-muted-foreground`}>Loading channels…</div>
                  ) : slackChannels.length > 0 ? (
                    <select value={channelId} onChange={(e) => handleSlackChannelSelect(e.target.value)} className={ic}>
                      <option value="">Select channel…</option>
                      {slackChannels.map((ch) => (
                        <option key={ch.id} value={ch.id}>#{ch.name}</option>
                      ))}
                    </select>
                  ) : (
                    <>
                      <input type="text" placeholder="C01234ABCDE"
                        value={channelId} onChange={(e) => setChannelId(e.target.value)} className={ic} />
                      <p className="mt-1 text-xs text-muted-foreground">Configure Slack in Chat Platform settings to load channels</p>
                    </>
                  )}
                </div>
              ) : (
                <>
                  <div><label className={lc}>Channel ID <span className="text-red-400">*</span></label>
                    <input type="text" placeholder="-100123456789"
                      value={channelId} onChange={(e) => setChannelId(e.target.value)} className={ic} /></div>
                  <div><label className={lc}>Channel Name (optional)</label>
                    <input type="text" placeholder="My Group"
                      value={channelName} onChange={(e) => setChannelName(e.target.value)} className={ic} /></div>
                </>
              )}

              <div><label className={lc}>Project <span className="text-red-400">*</span></label>
                <select value={projectId} onChange={(e) => setProjectId(e.target.value)} className={ic}>
                  <option value="">Select project…</option>
                  {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                </select></div>
              {formError && <p className="text-xs text-red-400">{formError}</p>}
            </div>
            <div className="mt-5 flex gap-3 justify-end">
              <Dialog.Close asChild>
                <button className="px-4 py-2 rounded-lg text-sm font-medium border border-border bg-background hover:bg-accent text-foreground transition-colors">Cancel</button>
              </Dialog.Close>
              <button onClick={link} disabled={submitting}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-primary hover:bg-primary/90 text-primary-foreground transition-colors disabled:opacity-50">
                {submitting ? "Linking…" : "Link Channel"}
              </button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      <Dialog.Root open={!!unlinkTarget} onOpenChange={(o) => { if (!o) setUnlinkTarget(null); }}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm" />
          <Dialog.Content className="fixed left-1/2 top-1/2 z-50 w-full max-w-sm -translate-x-1/2 -translate-y-1/2 rounded-2xl border border-border bg-card p-6 shadow-2xl">
            <div className="flex items-start gap-4">
              <div className="flex size-10 shrink-0 items-center justify-center rounded-full bg-destructive/10"><TrashIcon className="size-5 text-destructive" /></div>
              <div>
                <Dialog.Title className="text-base font-semibold text-foreground">Unlink channel?</Dialog.Title>
                <Dialog.Description className="mt-1 text-sm text-muted-foreground">
                  {unlinkTarget?.channel_name || unlinkTarget?.channel_id} will be unlinked from {unlinkTarget?.project_name}.
                </Dialog.Description>
              </div>
            </div>
            <div className="mt-6 flex gap-3 justify-end">
              <Dialog.Close asChild>
                <button className="px-4 py-2 rounded-lg text-sm font-medium border border-border bg-background hover:bg-accent text-foreground transition-colors">Cancel</button>
              </Dialog.Close>
              <button onClick={() => unlinkTarget && unlink(unlinkTarget)}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-destructive hover:bg-destructive/90 text-destructive-foreground transition-colors">Unlink</button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </div>
  );
}
