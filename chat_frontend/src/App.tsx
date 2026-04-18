import "./index.css";
import { Component, useState, useCallback, useEffect, useRef, type ReactNode, type RefObject } from "react";

// ── Error Boundary (catches React render crashes in the Thread) ───────────────

class ThreadErrorBoundary extends Component<
  { children: ReactNode },
  { crashed: boolean }
> {
  constructor(props: { children: ReactNode }) {
    super(props);
    this.state = { crashed: false };
  }

  static getDerivedStateFromError() {
    return { crashed: true };
  }

  componentDidCatch(error: Error, info: { componentStack: string }) {
    localStorage.removeItem("openclow_thread");
    console.error("[ThreadBoundary] crash:", error.message);
    console.error("[ThreadBoundary] component stack:", info.componentStack);
  }

  render() {
    if (this.state.crashed) {
      return (
        <div className="flex h-full items-center justify-center bg-background">
          <div className="text-center space-y-3 max-w-xs px-6">
            <p className="text-sm text-muted-foreground">
              Couldn't render this conversation. Select another chat from the sidebar or start a new one.
            </p>
            <button
              onClick={() => window.location.reload()}
              className="text-xs text-muted-foreground underline hover:text-foreground"
            >
              Reload page
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
import {
  AssistantRuntimeProvider,
  useExternalStoreRuntime,
  type AppendMessage,
  SimpleImageAttachmentAdapter,
  SimpleTextAttachmentAdapter,
  CompositeAttachmentAdapter,
} from "@assistant-ui/react";
import * as Dialog from "@radix-ui/react-dialog";
import { Thread } from "@/components/assistant-ui/thread";
import { ThinkingContext } from "@/lib/thinking-context";
import { TaskModeContext } from "@/lib/task-mode-context";
import { PlusIcon, SettingsIcon, LogOutIcon, PencilIcon, TrashIcon, CheckIcon, XIcon, ShieldIcon, AlertCircleIcon } from "lucide-react";
import { AccessPanel } from "@/components/AccessPanel";
import { SettingsPanel } from "@/components/SettingsPanel"; // admin-only settings

// ── Types ────────────────────────────────────────────────────────────────────

interface ChatThread {
  remoteId: string;
  title: string;
  projectId?: number | null;
  lastMessageAt?: string; // ISO string from API
}

interface Project {
  id: number;
  name: string;
  techStack?: string | null;
}

interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  createdAt?: string; // ISO string
}

// ── Stream reader ─────────────────────────────────────────────────────────────

async function readStream(
  body: ReadableStream<Uint8Array>,
  signal: AbortSignal,
  onText: (accumulated: string) => void,
  onTool: (tool: string) => void,
  onId?: (id: string) => void,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let accumulated = "";
  let leftover = "";

  try {
    while (true) {
      if (signal.aborted) break;
      const { done, value } = await reader.read();
      if (done) break;

      const chunk = leftover + decoder.decode(value, { stream: true });
      const lines = chunk.split("\n");
      // Last element might be incomplete — carry it over
      leftover = lines.pop() ?? "";

      for (const line of lines) {
        if (signal.aborted) break;
        if (line.startsWith("0:")) {
          try {
            const text: string = JSON.parse(line.slice(2));
            accumulated += text;
            onText(accumulated);
          } catch { /* malformed */ }
        } else if (line.startsWith("2:")) {
          try {
            const events = JSON.parse(line.slice(2)) as Array<{ type?: string; tool?: string; id?: string }>;
            for (const evt of events) {
              if (evt.type === "tool_use" && evt.tool) onTool(evt.tool);
              if (evt.type === "message_id" && evt.id) onId?.(evt.id);
            }
          } catch { /* malformed */ }
        }
      }
    }
  } finally {
    reader.cancel().catch(() => {});
  }
}

// ── App ──────────────────────────────────────────────────────────────────────

export default function App() {
  const [threads, setThreads] = useState<ChatThread[]>([]);
  const [activeThreadId, setActiveThreadId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isRunning, setIsRunning] = useState(false);
  const [sidebarLoading, setSidebarLoading] = useState(true);
  const [thinkingSteps, setThinkingSteps] = useState<string[]>([]);

  // Projects
  const [projects, setProjects] = useState<Project[]>([]);
  const [activeProjectId, setActiveProjectId] = useState<number | null>(null);

  // Current user id (for WebSocket URL)
  const [myUserId, setMyUserId] = useState<number | null>(null);
  const [isAdmin, setIsAdmin] = useState(false);
  const [showAccessPanel, setShowAccessPanel] = useState(false);
  const [showSettingsPanel, setShowSettingsPanel] = useState(false);

  // WebSocket for worker progress events
  const wsRef = useRef<WebSocket | null>(null);
  // Set to true when the WS was intentionally closed (thread switch / unmount)
  // Prevents the onclose reconnect loop from re-opening a dead session
  const wsIntentionalClose = useRef(false);

  // Rename state
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const renameInputRef = useRef<HTMLInputElement>(null);

  // Delete modal
  const [deleteTarget, setDeleteTarget] = useState<ChatThread | null>(null);

  // Task mode toggle: quick = skip planning, plan = generate plan for approval
  const [taskMode, setTaskMode] = useState<"quick" | "plan">("quick");

  // Pending plan approval (set when worker sends a plan_preview event)
  const [pendingPlan, setPendingPlan] = useState<{ taskId: string; messageId: string } | null>(null);

  // Abort controller for in-progress streams
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => { loadThreads(); loadProjects(); loadMe(); }, []);

  // Open worker-progress WebSocket whenever the active thread changes
  useEffect(() => {
    if (!myUserId || !activeThreadId) return;
    wsIntentionalClose.current = false;
    openWorkerSocket(myUserId, activeThreadId);
    return () => {
      wsIntentionalClose.current = true;
      if (wsRef.current) { wsRef.current.close(); wsRef.current = null; }
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [myUserId, activeThreadId]);

  // Persist active thread to localStorage so reload restores it
  useEffect(() => {
    if (activeThreadId) localStorage.setItem("openclow_thread", activeThreadId);
  }, [activeThreadId]);

  useEffect(() => {
    if (renamingId && renameInputRef.current) {
      renameInputRef.current.focus();
      renameInputRef.current.select();
    }
  }, [renamingId]);

  async function approvePlan(taskId: string) {
    const chatId = `web:${myUserId}:${activeThreadId}`;
    setPendingPlan(null);
    try {
      await fetch("/api/web-action", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action_id: `approve_plan:${taskId}`, chat_id: chatId, message_id: "" }),
      });
    } catch { /* best-effort */ }
  }

  async function rejectPlan(taskId: string) {
    const chatId = `web:${myUserId}:${activeThreadId}`;
    setPendingPlan(null);
    try {
      await fetch("/api/web-action", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action_id: `reject_plan:${taskId}`, chat_id: chatId, message_id: "" }),
      });
    } catch { /* best-effort */ }
  }

  // Cancel any in-flight stream
  function cancelStream() {
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
    setIsRunning(false);
    setThinkingSteps([]);
    // Drop any __LOADING__ stubs from message state immediately — they're placeholders
    // that the worker never finished populating, and we don't want them showing as blank rows
    setMessages((prev) => prev.filter((m) => m.content !== "__LOADING__" && m.content.trim() !== ""));
    // Also abort any arq worker jobs enqueued during this session
    if (activeThreadId) {
      fetch(`/api/threads/${activeThreadId}/cancel`, {
        method: "POST",
        credentials: "include",
      }).catch(() => {});
    }
  }

  async function loadMe() {
    try {
      const res = await fetch("/api/me", { credentials: "include" });
      if (res.ok) {
        const data = await res.json();
        setMyUserId(data.id ?? null);
        setIsAdmin(data.is_admin ?? false);
      }
    } catch { /* non-critical */ }
  }

  function scrollThreadToBottom(delay = 50) {
    // Give React one tick to commit the new message, then smooth-scroll to bottom
    setTimeout(() => {
      const viewport = document.querySelector(".aui-thread-viewport");
      if (viewport) viewport.scrollTo({ top: viewport.scrollHeight, behavior: "smooth" });
    }, delay);
  }

  // Sticky-scroll helper: if the viewport is already at (or near) the bottom,
  // keep it there. Called on every RAF frame during streaming so new content
  // is always visible without interrupting manual upward scroll.
  function stickyScrollToBottom() {
    const viewport = document.querySelector(".aui-thread-viewport");
    if (!viewport) return;
    const distFromBottom = viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight;
    if (distFromBottom < 120) viewport.scrollTop = viewport.scrollHeight;
  }

  function openWorkerSocket(userId: number, sessionId: string) {
    if (wsRef.current) { wsRef.current.close(); wsRef.current = null; }
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/api/ws/${userId}/${sessionId}`);

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data) as {
          type: string;
          text?: string;
          message_id?: string;
          task_id?: string;
          card?: unknown;
        };
        const text = data.text ?? "";
        const msgKey = data.message_id
          ? `worker-msg-${data.message_id}`
          : `worker-${sessionId}`;

        // plainId: the bare numeric DB message id — needed to deduplicate against
        // DB-loaded messages (id="123") vs WS-created messages (id="worker-msg-123")
        const plainId = data.message_id ?? null;
        const matchesMsg = (id: string) => id === msgKey || (plainId !== null && id === plainId);

        if (data.type === "agent_token" && data.text) {
          // Append agent text to the progress card's live stream_buffer
          setMessages((prev) =>
            prev.map((m) => {
              if (!matchesMsg(m.id)) return m;
              if (!m.content.startsWith("__PROGRESS_CARD__")) return m;
              try {
                const card = JSON.parse(m.content.slice("__PROGRESS_CARD__".length));
                const updated = { ...card, stream_buffer: (card.stream_buffer ?? "") + data.text };
                return { ...m, content: `__PROGRESS_CARD__${JSON.stringify(updated)}` };
              } catch { return m; }
            })
          );
        } else if (data.type === "progress_card" && data.card) {
          // Inject session_id so WorkerProgressCard can render a Stop button.
          // Preserve stream_buffer accumulated by agent_token events.
          const cardWithSession: Record<string, unknown> = { ...(data.card as Record<string, unknown>), session_id: sessionId };
          setMessages((prev) => {
            const existing = prev.find((m) => matchesMsg(m.id));
            // Carry over stream_buffer so progress_card updates don't wipe the log
            if (existing?.content.startsWith("__PROGRESS_CARD__")) {
              try {
                const old = JSON.parse(existing.content.slice("__PROGRESS_CARD__".length));
                if (old.stream_buffer) cardWithSession.stream_buffer = old.stream_buffer;
              } catch { /* ignore */ }
            }
            const content = `__PROGRESS_CARD__${JSON.stringify(cardWithSession)}`;
            if (existing) return prev.map((m) => matchesMsg(m.id) ? { ...m, id: msgKey, content } : m);
            // New card message — scroll after render
            scrollThreadToBottom();
            return [...prev, { id: msgKey, role: "assistant" as const, content }];
          });
        } else if (data.type === "plan_preview") {
          setMessages((prev) => {
            const exists = prev.find((m) => matchesMsg(m.id));
            if (exists) return prev.map((m) => matchesMsg(m.id) ? { ...m, id: msgKey, content: text } : m);
            scrollThreadToBottom();
            return [...prev, { id: msgKey, role: "assistant" as const, content: text }];
          });
          if (data.task_id) {
            setPendingPlan({ taskId: data.task_id, messageId: data.message_id ?? "" });
          }
        } else if (
          data.type === "msg_new" ||
          data.type === "msg_update" ||
          data.type === "msg_final" ||
          data.type === "msg_error" ||
          data.type === "diff_preview"
        ) {
          // All message events use the same msgKey so updates reconcile with the initial new message.
          // (Old code used worker-new-${Date.now()} for msg_new which could never be found by msg_update.)
          setMessages((prev) => {
            const exists = prev.find((m) => matchesMsg(m.id));
            if (exists) return prev.map((m) => matchesMsg(m.id) ? { ...m, id: msgKey, content: text } : m);
            scrollThreadToBottom();
            return [...prev, { id: msgKey, role: "assistant" as const, content: text }];
          });
        }
      } catch { /* ignore malformed */ }
    };

    ws.onerror = () => { /* onclose handles cleanup */ };

    // Reconnect automatically if the connection drops unexpectedly
    ws.onclose = () => {
      if (wsRef.current !== ws) return;  // intentional close — don't reconnect
      wsRef.current = null;
      if (!wsIntentionalClose.current) {
        setTimeout(() => openWorkerSocket(userId, sessionId), 3000);
      }
    };

    wsRef.current = ws;
  }

  async function loadProjects() {
    try {
      const res = await fetch("/api/projects", { credentials: "include" });
      if (res.ok) {
        const data = await res.json();
        setProjects(data.projects ?? []);
      }
    } catch { /* non-critical */ }
  }

  async function loadThreads() {
    setSidebarLoading(true);
    try {
      const res = await fetch("/api/threads", { credentials: "include" });
      if (res.status === 401) { window.location.href = "/chat/login"; return; }
      if (res.ok) {
        const data = await res.json();
        const list: ChatThread[] = data.threads ?? [];
        setThreads(list);

        if (list.length > 0) {
          // Restore last active thread from localStorage, fallback to newest
          const saved = localStorage.getItem("openclow_thread");
          const target = (saved && list.find((t) => t.remoteId === saved))
            ? saved
            : list[0].remoteId;
          await selectThread(target, list);
        }
      }
    } catch { /* network error */ }
    finally { setSidebarLoading(false); }
  }

  async function selectProject(projectId: number | null, threadId: string | null) {
    setActiveProjectId(projectId);
    if (!threadId) return;
    try {
      await fetch(`/api/threads/${threadId}/project`, {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: projectId }),
      });
      // Update local thread list to reflect new project
      setThreads((prev) => prev.map((t) =>
        t.remoteId === threadId ? { ...t, projectId } : t
      ));
    } catch { /* best-effort */ }
  }

  async function selectThread(id: string, fromList?: ChatThread[]) {
    if (id === activeThreadId) return; // already on this thread
    cancelStream();
    setPendingPlan(null);
    setActiveThreadId(id);
    setMessages([]);
    // Restore project context — use fromList if provided (avoids stale threads state)
    const lookup = fromList ?? threads;
    const thread = lookup.find((t) => t.remoteId === id);
    if (thread !== undefined) setActiveProjectId(thread.projectId ?? null);
    try {
      const res = await fetch(`/api/threads/${id}/messages`, { credentials: "include" });
      if (res.ok) {
        const data = await res.json();
        setMessages(
          (data.messages ?? [])
            // Filter out truly empty assistant stubs (content="" or whitespace-only).
            // __LOADING__ stubs are kept — they show a spinner until the progress card heartbeat
            // overwrites the DB content, and they give WebSocket events a message slot to update.
            .filter((m: { role: string; content: string }) =>
              m.role !== "assistant" || m.content.trim() !== ""
            )
            // Deduplicate by id — DB load vs WebSocket can both create an entry for the same message_id
            .filter((m: { id: string }, i: number, arr: { id: string }[]) =>
              arr.findIndex((x) => x.id === m.id) === i
            )
            .map((m: { id: string; role: string; content: string; createdAt?: string }) => ({
              id: m.id,
              role: m.role as "user" | "assistant",
              content: m.content,
              createdAt: m.createdAt,
            }))
        );
        // Scroll to the end of the conversation — messages may be many and
        // React needs time to render them all before scrollHeight is accurate.
        scrollThreadToBottom(200);
      }
    } catch { /* silently fail */ }
  }

  async function newThread() {
    cancelStream();
    setPendingPlan(null);
    try {
      const res = await fetch("/api/threads", { method: "POST", credentials: "include" });
      if (res.ok) {
        const t = await res.json();
        const thread: ChatThread = { remoteId: t.remoteId, title: "New Chat" };
        setThreads((prev) => [thread, ...prev]);
        setMessages([]);
        setActiveProjectId(null);
        // Use a small delay to ensure the thread is in state before setting active
        // (avoids the selectThread guard check `if (id === activeThreadId) return`)
        setActiveThreadId(null);
        setTimeout(() => setActiveThreadId(thread.remoteId), 0);
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
    if (activeThreadId === threadId) cancelStream();
    try {
      await fetch(`/api/threads/${threadId}/archive`, { method: "POST", credentials: "include" });
      setDeleteTarget(null);
      setThreads((prev) => prev.filter((t) => t.remoteId !== threadId));
      if (activeThreadId === threadId) {
        const remaining = threads.filter((t) => t.remoteId !== threadId);
        if (remaining.length > 0) {
          // selectThread skips if same id; force it by clearing active first
          setActiveThreadId(null);
          await selectThread(remaining[0].remoteId);
        } else {
          setActiveThreadId(null);
          setMessages([]);
        }
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

  // ── Core stream helper ────────────────────────────────────────────────────────

  async function runStream(asstMsgId: string, body: object): Promise<void> {
    const abort = new AbortController();
    abortRef.current = abort;

    const res = await fetch("/api/assistant", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: abort.signal,
    });

    if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);

    // Animation queue: network text accumulates in a plain ref,
    // RAF loop drains it at ~8 chars/frame → smooth character-by-character display
    const pendingRef = { current: "" };   // full received text (updated by network)
    const displayedRef = { current: 0 };  // how many chars are currently shown
    // Tracks the live message id — starts as asstMsgId, updated when server sends the real DB id.
    // RAF and finally must use this ref so they still find the message after the rename.
    const liveIdRef = { current: asstMsgId };
    let rafId: number | null = null;
    let streamDone = false;

    function animateTick() {
      const full = pendingRef.current;
      const shown = displayedRef.current;
      if (shown < full.length) {
        const next = Math.min(full.length, shown + 8);
        displayedRef.current = next;
        setMessages((prev) =>
          prev.map((m) => m.id === liveIdRef.current ? { ...m, content: full.slice(0, next) } : m)
        );
        // Sticky-scroll: if the user hasn't scrolled up, keep the bottom in view
        stickyScrollToBottom();
      }
      if (!streamDone || displayedRef.current < pendingRef.current.length) {
        rafId = requestAnimationFrame(animateTick);
      }
    }
    rafId = requestAnimationFrame(animateTick);

    try {
      await readStream(
        res.body,
        abort.signal,
        (accumulated) => { pendingRef.current = accumulated; }, // ref only — no setState per chunk
        (tool) => setThinkingSteps((prev) => [...prev, tool]),
        // Reconcile temp ID with the real DB message ID so refresh shows the right message.
        // Also update liveIdRef so RAF/finally keep finding the message after rename.
        (realId) => {
          liveIdRef.current = realId;
          setMessages((prev) =>
            prev.map((m) => m.id === asstMsgId ? { ...m, id: realId } : m)
          );
        },
      );
    } finally {
      streamDone = true;
      // Cancel RAF and snap immediately to full received text
      if (rafId !== null) cancelAnimationFrame(rafId);
      setMessages((prev) =>
        prev.map((m) => m.id === liveIdRef.current ? { ...m, content: pendingRef.current } : m)
      );
    }
  }

  // ── onNew ─────────────────────────────────────────────────────────────────────

  const onNew = useCallback(async (message: AppendMessage) => {
    const userText = message.content
      .filter((p) => p.type === "text")
      .map((p: { type: string; text?: string }) => p.text ?? "")
      .join(" ").trim();

    // Extract attachments provided by assistant-ui's attachment adapter
    const msgAttachments = (message as AppendMessage & { attachments?: Array<{ name: string; contentType?: string; content?: Array<{ type: string; image?: string; text?: string }> }> }).attachments ?? [];
    if (!userText && msgAttachments.length === 0) return;

    // Build backend attachment payload from CompleteAttachment content parts
    const backendAttachments = msgAttachments.flatMap((att) =>
      (att.content ?? []).flatMap((part) => {
        if (part.type === "image" && part.image) {
          // data URL: "data:image/png;base64,<data>"
          const [header, data] = part.image.split(",");
          const mediaType = header.replace("data:", "").replace(";base64", "");
          return [{ name: att.name, mediaType, data }];
        }
        // text parts (txt/md) are merged into the prompt text below
        return [];
      })
    );

    // Merge text file content inline into the prompt
    const inlineTextParts = msgAttachments.flatMap((att) =>
      (att.content ?? [])
        .filter((p): p is { type: "text"; text: string } => p.type === "text" && "text" in p)
        .map((p) => p.text)
    );
    const fullPrompt = [userText, ...inlineTextParts].filter(Boolean).join("\n\n");

    const nowIso = new Date().toISOString();
    const userMsgId = `user-${Date.now()}`;
    const asstMsgId = `asst-${Date.now() + 1}`;
    const threadId = activeThreadId;
    const isFirstMessage = threads.find((t) => t.remoteId === threadId)?.title === "New Chat";

    // Show attachment filenames in the user message bubble
    const attachmentNote = msgAttachments.length > 0
      ? `\n[Attached: ${msgAttachments.map((a) => a.name).join(", ")}]`
      : "";
    const displayText = (userText || "(file)") + attachmentNote;

    setThinkingSteps([]);
    setMessages((prev) => [
      ...prev,
      { id: userMsgId, role: "user", content: displayText, createdAt: nowIso },
      { id: asstMsgId, role: "assistant", content: "", createdAt: nowIso },
    ]);
    scrollThreadToBottom();
    setThreads((prev) => prev.map((t) => t.remoteId === threadId ? { ...t, lastMessageAt: nowIso } : t));
    setIsRunning(true);

    try {
      await runStream(asstMsgId, {
        commands: [{ type: "add-message", message: { role: "user", parts: [{ type: "text", text: fullPrompt }] } }],
        threadId,
        mode: taskMode,
        ...(activeProjectId ? { projectId: activeProjectId } : {}),
        ...(backendAttachments.length > 0 ? { attachments: backendAttachments } : {}),
      });

      if (isFirstMessage && threadId) {
        const newTitle = userText.slice(0, 60).trim();
        setThreads((prev) => prev.map((t) => t.remoteId === threadId ? { ...t, title: newTitle } : t));
        await persistTitle(threadId, newTitle);
      }
    } catch (e: unknown) {
      if ((e as Error)?.name === "AbortError") return; // user cancelled — no error shown
      const errMsg = e instanceof Error ? e.message : String(e);
      setMessages((prev) => prev.map((m) => m.id === asstMsgId ? { ...m, content: `Error: ${errMsg}` } : m));
    } finally {
      if (!abortRef.current?.signal.aborted) setIsRunning(false);
      if (abortRef.current?.signal.aborted === false) abortRef.current = null;
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeThreadId, threads, activeProjectId, taskMode]);

  // ── onReload (retry last assistant response) ──────────────────────────────────

  const onReload = useCallback(async () => {
    if (isRunning || !activeThreadId) return;

    const lastAsst = [...messages].reverse().find((m) => m.role === "assistant");
    const lastUser = [...messages].reverse().find((m) => m.role === "user");
    if (!lastUser || !lastAsst) return;

    const newAsstId = `asst-retry-${Date.now()}`;
    setMessages((prev) => {
      const without = prev.filter((m) => m.id !== lastAsst.id);
      return [...without, { id: newAsstId, role: "assistant", content: "" }];
    });
    setThinkingSteps([]);
    setIsRunning(true);

    try {
      await runStream(newAsstId, {
        commands: [{ type: "add-message", message: { role: "user", parts: [{ type: "text", text: lastUser.content }] } }],
        threadId: activeThreadId,
        mode: taskMode,
        retry: true,
        ...(activeProjectId ? { projectId: activeProjectId } : {}),
      });
    } catch (e: unknown) {
      if ((e as Error)?.name === "AbortError") return;
      const errMsg = e instanceof Error ? e.message : String(e);
      setMessages((prev) => prev.map((m) => m.id === newAsstId ? { ...m, content: `Error: ${errMsg}` } : m));
    } finally {
      if (!abortRef.current?.signal.aborted) setIsRunning(false);
      if (abortRef.current?.signal.aborted === false) abortRef.current = null;
    }
  }, [activeThreadId, isRunning, messages, activeProjectId, taskMode]);

  // ── onEdit (replace a previous message) ──────────────────────────────────────

  const onEdit = useCallback(async (message: AppendMessage) => {
    if (isRunning || !activeThreadId) return;

    const newText = message.content
      .filter((p) => p.type === "text")
      .map((p: { type: string; text?: string }) => p.text ?? "")
      .join(" ").trim();

    if (!newText) return;

    const parentId = message.parentId;
    const keepUpToIdx = parentId ? messages.findIndex((m) => m.id === parentId) : -1;
    const keepCount = keepUpToIdx >= 0 ? keepUpToIdx + 1 : 0;

    // Best-effort: clean up DB messages after the edit point
    fetch(`/api/threads/${activeThreadId}/truncate`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keep_count: keepCount }),
    }).catch(() => {});

    const newAsstId = `asst-edit-${Date.now()}`;
    const newUserMsgId = `user-edit-${Date.now()}`;

    setMessages([
      ...messages.slice(0, keepCount),
      { id: newUserMsgId, role: "user", content: newText },
      { id: newAsstId, role: "assistant", content: "" },
    ]);
    setThinkingSteps([]);
    setIsRunning(true);

    try {
      await runStream(newAsstId, {
        commands: [{ type: "add-message", message: { role: "user", parts: [{ type: "text", text: newText }] } }],
        threadId: activeThreadId,
        mode: taskMode,
        ...(activeProjectId ? { projectId: activeProjectId } : {}),
      });
    } catch (e: unknown) {
      if ((e as Error)?.name === "AbortError") return;
      const errMsg = e instanceof Error ? e.message : String(e);
      setMessages((prev) => prev.map((m) => m.id === newAsstId ? { ...m, content: `Error: ${errMsg}` } : m));
    } finally {
      if (!abortRef.current?.signal.aborted) setIsRunning(false);
      if (abortRef.current?.signal.aborted === false) abortRef.current = null;
    }
  }, [activeThreadId, isRunning, messages, activeProjectId, taskMode]);

  // ── onCancel (stop generating) ────────────────────────────────────────────────

  const onCancel = useCallback(async () => {
    cancelStream();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Runtime ───────────────────────────────────────────────────────────────────

  const attachmentAdapter = new CompositeAttachmentAdapter([
    new SimpleImageAttachmentAdapter(),
    new SimpleTextAttachmentAdapter(),
  ]);

  const runtime = useExternalStoreRuntime<ChatMessage>({
    messages,
    isRunning,
    onNew,
    onEdit,
    onReload,
    onCancel,
    convertMessage: (msg) => ({
      id: msg.id,
      role: msg.role,
      content: [{ type: "text" as const, text: msg.content }],
      createdAt: msg.createdAt ? new Date(msg.createdAt) : undefined,
    }),
    adapters: { attachments: attachmentAdapter },
  });

  // ── Render ────────────────────────────────────────────────────────────────────

  return (
    <div className="h-screen flex overflow-hidden bg-background">
      {/* Sidebar */}
      <aside className="w-[260px] shrink-0 flex flex-col border-r border-border bg-card">
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
          {projects.length > 0 && (
            <select
              value={activeProjectId ?? ""}
              onChange={(e) => {
                const val = e.target.value;
                selectProject(val ? Number(val) : null, activeThreadId);
              }}
              className="mt-2 w-full px-3 py-1.5 rounded-lg text-xs bg-secondary border border-border text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
              title="Focus agent on a project"
            >
              <option value="">General (no project)</option>
              {projects.map((p) => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </select>
          )}
        </div>

        <div className="flex-1 overflow-y-auto px-2 pb-2">
          {sidebarLoading ? (
            <div className="px-3 py-2 text-sm text-muted-foreground">Loading...</div>
          ) : threads.length === 0 ? (
            <div className="px-3 py-2 text-sm text-muted-foreground">No chats yet</div>
          ) : (
            groupThreadsByDate(threads).map(({ label, threads: group }) => (
              <div key={label}>
                <p className="px-2 pt-3 pb-1 text-[11px] font-medium uppercase tracking-wider text-muted-foreground/60">
                  {label}
                </p>
                {group.map((t) => (
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
                ))}
              </div>
            ))
          )}
        </div>

        <div className="px-2 py-3 border-t border-border flex flex-col gap-0.5">
          {isAdmin && (
            <button
              onClick={() => setShowAccessPanel(true)}
              className="flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm text-muted-foreground hover:text-foreground hover:bg-accent transition-colors w-full text-left"
            >
              <ShieldIcon className="size-4" />
              Access Control
            </button>
          )}
          {isAdmin && (
            <button
              onClick={() => setShowSettingsPanel(true)}
              className={`flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm transition-colors w-full text-left ${showSettingsPanel ? "bg-accent text-foreground" : "text-muted-foreground hover:text-foreground hover:bg-accent"}`}
            >
              <SettingsIcon className="size-4" />
              Settings
            </button>
          )}
          <a href="/chat/logout" className="flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm text-muted-foreground hover:text-foreground hover:bg-accent transition-colors">
            <LogOutIcon className="size-4" />
            Logout
          </a>
        </div>
      </aside>

      {/* Chat area or settings panel */}
      {showSettingsPanel ? (
        <SettingsPanel onClose={() => setShowSettingsPanel(false)} />
      ) : (
        <main className="flex-1 overflow-hidden bg-background relative flex flex-col">
          {pendingPlan && (
            <div className="shrink-0 px-4 pt-3 z-10">
              <div className="flex items-start gap-3 rounded-xl border border-amber-300 bg-amber-50 dark:bg-amber-950/30 dark:border-amber-700 px-4 py-3 text-sm">
                <AlertCircleIcon className="size-5 text-amber-500 shrink-0 mt-0.5" />
                <div className="flex-1 min-w-0">
                  <p className="font-semibold text-amber-900 dark:text-amber-200">Plan ready for review</p>
                  <p className="text-amber-700 dark:text-amber-400 text-xs mt-0.5">Approve to start implementation, or reject to cancel.</p>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <button
                    onClick={() => rejectPlan(pendingPlan.taskId)}
                    className="px-3 py-1.5 rounded-lg text-xs font-medium border border-border bg-background hover:bg-accent text-foreground transition-colors"
                  >
                    Reject
                  </button>
                  <button
                    onClick={() => approvePlan(pendingPlan.taskId)}
                    className="px-3 py-1.5 rounded-lg text-xs font-medium bg-primary hover:bg-primary/90 text-primary-foreground transition-colors"
                  >
                    Approve Plan
                  </button>
                </div>
              </div>
            </div>
          )}
          <ThreadErrorBoundary key={activeThreadId ?? "no-thread"}>
            <ThinkingContext.Provider value={{ steps: thinkingSteps }}>
              <TaskModeContext.Provider value={{ mode: taskMode, setMode: setTaskMode }}>
                <AssistantRuntimeProvider runtime={runtime}>
                  <Thread />
                </AssistantRuntimeProvider>
              </TaskModeContext.Provider>
            </ThinkingContext.Provider>
          </ThreadErrorBoundary>
        </main>
      )}

      {/* Access control panel (admin only) */}
      <AccessPanel open={showAccessPanel} onClose={() => setShowAccessPanel(false)} />

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
                <Dialog.Title className="text-base font-semibold text-foreground">Delete chat?</Dialog.Title>
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
  thread, isActive, isRenaming, renameValue, renameInputRef,
  onSelect, onStartRename, onRenameChange, onRenameCommit, onRenameCancel, onDelete,
}: ThreadItemProps) {
  const [hovered, setHovered] = useState(false);

  if (isRenaming) {
    return (
      <div className="flex items-center gap-1 px-2 py-1 rounded-lg bg-accent">
        <input
          ref={renameInputRef}
          value={renameValue}
          onChange={(e) => onRenameChange(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") onRenameCommit(); if (e.key === "Escape") onRenameCancel(); }}
          className="flex-1 min-w-0 bg-transparent text-sm text-foreground outline-none"
        />
        <button onClick={onRenameCommit} className="shrink-0 p-1 rounded text-muted-foreground hover:text-foreground" title="Save">
          <CheckIcon className="size-3" />
        </button>
        <button onClick={onRenameCancel} className="shrink-0 p-1 rounded text-muted-foreground hover:text-foreground" title="Cancel">
          <XIcon className="size-3" />
        </button>
      </div>
    );
  }

  const timeLabel = thread.lastMessageAt ? formatRelativeTime(thread.lastMessageAt) : null;

  return (
    <div
      className={`group relative flex items-center rounded-lg transition-colors ${
        isActive ? "bg-accent text-foreground" : "text-muted-foreground hover:bg-accent/50 hover:text-foreground"
      }`}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      <button onClick={onSelect} className="flex-1 text-left px-3 py-2 min-w-0">
        <div className="flex items-center justify-between gap-1">
          <span className="text-sm truncate">{thread.title}</span>
          {timeLabel && !hovered && !isActive && (
            <span className="text-[10px] text-muted-foreground/60 shrink-0 tabular-nums">{timeLabel}</span>
          )}
        </div>
      </button>

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

// ── Date grouping helpers ─────────────────────────────────────────────────────

function formatRelativeTime(isoStr: string): string {
  const d = new Date(isoStr);
  const now = new Date();
  const diffMs = now.getTime() - d.getTime();
  const diffMin = Math.floor(diffMs / 60000);
  if (diffMin < 1) return "just now";
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffH = Math.floor(diffMin / 60);
  if (diffH < 24) return `${diffH}h ago`;
  const diffD = Math.floor(diffH / 24);
  if (diffD === 1) return "yesterday";
  if (diffD < 7) return `${diffD}d ago`;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

type DateGroup = "Today" | "Yesterday" | "This week" | "This month" | "Older";

function getDateGroup(isoStr?: string): DateGroup {
  if (!isoStr) return "Older";
  const d = new Date(isoStr);
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const startOfYesterday = new Date(startOfToday.getTime() - 86400000);
  const startOfWeek = new Date(startOfToday.getTime() - 6 * 86400000);
  const startOfMonth = new Date(now.getFullYear(), now.getMonth(), 1);
  if (d >= startOfToday) return "Today";
  if (d >= startOfYesterday) return "Yesterday";
  if (d >= startOfWeek) return "This week";
  if (d >= startOfMonth) return "This month";
  return "Older";
}

function groupThreadsByDate(threads: ChatThread[]): { label: DateGroup; threads: ChatThread[] }[] {
  const order: DateGroup[] = ["Today", "Yesterday", "This week", "This month", "Older"];
  const map = new Map<DateGroup, ChatThread[]>();
  for (const t of threads) {
    const g = getDateGroup(t.lastMessageAt);
    if (!map.has(g)) map.set(g, []);
    map.get(g)!.push(t);
  }
  return order.filter((g) => map.has(g)).map((g) => ({ label: g, threads: map.get(g)! }));
}
