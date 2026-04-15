import "./index.css";
import { useState, useCallback, useEffect, useRef, type RefObject } from "react";
import {
  AssistantRuntimeProvider,
  useExternalStoreRuntime,
  type AppendMessage,
} from "@assistant-ui/react";
import * as Dialog from "@radix-ui/react-dialog";
import { Thread } from "@/components/assistant-ui/thread";
import { ThinkingContext } from "@/lib/thinking-context";
import { PlusIcon, SettingsIcon, LogOutIcon, PencilIcon, TrashIcon, CheckIcon, XIcon } from "lucide-react";

// ── Types ────────────────────────────────────────────────────────────────────

interface ChatThread {
  remoteId: string;
  title: string;
}

interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
}

// ── Stream parser ─────────────────────────────────────────────────────────────
// Parses assistant-stream data-stream lines into text deltas and tool events.

async function readStream(
  body: ReadableStream<Uint8Array>,
  onText: (accumulated: string) => void,
  onTool: (tool: string) => void,
): Promise<string> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let accumulated = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    const chunk = decoder.decode(value, { stream: true });
    for (const line of chunk.split("\n")) {
      if (line.startsWith("0:")) {
        try {
          const text: string = JSON.parse(line.slice(2));
          accumulated += text;
          onText(accumulated);
        } catch { /* malformed chunk */ }
      } else if (line.startsWith("2:")) {
        try {
          const events = JSON.parse(line.slice(2)) as Array<{ type?: string; tool?: string }>;
          for (const evt of events) {
            if (evt.type === "tool_use" && evt.tool) onTool(evt.tool);
          }
        } catch { /* malformed data line */ }
      }
    }
  }
  return accumulated;
}

// ── App ──────────────────────────────────────────────────────────────────────

export default function App() {
  const [threads, setThreads] = useState<ChatThread[]>([]);
  const [activeThreadId, setActiveThreadId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isRunning, setIsRunning] = useState(false);
  const [sidebarLoading, setSidebarLoading] = useState(true);
  const [thinkingSteps, setThinkingSteps] = useState<string[]>([]);

  // Rename state
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const renameInputRef = useRef<HTMLInputElement>(null);

  // Delete confirmation modal
  const [deleteTarget, setDeleteTarget] = useState<ChatThread | null>(null);

  useEffect(() => {
    loadThreads();
  }, []);

  useEffect(() => {
    if (renamingId && renameInputRef.current) {
      renameInputRef.current.focus();
      renameInputRef.current.select();
    }
  }, [renamingId]);

  async function loadThreads() {
    setSidebarLoading(true);
    try {
      const res = await fetch("/api/threads", { credentials: "include" });
      if (res.status === 401) { window.location.href = "/chat/login"; return; }
      if (res.ok) {
        const data = await res.json();
        const list: ChatThread[] = data.threads ?? [];
        setThreads(list);
        if (list.length > 0) await selectThread(list[0].remoteId);
      }
    } catch { /* network error */ }
    finally { setSidebarLoading(false); }
  }

  async function selectThread(id: string) {
    setActiveThreadId(id);
    setMessages([]);
    setThinkingSteps([]);
    try {
      const res = await fetch(`/api/threads/${id}/messages`, { credentials: "include" });
      if (res.ok) {
        const data = await res.json();
        setMessages(
          (data.messages ?? []).map((m: { id: string; role: string; content: string }) => ({
            id: m.id,
            role: m.role as "user" | "assistant",
            content: m.content,
          }))
        );
      }
    } catch { /* silently fail */ }
  }

  async function newThread() {
    try {
      const res = await fetch("/api/threads", { method: "POST", credentials: "include" });
      if (res.ok) {
        const t = await res.json();
        const thread: ChatThread = { remoteId: t.remoteId, title: "New Chat" };
        setThreads((prev) => [thread, ...prev]);
        setActiveThreadId(thread.remoteId);
        setMessages([]);
        setThinkingSteps([]);
      }
    } catch { /* silently fail */ }
  }

  async function persistTitle(threadId: string, title: string) {
    try {
      await fetch(`/api/threads/${threadId}/rename`, {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title }),
      });
    } catch { /* best-effort */ }
  }

  async function deleteThread(threadId: string) {
    try {
      await fetch(`/api/threads/${threadId}/archive`, {
        method: "POST",
        credentials: "include",
      });
      setDeleteTarget(null);
      setThreads((prev) => prev.filter((t) => t.remoteId !== threadId));
      if (activeThreadId === threadId) {
        const remaining = threads.filter((t) => t.remoteId !== threadId);
        if (remaining.length > 0) await selectThread(remaining[0].remoteId);
        else { setActiveThreadId(null); setMessages([]); }
      }
    } catch { /* silently fail */ }
  }

  function startRename(thread: ChatThread) {
    setRenamingId(thread.remoteId);
    setRenameValue(thread.title);
  }

  async function commitRename(threadId: string) {
    const title = renameValue.trim() || "New Chat";
    setThreads((prev) => prev.map((t) => t.remoteId === threadId ? { ...t, title } : t));
    setRenamingId(null);
    await persistTitle(threadId, title);
  }

  // ── Shared streaming helper ─────────────────────────────────────────────────

  async function streamAssistant(
    asstMsgId: string,
    body: object,
  ) {
    const res = await fetch("/api/assistant", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);

    await readStream(
      res.body,
      (accumulated) => {
        setMessages((prev) =>
          prev.map((m) => m.id === asstMsgId ? { ...m, content: accumulated } : m)
        );
      },
      (tool) => setThinkingSteps((prev) => [...prev, tool]),
    );
  }

  // ── onNew (send message) ─────────────────────────────────────────────────────

  const onNew = useCallback(
    async (message: AppendMessage) => {
      const userText = message.content
        .filter((p) => p.type === "text")
        .map((p) => (p as { type: "text"; text: string }).text)
        .join(" ")
        .trim();

      if (!userText) return;

      const userMsgId = `user-${Date.now()}`;
      const asstMsgId = `asst-${Date.now() + 1}`;

      setThinkingSteps([]);
      setMessages((prev) => [
        ...prev,
        { id: userMsgId, role: "user", content: userText },
        { id: asstMsgId, role: "assistant", content: "" },
      ]);
      setIsRunning(true);

      // Capture current thread id and whether title needs setting
      const threadId = activeThreadId;
      const currentTitle = threads.find((t) => t.remoteId === threadId)?.title ?? "";
      const isFirstMessage = currentTitle === "New Chat";

      try {
        await streamAssistant(asstMsgId, {
          commands: [{
            type: "add-message",
            message: { role: "user", parts: [{ type: "text", text: userText }] },
          }],
          threadId,
          mode: "quick",
        });

        // Auto-title: persist on first message only
        if (isFirstMessage && threadId) {
          const newTitle = userText.slice(0, 60).trim();
          setThreads((prev) =>
            prev.map((t) => t.remoteId === threadId ? { ...t, title: newTitle } : t)
          );
          await persistTitle(threadId, newTitle);
        }
      } catch (e: unknown) {
        const errMsg = e instanceof Error ? e.message : String(e);
        setMessages((prev) =>
          prev.map((m) => m.id === asstMsgId ? { ...m, content: `Error: ${errMsg}` } : m)
        );
      } finally {
        setIsRunning(false);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [activeThreadId, threads]
  );

  // ── onReload (retry) — keeps user message, replaces only failed assistant msg ─

  const onReload = useCallback(async () => {
    if (isRunning || !activeThreadId) return;

    const lastAsst = [...messages].reverse().find((m) => m.role === "assistant");
    const lastUser = [...messages].reverse().find((m) => m.role === "user");
    if (!lastUser || !lastAsst) return;

    const newAsstId = `asst-retry-${Date.now()}`;

    // Keep user message in place, swap assistant message with fresh placeholder
    setMessages((prev) => {
      const without = prev.filter((m) => m.id !== lastAsst.id);
      return [...without, { id: newAsstId, role: "assistant", content: "" }];
    });
    setThinkingSteps([]);
    setIsRunning(true);

    try {
      await streamAssistant(newAsstId, {
        commands: [{
          type: "add-message",
          message: { role: "user", parts: [{ type: "text", text: lastUser.content }] },
        }],
        threadId: activeThreadId,
        mode: "quick",
        retry: true,  // backend deletes old assistant msg and skips saving new user msg
      });
    } catch (e: unknown) {
      const errMsg = e instanceof Error ? e.message : String(e);
      setMessages((prev) =>
        prev.map((m) => m.id === newAsstId ? { ...m, content: `Error: ${errMsg}` } : m)
      );
    } finally {
      setIsRunning(false);
    }
  }, [activeThreadId, isRunning, messages]);

  // ── Runtime ──────────────────────────────────────────────────────────────────

  const runtime = useExternalStoreRuntime<ChatMessage>({
    messages,
    isRunning,
    onNew,
    onReload,
    convertMessage: (msg) => ({
      id: msg.id,
      role: msg.role,
      content: [{ type: "text" as const, text: msg.content }],
    }),
  });

  // ── Render ───────────────────────────────────────────────────────────────────

  return (
    <div className="h-screen flex overflow-hidden bg-background">
      {/* Sidebar */}
      <aside className="w-[260px] shrink-0 flex flex-col border-r border-border bg-card">
        {/* Header */}
        <div className="px-4 pt-5 pb-3">
          <div className="flex items-center gap-2.5 mb-4">
            <div className="size-7 rounded-lg flex items-center justify-center text-xs font-bold bg-primary text-primary-foreground">
              AI
            </div>
            <span className="font-semibold text-sm text-foreground">DevOps</span>
          </div>
          <button
            onClick={newThread}
            className="w-full flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium transition-colors bg-secondary hover:bg-accent text-secondary-foreground border border-border"
          >
            <PlusIcon className="size-4" />
            New Chat
          </button>
        </div>

        {/* Thread list */}
        <div className="flex-1 overflow-y-auto px-2 pb-2">
          <p className="px-2 py-1.5 text-xs font-medium uppercase tracking-wider text-muted-foreground">
            Recent
          </p>
          {sidebarLoading ? (
            <div className="px-3 py-2 text-sm text-muted-foreground">Loading...</div>
          ) : threads.length === 0 ? (
            <div className="px-3 py-2 text-sm text-muted-foreground">No chats yet</div>
          ) : (
            threads.map((t) => (
              <ThreadItem
                key={t.remoteId}
                thread={t}
                isActive={t.remoteId === activeThreadId}
                isRenaming={renamingId === t.remoteId}
                renameValue={renamingId === t.remoteId ? renameValue : ""}
                renameInputRef={renamingId === t.remoteId ? renameInputRef : undefined}
                onSelect={() => { if (renamingId !== t.remoteId) selectThread(t.remoteId); }}
                onStartRename={() => startRename(t)}
                onRenameChange={setRenameValue}
                onRenameCommit={() => commitRename(t.remoteId)}
                onRenameCancel={() => setRenamingId(null)}
                onDelete={() => setDeleteTarget(t)}
              />
            ))
          )}
        </div>

        {/* Footer */}
        <div className="px-2 py-3 border-t border-border flex flex-col gap-0.5">
          <a
            href="/settings"
            className="flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
          >
            <SettingsIcon className="size-4" />
            Settings
          </a>
          <a
            href="/chat/logout"
            className="flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
          >
            <LogOutIcon className="size-4" />
            Logout
          </a>
        </div>
      </aside>

      {/* Chat area */}
      <main className="flex-1 overflow-hidden bg-background">
        <ThinkingContext.Provider value={{ steps: thinkingSteps }}>
          <AssistantRuntimeProvider runtime={runtime}>
            <Thread />
          </AssistantRuntimeProvider>
        </ThinkingContext.Provider>
      </main>

      {/* Delete confirmation modal */}
      <Dialog.Root open={!!deleteTarget} onOpenChange={(open) => { if (!open) setDeleteTarget(null); }}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0" />
          <Dialog.Content className="fixed left-1/2 top-1/2 z-50 w-full max-w-sm -translate-x-1/2 -translate-y-1/2 rounded-2xl border border-border bg-card p-6 shadow-2xl data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95">
            <div className="flex items-start gap-4">
              <div className="flex size-10 shrink-0 items-center justify-center rounded-full bg-destructive/10">
                <TrashIcon className="size-5 text-destructive" />
              </div>
              <div className="flex-1 min-w-0">
                <Dialog.Title className="text-base font-semibold text-foreground">
                  Delete chat?
                </Dialog.Title>
                <Dialog.Description className="mt-1 text-sm text-muted-foreground line-clamp-2">
                  "{deleteTarget?.title}" will be permanently deleted.
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
                onClick={() => deleteTarget && deleteThread(deleteTarget.remoteId)}
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

// ── ThreadItem ────────────────────────────────────────────────────────────────

interface ThreadItemProps {
  thread: ChatThread;
  isActive: boolean;
  isRenaming: boolean;
  renameValue: string;
  renameInputRef?: RefObject<HTMLInputElement | null>;
  onSelect: () => void;
  onStartRename: () => void;
  onRenameChange: (v: string) => void;
  onRenameCommit: () => void;
  onRenameCancel: () => void;
  onDelete: () => void;
}

function ThreadItem({
  thread,
  isActive,
  isRenaming,
  renameValue,
  renameInputRef,
  onSelect,
  onStartRename,
  onRenameChange,
  onRenameCommit,
  onRenameCancel,
  onDelete,
}: ThreadItemProps) {
  const [hovered, setHovered] = useState(false);

  if (isRenaming) {
    return (
      <div className="flex items-center gap-1 px-2 py-1 rounded-lg bg-accent">
        <input
          ref={renameInputRef}
          value={renameValue}
          onChange={(e) => onRenameChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") onRenameCommit();
            if (e.key === "Escape") onRenameCancel();
          }}
          className="flex-1 min-w-0 bg-transparent text-sm text-foreground outline-none"
        />
        <button
          onClick={onRenameCommit}
          className="shrink-0 p-1 rounded text-muted-foreground hover:text-foreground"
          title="Save"
        >
          <CheckIcon className="size-3" />
        </button>
        <button
          onClick={onRenameCancel}
          className="shrink-0 p-1 rounded text-muted-foreground hover:text-foreground"
          title="Cancel"
        >
          <XIcon className="size-3" />
        </button>
      </div>
    );
  }

  return (
    <div
      className={`group relative flex items-center rounded-lg transition-colors ${
        isActive ? "bg-accent text-foreground" : "text-muted-foreground hover:bg-accent/50 hover:text-foreground"
      }`}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      <button
        onClick={onSelect}
        className="flex-1 text-left px-3 py-2 text-sm truncate min-w-0"
      >
        {thread.title}
      </button>

      {/* Hover actions */}
      {(hovered || isActive) && (
        <div className="flex items-center gap-0.5 pr-1.5 shrink-0">
          <button
            onClick={(e) => { e.stopPropagation(); onStartRename(); }}
            className="p-1 rounded hover:bg-muted/60 text-muted-foreground hover:text-foreground transition-colors"
            title="Rename"
          >
            <PencilIcon className="size-3" />
          </button>
          <button
            onClick={(e) => { e.stopPropagation(); onDelete(); }}
            className="p-1 rounded hover:bg-destructive/20 text-muted-foreground hover:text-destructive transition-colors"
            title="Delete"
          >
            <TrashIcon className="size-3" />
          </button>
        </div>
      )}
    </div>
  );
}
