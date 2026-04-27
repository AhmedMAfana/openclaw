import { useState, useEffect, useMemo } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { PlusIcon, TrashIcon, SearchIcon, CheckIcon, ToggleLeftIcon, ToggleRightIcon } from "lucide-react";

interface User { id: number; chat_provider_type: string; chat_provider_uid: string; username: string | null; is_allowed: boolean; is_admin: boolean; }
interface SlackMember { id: string; name: string; real_name: string; avatar: string; already_added: boolean; }

const ic = "w-full px-3 py-2 rounded-lg text-sm bg-secondary border border-border text-foreground focus:outline-none focus:ring-1 focus:ring-ring";
const lc = "block text-sm font-medium text-foreground mb-1";

export function SettingsUsers() {
  const [users, setUsers] = useState<User[]>([]);
  const [hasSlack, setHasSlack] = useState(false);
  const [loading, setLoading] = useState(true);
  const [addOpen, setAddOpen] = useState(false);
  const [activeTab, setActiveTab] = useState<"slack"|"telegram">("telegram");
  const [deleteTarget, setDeleteTarget] = useState<User | null>(null);
  const [slackMembers, setSlackMembers] = useState<SlackMember[]>([]);
  const [slackSearch, setSlackSearch] = useState("");
  const [slackLoading, setSlackLoading] = useState(false);
  const [selectedSlackId, setSelectedSlackId] = useState("");
  const [tgUid, setTgUid] = useState("");
  const [tgUsername, setTgUsername] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState("");

  useEffect(() => { loadAll(); }, []);

  async function loadAll() {
    setLoading(true);
    try {
      const [ur, cr] = await Promise.all([
        fetch("/api/settings/users", { credentials: "include" }),
        fetch("/api/settings/config", { credentials: "include" }),
      ]);
      if (ur.ok) setUsers(await ur.json());
      if (cr.ok) {
        const cfg = await cr.json();
        const slack = Object.keys(cfg).some((k) => k.includes("slack"));
        setHasSlack(slack);
        setActiveTab(slack ? "slack" : "telegram");
      }
    } finally { setLoading(false); }
  }

  async function loadSlackMembers() {
    setSlackLoading(true);
    const res = await fetch("/api/settings/slack/members", { credentials: "include" });
    if (res.ok) { const d = await res.json(); if (d.ok) setSlackMembers(d.members || []); }
    setSlackLoading(false);
  }

  const filtered = useMemo(() =>
    slackMembers.filter((m) => !m.already_added &&
      (m.name.toLowerCase().includes(slackSearch.toLowerCase()) || m.real_name.toLowerCase().includes(slackSearch.toLowerCase()))),
    [slackMembers, slackSearch]);

  async function addUser() {
    setFormError("");
    if (activeTab === "slack" && !selectedSlackId) { setFormError("Select a Slack member"); return; }
    if (activeTab === "telegram" && !tgUid) { setFormError("Telegram user ID is required"); return; }
    setSubmitting(true);
    const payload = activeTab === "slack"
      ? { chat_provider_type: "slack", chat_provider_uid: selectedSlackId }
      : { chat_provider_type: "telegram", chat_provider_uid: tgUid, username: tgUsername || null };
    try {
      const res = await fetch("/api/settings/users", {
        method: "POST", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (res.ok) { const created = await res.json(); setUsers((p) => [...p, created]); setAddOpen(false); setSelectedSlackId(""); setTgUid(""); setTgUsername(""); }
      else { const d = await res.json(); setFormError(d.detail || "Failed"); }
    } finally { setSubmitting(false); }
  }

  async function toggleAllowed(u: User) {
    const res = await fetch(`/api/settings/users/${u.id}/allow`, {
      method: "PATCH", credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ is_allowed: !u.is_allowed }),
    });
    if (res.ok) { const d = await res.json(); setUsers((p) => p.map((x) => x.id === u.id ? {...x, is_allowed: d.is_allowed} : x)); }
  }

  async function deleteUser(u: User) {
    const res = await fetch(`/api/settings/users/${u.id}`, { method: "DELETE", credentials: "include" });
    if (res.ok) setUsers((p) => p.filter((x) => x.id !== u.id));
    setDeleteTarget(null);
  }

  return (
    <div className="space-y-4 max-w-4xl">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">{users.length} user{users.length !== 1 ? "s" : ""}</p>
        <button onClick={() => { setFormError(""); setSelectedSlackId(""); setTgUid(""); setTgUsername(""); setAddOpen(true); if (activeTab === "slack" && slackMembers.length === 0) loadSlackMembers(); }}
          className="flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium bg-primary hover:bg-primary/90 text-primary-foreground transition-colors">
          <PlusIcon className="size-4" /> Add User
        </button>
      </div>

      {loading ? <div className="text-sm text-muted-foreground py-4">Loading…</div> : users.length === 0 ? (
        <div className="rounded-xl border border-border bg-card p-8 text-center text-sm text-muted-foreground">No users yet.</div>
      ) : (
        <div className="rounded-xl border border-border bg-card overflow-hidden">
          <table className="w-full text-sm">
            <thead><tr className="border-b border-border">
              {["Platform","User ID","Username","Access",""].map((h) => (
                <th key={h} className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider">{h}</th>
              ))}
            </tr></thead>
            <tbody className="divide-y divide-border">
              {users.map((u) => (
                <tr key={u.id} className="hover:bg-accent/30 transition-colors">
                  <td className="px-4 py-3">
                    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${u.chat_provider_type === "slack" ? "bg-purple-500/15 text-purple-400" : "bg-blue-500/15 text-blue-400"}`}>
                      {u.chat_provider_type}
                    </span>
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-muted-foreground">{u.chat_provider_uid}</td>
                  <td className="px-4 py-3 text-foreground">{u.username || "—"}</td>
                  <td className="px-4 py-3">
                    <button onClick={() => toggleAllowed(u)} className="flex items-center gap-1.5 text-xs transition-colors">
                      {u.is_allowed
                        ? <><ToggleRightIcon className="size-4 text-green-400" /><span className="text-green-400">Authorized</span></>
                        : <><ToggleLeftIcon className="size-4 text-muted-foreground" /><span className="text-muted-foreground">Disabled</span></>}
                    </button>
                  </td>
                  <td className="px-4 py-3 text-right">
                    <button onClick={() => setDeleteTarget(u)} className="p-1.5 rounded-lg text-muted-foreground hover:text-destructive hover:bg-destructive/10 transition-colors">
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
          <Dialog.Content className="fixed left-1/2 top-1/2 z-50 w-full max-w-md -translate-x-1/2 -translate-y-1/2 rounded-2xl border border-border bg-card p-6 shadow-2xl max-h-[85vh] overflow-y-auto">
            <Dialog.Title className="text-base font-semibold text-foreground mb-4">Add User</Dialog.Title>
            {hasSlack && (
              <div className="flex gap-1 mb-4 p-1 rounded-lg bg-secondary border border-border">
                {(["slack","telegram"] as const).map((tab) => (
                  <button key={tab} onClick={() => { setActiveTab(tab); if (tab === "slack" && slackMembers.length === 0) loadSlackMembers(); }}
                    className={`flex-1 px-3 py-1.5 rounded-md text-sm font-medium transition-colors capitalize ${activeTab === tab ? "bg-card text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground"}`}>
                    {tab}
                  </button>
                ))}
              </div>
            )}
            {activeTab === "slack" && (
              <div className="space-y-3">
                <div className="relative">
                  <SearchIcon className="absolute left-3 top-1/2 -translate-y-1/2 size-4 text-muted-foreground" />
                  <input type="text" placeholder="Search members…" value={slackSearch} onChange={(e) => setSlackSearch(e.target.value)} className={`${ic} pl-9`} />
                </div>
                {slackLoading ? <p className="text-sm text-muted-foreground text-center py-4">Loading…</p> : (
                  <div className="space-y-1 max-h-56 overflow-y-auto">
                    {filtered.length === 0 ? <p className="text-sm text-muted-foreground text-center py-4">No members found</p> :
                      filtered.map((m) => (
                        <button key={m.id} onClick={() => setSelectedSlackId(m.id === selectedSlackId ? "" : m.id)}
                          className={`w-full flex items-center gap-3 px-3 py-2 rounded-lg text-left transition-colors ${selectedSlackId === m.id ? "bg-primary/10 border border-primary/30" : "hover:bg-accent/50"}`}>
                          {m.avatar
                            ? <img src={m.avatar} alt="" className="size-7 rounded-full" />
                            : <div className="size-7 rounded-full bg-secondary flex items-center justify-center text-xs text-muted-foreground">{(m.real_name || m.name)[0]?.toUpperCase()}</div>}
                          <div className="flex-1 min-w-0">
                            <p className="text-sm font-medium text-foreground truncate">{m.real_name || m.name}</p>
                            <p className="text-xs text-muted-foreground">@{m.name}</p>
                          </div>
                          {selectedSlackId === m.id && <CheckIcon className="size-4 text-primary shrink-0" />}
                        </button>
                      ))}
                  </div>
                )}
              </div>
            )}
            {activeTab === "telegram" && (
              <div className="space-y-3">
                <div><label className={lc}>Telegram User ID <span className="text-red-400">*</span></label>
                  <input type="text" placeholder="123456789" value={tgUid} onChange={(e) => setTgUid(e.target.value)} className={ic} /></div>
                <div><label className={lc}>Username (optional)</label>
                  <input type="text" placeholder="@username" value={tgUsername} onChange={(e) => setTgUsername(e.target.value)} className={ic} /></div>
              </div>
            )}
            {formError && <p className="text-xs text-red-400 mt-3">{formError}</p>}
            <div className="mt-5 flex gap-3 justify-end">
              <Dialog.Close asChild>
                <button className="px-4 py-2 rounded-lg text-sm font-medium border border-border bg-background hover:bg-accent text-foreground transition-colors">Cancel</button>
              </Dialog.Close>
              <button onClick={addUser} disabled={submitting}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-primary hover:bg-primary/90 text-primary-foreground transition-colors disabled:opacity-50">
                {submitting ? "Adding…" : "Add User"}
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
                <Dialog.Title className="text-base font-semibold text-foreground">Delete user?</Dialog.Title>
                <Dialog.Description className="mt-1 text-sm text-muted-foreground">{deleteTarget?.username || deleteTarget?.chat_provider_uid} will be removed.</Dialog.Description>
              </div>
            </div>
            <div className="mt-6 flex gap-3 justify-end">
              <Dialog.Close asChild>
                <button className="px-4 py-2 rounded-lg text-sm font-medium border border-border bg-background hover:bg-accent text-foreground transition-colors">Cancel</button>
              </Dialog.Close>
              <button onClick={() => deleteTarget && deleteUser(deleteTarget)}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-destructive hover:bg-destructive/90 text-destructive-foreground transition-colors">Delete</button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </div>
  );
}
