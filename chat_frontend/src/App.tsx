import "./index.css";
import { Component, memo, useState, useCallback, useEffect, useRef, type ReactNode, type RefObject } from "react";

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
import { InstanceBanner, type BannerKind } from "@/components/instance/InstanceBanner";
import { InstanceCard, type CardKind } from "@/components/instance/InstanceCard";
import { NewChatModal } from "@/components/NewChatModal";
import type { CardAction } from "@/types/stream-events";
import { ThinkingContext } from "@/lib/thinking-context";
import { TaskModeContext } from "@/lib/task-mode-context";
import { PlusIcon, SettingsIcon, LogOutIcon, PencilIcon, TrashIcon, CheckIcon, XIcon, ShieldIcon, AlertCircleIcon, Loader2 } from "lucide-react";
import { AccessPanel } from "@/components/AccessPanel";
import { SettingsPanel } from "@/components/SettingsPanel"; // admin-only settings

// ── Types ────────────────────────────────────────────────────────────────────

interface ChatThread {
  remoteId: string;
  title: string;
  projectId?: number | null;
  gitMode?: string;
  lastMessageAt?: string; // ISO string from API
}

interface Project {
  id: number;
  name: string;
  techStack?: string | null;
  /** "container" | "docker" | "host" — used by the new-chat modal badge
   *  and by future pre-flight checks. Optional because legacy
   *  ProjectResponse rows may omit it. */
  mode?: string;
  status?: string;
}

interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  createdAt?: string; // ISO string
}

// ── Stream reader ─────────────────────────────────────────────────────────────
//
// `2:`-prefixed lines carry an array of StreamEvent payloads (see
// chat_frontend/src/types/stream-events.ts — generated from the JSON
// Schema at specs/001-per-chat-instances/contracts/stream-events.schema.json).
// `0:`-prefixed lines carry text deltas.
//
// Until 2026-04-24 this parser only handled `tool_use` + `message_id`
// and silently dropped the seven container-mode events. The
// pipeline-fitness audit (`stream_event_contract`) now gates this:
// every type in the schema MUST have a case here. The exhaustive
// switch (with a `never`-typed default) makes adding a new event in
// the schema a TypeScript compile error until every consumer updates.

import type { StreamEvent } from "@/types/stream-events";

async function readStream(
  body: ReadableStream<Uint8Array>,
  signal: AbortSignal,
  onText: (accumulated: string) => void,
  onTool: (tool: string) => void,
  onId?: (id: string) => void,
  onInstanceEvent?: (evt: StreamEvent) => void,
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
            const events = JSON.parse(line.slice(2)) as StreamEvent[];
            for (const evt of events) {
              switch (evt.type) {
                case "tool_use":
                  if (evt.tool) onTool(evt.tool);
                  break;
                case "message_id":
                  if (evt.id) onId?.(evt.id);
                  break;
                case "instance_provisioning":
                case "instance_failed":
                case "instance_limit_exceeded":
                case "instance_upstream_degraded":
                case "instance_busy":
                case "instance_terminating":
                case "instance_retry_started":
                case "confirm":
                case "tool_result":
                case "error":
                  onInstanceEvent?.(evt);
                  break;
                default: {
                  // Exhaustiveness gate: TypeScript reports a `never`
                  // mismatch here if a new StreamEvent variant lands
                  // without a matching case above.
                  const _exhaustive: never = evt;
                  void _exhaustive;
                  console.warn("[parseStream] unknown event type", evt);
                }
              }
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

  // T100/T101/T102/T104 — container-mode UX state. Banners stack
  // (newest-first, max 3); the active card replaces any prior card.
  type Banner = {
    kind: BannerKind;
    slug?: string;
    etaSeconds?: number;
    capabilities?: Record<string, string>;
    /** monotonic id for keying React lists + de-dup */
    seq: number;
  };
  type Card = {
    kind: CardKind;
    prompt: string;
    actions: CardAction[];
    failureCode?: string;
    variant?: "per_user_cap" | "platform_capacity";
    /** Plan v2 Change 2: provisioning card carries the slug + ETA +
     *  wall-clock start so the body can render a live elapsed counter. */
    slug?: string;
    etaSeconds?: number;
    startedAtMs?: number;
  };
  const [activeBanners, setActiveBanners] = useState<Banner[]>([]);
  const [activeCard, setActiveCard] = useState<Card | null>(null);
  // Plan v2 Change 1: mandatory project-picker modal on "New chat".
  const [showNewChatModal, setShowNewChatModal] = useState(false);
  const bannerSeq = useRef(0);

  function pushBanner(b: Omit<Banner, "seq">) {
    bannerSeq.current += 1;
    const seq = bannerSeq.current;
    setActiveBanners((prev) => [
      { ...b, seq },
      ...prev.filter((x) => x.kind !== b.kind),
    ].slice(0, 3));
  }
  function clearBannersOfKind(kinds: BannerKind[]) {
    setActiveBanners((prev) => prev.filter((b) => !kinds.includes(b.kind)));
  }

  // Single dispatch of one StreamEvent into banner / card state.
  // Used by BOTH the runtime's readStream pipeline AND the
  // action-button handler (which raw-fetches /api/assistant for
  // synthetic action_id chat messages and needs to drain the
  // response stream so the events reach this dispatcher).
  function dispatchInstanceEvent(evt: StreamEvent) {
    switch (evt.type) {
      case "instance_provisioning":
        // Plan v2 Change 2: render provisioning as a full-width card
        // (the user explicitly asked for this — the banner pill was
        // too thin to convey "the platform is working for me right
        // now"). The card's body has a live elapsed counter against
        // the ETA. Replaces any stale failure card from a prior
        // attempt; clears terminating / busy / retry banners.
        clearBannersOfKind([
          "terminating",
          "busy",
          "retry_started",
          "provisioning",
        ]);
        setActiveCard({
          kind: "provisioning",
          prompt: "Spinning up your environment",
          actions: [],
          slug: evt.slug,
          etaSeconds: evt.estimated_seconds,
          startedAtMs: Date.now(),
        });
        break;
      case "instance_upstream_degraded":
        pushBanner({ kind: "upstream_degraded", slug: evt.slug, capabilities: evt.capabilities });
        break;
      case "instance_busy":
        pushBanner({ kind: "busy", slug: evt.slug });
        break;
      case "instance_terminating":
        clearBannersOfKind(["provisioning", "upstream_degraded", "busy", "retry_started"]);
        pushBanner({ kind: "terminating", slug: evt.slug });
        break;
      case "instance_retry_started":
        // Retry just started: kill the old provisioning banner so the
        // user doesn't see a stale slug between events.
        clearBannersOfKind(["provisioning", "terminating"]);
        pushBanner({ kind: "retry_started" });
        break;
      case "instance_failed":
        // A new failure replaces the old card and clears any stale
        // provisioning banner from the previous attempt. The optional
        // `message` field carries the per-failure-code prose from
        // `_failure_chat_copy` (Change 3 — no longer impersonates the
        // LLM via append_text).
        clearBannersOfKind(["provisioning", "retry_started"]);
        setActiveCard({
          kind: "failed",
          prompt: evt.message ?? "Something went wrong starting your environment.",
          actions: evt.actions ?? [],
          failureCode: evt.failure_code,
        });
        break;
      case "instance_limit_exceeded":
        if (evt.variant === "per_user_cap") {
          setActiveCard({
            kind: "cap_exceeded",
            prompt: `You already have ${evt.active_chat_ids?.length ?? 0} active chats (cap=${evt.cap}). End one to start another.`,
            actions: evt.actions ?? [],
            variant: "per_user_cap",
          });
        } else {
          setActiveCard({
            kind: "cap_exceeded",
            prompt: "The platform is at capacity right now. Please try again in a few minutes.",
            actions: [],
            variant: "platform_capacity",
          });
        }
        break;
      case "confirm":
        setActiveCard({
          kind: "confirm",
          prompt: evt.prompt,
          actions: evt.actions ?? [],
        });
        break;
      case "tool_result":
        // Pre-existing event — surfaced via the assistant-ui
        // ToolResultBlock pipeline elsewhere. No-op here keeps the
        // exhaustiveness gate satisfied.
        break;
      case "error":
        // Short orchestrator-level error (Change 3 of senior-DevOps
        // refactor): rendered as an inline card with Main Menu so the
        // user is never dead-ended (CLAUDE.md No Dead Ends).
        setActiveCard({
          kind: "failed",
          prompt: evt.message,
          actions: [{ label: "Main Menu", action_id: "menu:main" }],
        });
        break;
      // tool_use / message_id are handled by their dedicated
      // callbacks; they shouldn't reach this dispatcher, but the
      // exhaustiveness gate forces explicit cases.
      case "tool_use":
      case "message_id":
        break;
    }
  }

  // Drain the body of a raw /api/assistant response so events from
  // an action-button-triggered request reach dispatchInstanceEvent.
  // Without this, the action button would POST + ignore the response
  // and the user would never see the resulting banners/cards.
  async function drainAssistantResponse(res: Response) {
    if (!res.body) return;
    const abort = new AbortController();
    await readStream(
      res.body,
      abort.signal,
      () => {},
      () => {},
      () => {},
      dispatchInstanceEvent,
    );
  }

  // Projects
  const [projects, setProjects] = useState<Project[]>([]);
  const [activeProjectId, setActiveProjectId] = useState<number | null>(null);
  // Git mode selector was removed from the UI — every chat uses session_branch
  // by default (one branch per chat, every task = a commit on it).

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
  // Reconnect backoff state — exponential backoff with max 8 retries
  const wsReconnectAttempt = useRef(0);
  const _WS_MAX_RETRIES = 8;

  // Rename state
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const renameInputRef = useRef<HTMLInputElement>(null);

  // Delete modal
  const [deleteTarget, setDeleteTarget] = useState<ChatThread | null>(null);

  // Task mode toggle: quick = skip planning, plan = generate plan for approval
  const [taskMode, setTaskMode] = useState<"quick" | "plan">("quick");

  // Pending plan approval (set when worker sends a plan_preview event)
  type PlanStatus = "idle" | "approving" | "rejecting" | "approved" | "rejected" | "error";
  const [pendingPlan, setPendingPlan] = useState<{
    taskId: string;
    messageId: string;
    status?: PlanStatus;
    errorMsg?: string;
  } | null>(null);

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

  async function _submitPlanAction(
    kind: "approve" | "reject",
    taskId: string,
  ) {
    const chatId = `web:${myUserId}:${activeThreadId}`;
    const pendingStatus: PlanStatus = kind === "approve" ? "approving" : "rejecting";
    const doneStatus: PlanStatus = kind === "approve" ? "approved" : "rejected";
    setPendingPlan((p) => (p ? { ...p, status: pendingStatus, errorMsg: undefined } : p));
    try {
      const res = await fetch("/api/web-action", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action_id: `${kind}_plan:${taskId}`,
          chat_id: chatId,
          message_id: "",
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setPendingPlan((p) => (p ? { ...p, status: doneStatus } : p));
      // Auto-clear the banner after a short confirmation so the subsequent
      // task progress card has the viewport to itself.
      setTimeout(() => {
        setPendingPlan((p) =>
          p && p.taskId === taskId && (p.status === "approved" || p.status === "rejected")
            ? null
            : p,
        );
      }, 1500);
    } catch (e) {
      setPendingPlan((p) =>
        p ? { ...p, status: "error", errorMsg: (e as Error)?.message ?? "Request failed" } : p,
      );
    }
  }

  function approvePlan(taskId: string) { return _submitPlanAction("approve", taskId); }
  function rejectPlan(taskId: string) { return _submitPlanAction("reject", taskId); }

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
    setMessages((prev) => prev.filter((m) => m.content !== "__LOADING__"));
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
    setTimeout(() => {
      const viewport = getViewport();
      if (viewport) viewport.scrollTo({ top: viewport.scrollHeight, behavior: "smooth" });
    }, delay);
  }

  // Cached viewport element for scroll helpers — avoids DOM query every frame
  const viewportRef = useRef<Element | null>(null);
  function getViewport(): Element | null {
    if (!viewportRef.current || !viewportRef.current.isConnected) {
      viewportRef.current = document.querySelector(".aui-thread-viewport");
    }
    return viewportRef.current;
  }

  // Sticky-scroll helper: if the viewport is already at (or near) the bottom,
  // keep it there. Uses cached viewport ref instead of querying DOM every frame.
  function stickyScrollToBottom() {
    const viewport = getViewport();
    if (!viewport) return;
    const distFromBottom = viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight;
    if (distFromBottom < 120) viewport.scrollTop = viewport.scrollHeight;
  }

  function openWorkerSocket(userId: number, sessionId: string) {
    if (wsRef.current) { wsRef.current.close(); wsRef.current = null; }
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/api/ws/${userId}/${sessionId}`);

    ws.onopen = () => { wsReconnectAttempt.current = 0; };

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data) as {
          type: string;
          text?: string;
          delta?: string;
          message_id?: string;
          task_id?: string;
          card?: unknown;
        };

        // Server keepalive ping — ignore
        if (data.type === "ping") return;
        const text = data.text ?? "";
        const msgKey = data.message_id
          ? `worker-msg-${data.message_id}`
          : `worker-${sessionId}`;

        // plainId: the bare numeric DB message id — needed to deduplicate against
        // DB-loaded messages (id="123") vs WS-created messages (id="worker-msg-123")
        // Coerce to string because JSON.parse may return a number for message_id.
        const plainId = data.message_id != null ? String(data.message_id) : null;
        const matchesMsg = (id: string) => id === msgKey || (plainId !== null && id === plainId);

        if (data.type === "token" && data.delta) {
          // Token-level streaming from agent session — append to the latest assistant message
          const delta = data.delta;
          setMessages((prev) => {
            const lastAssistantIdx = prev.map((m, i) => ({ m, i }))
              .filter(({ m }) => m.role === "assistant")
              .pop()?.i;
            if (lastAssistantIdx === undefined) {
              // No open assistant message — create one
              scrollThreadToBottom();
              return [...prev, { id: msgKey, role: "assistant" as const, content: delta }];
            }
            return prev.map((m, i) =>
              i === lastAssistantIdx ? { ...m, content: m.content + delta } : m
            );
          });
        } else if (data.type === "agent_token" && data.text) {
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
        } else if (data.type === "tool_output") {
          // Stream stdout/stderr from a long-running host_run_command into the
          // same progress-card stream_buffer so the user sees live output.
          const toolData = data as unknown as { chunk?: string; tool?: string; final?: boolean };
          const chunk = toolData.chunk ?? "";
          if (chunk) {
            setMessages((prev) =>
              prev.map((m) => {
                if (!matchesMsg(m.id)) return m;
                if (!m.content.startsWith("__PROGRESS_CARD__")) return m;
                try {
                  const card = JSON.parse(m.content.slice("__PROGRESS_CARD__".length));
                  const updated = { ...card, stream_buffer: (card.stream_buffer ?? "") + chunk };
                  return { ...m, content: `__PROGRESS_CARD__${JSON.stringify(updated)}` };
                } catch { return m; }
              })
            );
          }
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
            if (exists) {
              return prev.map((m) => {
                if (!matchesMsg(m.id)) return m;
                // NEVER wipe a progress card with empty text — msg_new for card placeholders
                // sometimes races behind progress_card and would blank the card entirely.
                if (m.content.startsWith("__PROGRESS_CARD__") && !text) return m;
                return { ...m, id: msgKey, content: text };
              });
            }
            scrollThreadToBottom();
            return [...prev, { id: msgKey, role: "assistant" as const, content: text }];
          });
        }
      } catch { /* ignore malformed */ }
    };

    ws.onerror = () => { /* onclose handles cleanup */ };

    // Reconnect with exponential backoff (1s, 2s, 4s, 8s, ... up to 30s, max 8 retries)
    ws.onclose = () => {
      if (wsRef.current !== ws) return;
      wsRef.current = null;
      if (!wsIntentionalClose.current && wsReconnectAttempt.current < _WS_MAX_RETRIES) {
        const delay = Math.min(1000 * Math.pow(2, wsReconnectAttempt.current), 30000);
        wsReconnectAttempt.current += 1;
        setTimeout(() => openWorkerSocket(userId, sessionId), delay);
      }
    };

    wsRef.current = ws;
  }

  async function loadProjects() {
    try {
      const res = await fetch("/api/projects", { credentials: "include" });
      if (res.ok) {
        const data = await res.json();
        // /api/projects returns a raw list[ProjectResponse]; legacy
        // wrappers used `{projects: [...]}`. Handle both.
        const raw = Array.isArray(data) ? data : (data.projects ?? []);
        const list: Project[] = raw.map((p: {
          id: number;
          name: string;
          tech_stack?: string | null;
          mode?: string;
          status?: string;
        }) => ({
          id: p.id,
          name: p.name,
          techStack: p.tech_stack ?? null,
          mode: p.mode,
          status: p.status,
        }));
        setProjects(list);
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
    setShowSettingsPanel(false); // clicking a chat should hide settings
    setActiveThreadId(id);
    setMessages([]);
    // Restore project context — use fromList if provided (avoids stale threads state)
    const lookup = fromList ?? threads;
    const thread = lookup.find((t) => t.remoteId === id);
    if (thread !== undefined) {
      setActiveProjectId(thread.projectId ?? null);
      // gitMode tracking removed; backend defaults to session_branch.
      void thread;
    }
    try {
      const res = await fetch(`/api/threads/${id}/messages`, { credentials: "include" });
      if (res.ok) {
        const data = await res.json();
        setMessages(
          (data.messages ?? [])
            // Filter out empty assistant stubs UNLESS they are incomplete (streaming).
            // Incomplete assistant messages show a TinkeringIndicator spinner after refresh.
            .filter((m: { role: string; content: string; isComplete?: boolean }) =>
              m.role !== "assistant" || m.content.trim() !== "" || m.isComplete === false
            )
            // Deduplicate by id — DB load vs WebSocket can both create an entry for the same message_id
            .filter((m: { id: string }, i: number, arr: { id: string }[]) =>
              arr.findIndex((x) => x.id === m.id) === i
            )
            .map((m: { id: string; role: string; content: string; createdAt?: string; isComplete?: boolean }) => ({
              id: m.id,
              role: m.role as "user" | "assistant",
              // Empty incomplete assistant messages show as loading spinner on refresh
              content: m.role === "assistant" && m.isComplete === false && m.content.trim() === ""
                ? "__LOADING__"
                : m.content,
              createdAt: m.createdAt,
            }))
        );
        // Scroll to the end of the conversation — messages may be many and
        // React needs time to render them all before scrollHeight is accurate.
        scrollThreadToBottom(200);
      }
    } catch { /* silently fail */ }
  }

  // Plan v2 Change 1: clicking "New conversation" no longer creates a
  // chat row immediately. Instead it opens a mandatory project-picker
  // modal. Only the user picking a project from the modal triggers the
  // POST /api/threads with `project_id` baked in (Change-4 atomic-
  // binding path). Cancel = no chat row. This makes a no-project chat
  // structurally impossible — the bug class that produced gaslit "I'll
  // spin up your env" replies on chat 35 etc.
  function newThread() {
    cancelStream();
    setPendingPlan(null);
    setShowSettingsPanel(false); // creating a chat should hide settings
    setShowNewChatModal(true);
  }

  async function createThreadWithProject(projectId: number) {
    setShowNewChatModal(false);
    try {
      const res = await fetch("/api/threads", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: projectId }),
      });
      if (res.ok) {
        const t = await res.json();
        const thread: ChatThread = {
          remoteId: t.remoteId,
          title: "New Chat",
          projectId,
        };
        setThreads((prev) => [thread, ...prev]);
        setMessages([]);
        setActiveProjectId(projectId);
        // Use a small delay to ensure the thread is in state before
        // setting active (avoids the selectThread guard check
        // `if (id === activeThreadId) return`)
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
      const res = await fetch(`/api/threads/${threadId}/archive`, { method: "POST", credentials: "include" });
      if (!res.ok) { console.warn("Delete failed:", res.status); return; }
      setDeleteTarget(null);
      // Use functional updater to get fresh threads state (avoids stale closure)
      let remaining: ChatThread[] = [];
      setThreads((prev) => {
        const filtered = prev.filter((t) => t.remoteId !== threadId);
        remaining = filtered;
        return filtered;
      });
      if (activeThreadId === threadId) {
        if (remaining.length > 0) {
          setActiveThreadId(null);
          await selectThread(remaining[0].remoteId);
        } else {
          setActiveThreadId(null);
          setMessages([]);
        }
      }
    } catch (e) { console.warn("Delete thread error:", e); }
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

    // Animation queue: network text accumulates in a plain ref; a RAF loop drains
    // it smoothly at ~30fps (every ~32ms) revealing ~6 chars per tick. That's a
    // steady word-per-tick cadence — feels like ChatGPT instead of big 80-char
    // blocks landing at 15fps. stickyScrollToBottom() is throttled separately so
    // scroll reflow doesn't compound the per-tick render cost.
    const pendingRef = { current: "" };   // full received text (updated by network)
    const displayedRef = { current: 0 };  // how many chars are currently shown
    const liveIdRef = { current: asstMsgId };
    let rafId: number | null = null;
    let streamDone = false;
    let lastRenderTime = 0;
    let lastScrollTime = 0;
    const RENDER_INTERVAL = 32; // ~30fps — smooth token reveal
    const CHARS_PER_TICK = 6;   // ~one short word per tick
    const SCROLL_INTERVAL = 120; // throttle scroll reflow independently

    function animateTick(now: number) {
      const full = pendingRef.current;
      const shown = displayedRef.current;
      const elapsed = now - lastRenderTime;
      if (shown < full.length && elapsed >= RENDER_INTERVAL) {
        const next = Math.min(full.length, shown + CHARS_PER_TICK);
        displayedRef.current = next;
        lastRenderTime = now;
        setMessages((prev) =>
          prev.map((m) => m.id === liveIdRef.current ? { ...m, content: full.slice(0, next) } : m)
        );
        if (now - lastScrollTime >= SCROLL_INTERVAL) {
          stickyScrollToBottom();
          lastScrollTime = now;
        }
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
        // T100 — container-mode events go through the shared
        // dispatcher so the action-button raw-fetch path can drain
        // the same way (drainAssistantResponse calls into it).
        dispatchInstanceEvent,
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

    // Clean up DB messages after the edit point — await to ensure consistency on reload
    try {
      await fetch(`/api/threads/${activeThreadId}/truncate`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ keep_count: keepCount }),
      });
    } catch (e) { console.warn("Truncate failed:", e); }

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
      {/* Plan v2 Change 1: mandatory project-pick modal on "New chat".
          Mounted at root so it overlays the entire chat surface. */}
      {showNewChatModal ? (
        <NewChatModal
          projects={projects}
          onPick={createThreadWithProject}
          onCancel={() => setShowNewChatModal(false)}
        />
      ) : null}
      {/* Sidebar */}
      <aside className="w-[255px] shrink-0 flex flex-col border-r border-border/60" style={{ background: "var(--sidebar)" }}>
        {/* Brand header */}
        <div className="px-4 pt-5 pb-4">
          <div className="flex items-center gap-2.5 mb-5">
            {/* Logo mark — monogram chip matches the login page brand mark */}
            <div
              className="size-8 rounded-xl grid place-items-center shrink-0 font-bold text-[13px] tracking-tight text-neutral-900 shadow-lg ring-1 ring-white/5"
              style={{ background: "linear-gradient(135deg, #ffffff 0%, #c7c7c7 100%)" }}
            >
              T
            </div>
            <div>
              <span className="font-semibold text-sm text-foreground tracking-tight">TAGH DevOps</span>
              <p className="text-[10px] text-muted-foreground/70 leading-none mt-0.5">AI DevOps Agent</p>
            </div>
          </div>

          <button
            onClick={newThread}
            className="w-full flex items-center gap-2 px-3 py-2.5 rounded-xl text-sm font-medium transition-all duration-150 border border-border/60 text-foreground/80 hover:text-foreground hover:border-primary/40"
            style={{ background: "oklch(0.13 0.008 265)" }}
          >
            <div
              className="size-4.5 rounded-md flex items-center justify-center text-neutral-900"
              style={{ background: "linear-gradient(135deg, #ffffff 0%, #c7c7c7 100%)" }}
            >
              <PlusIcon className="size-3" />
            </div>
            New conversation
          </button>

          {projects.length > 0 && (
            <select
              value={activeProjectId ?? ""}
              onChange={(e) => {
                const val = e.target.value;
                selectProject(val ? Number(val) : null, activeThreadId);
              }}
              className="mt-2 w-full px-3 py-1.5 rounded-lg text-xs border border-border/60 text-foreground/80 focus:outline-none focus:ring-1 focus:ring-primary/50 focus:border-primary/40 transition-colors"
              style={{ background: "oklch(0.13 0.008 265)" }}
              title="Focus agent on a project"
            >
              <option value="">All projects</option>
              {projects.map((p) => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </select>
          )}

          {/* Git workflow is fixed: every chat = one branch, every task in
              that chat = a commit on it. No user selector. */}
        </div>

        {/* Thread list */}
        <div className="flex-1 overflow-y-auto px-2 pb-2">
          {sidebarLoading ? (
            <div className="px-3 py-6 flex flex-col gap-2">
              {[...Array(4)].map((_, i) => (
                <div key={i} className="h-8 rounded-lg animate-pulse" style={{ background: "oklch(0.15 0.008 265)", opacity: 1 - i * 0.15 }} />
              ))}
            </div>
          ) : threads.length === 0 ? (
            <div className="px-3 py-6 text-center">
              <p className="text-xs text-muted-foreground/60">No conversations yet</p>
              <p className="text-xs text-muted-foreground/40 mt-0.5">Start one above</p>
            </div>
          ) : (
            groupThreadsByDate(threads).map(({ label, threads: group }) => (
              <div key={label}>
                <p className="px-3 pt-4 pb-1.5 text-[10px] font-semibold uppercase tracking-widest text-muted-foreground/40">
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

        {/* Bottom actions */}
        <div className="px-2 py-3 border-t border-border/40 flex flex-col gap-0.5">
          {isAdmin && (
            <button
              onClick={() => setShowAccessPanel(true)}
              className="flex items-center gap-2.5 px-3 py-2 rounded-lg text-xs text-muted-foreground hover:text-foreground hover:bg-white/5 transition-colors w-full text-left"
            >
              <ShieldIcon className="size-3.5 shrink-0" />
              Access Control
            </button>
          )}
          {isAdmin && (
            <button
              onClick={() => setShowSettingsPanel(true)}
              className={`flex items-center gap-2.5 px-3 py-2 rounded-lg text-xs transition-colors w-full text-left ${showSettingsPanel ? "bg-white/8 text-foreground" : "text-muted-foreground hover:text-foreground hover:bg-white/5"}`}
            >
              <SettingsIcon className="size-3.5 shrink-0" />
              Settings
            </button>
          )}
          <a href="/chat/logout" className="flex items-center gap-2.5 px-3 py-2 rounded-lg text-xs text-muted-foreground hover:text-foreground hover:bg-white/5 transition-colors">
            <LogOutIcon className="size-3.5 shrink-0" />
            Sign out
          </a>
        </div>
      </aside>

      {/* Chat area or settings panel */}
      {showSettingsPanel ? (
        <SettingsPanel onClose={() => setShowSettingsPanel(false)} />
      ) : (
        <main className="flex-1 overflow-hidden bg-background relative flex flex-col">
          {pendingPlan && (() => {
            const st = pendingPlan.status ?? "idle";
            const busy = st === "approving" || st === "rejecting";
            const isApproved = st === "approved";
            const isRejected = st === "rejected";
            const isError = st === "error";
            // Terminal success/failure states swap the banner color so the click
            // gives an unambiguous visual landing.
            const wrap =
              isApproved
                ? "border-emerald-300 bg-emerald-50 dark:bg-emerald-950/30 dark:border-emerald-700"
                : isRejected
                ? "border-border bg-muted"
                : isError
                ? "border-red-300 bg-red-50 dark:bg-red-950/30 dark:border-red-700"
                : "border-amber-300 bg-amber-50 dark:bg-amber-950/30 dark:border-amber-700";
            return (
              <div className="shrink-0 px-4 pt-3 z-10">
                <div className={`flex items-start gap-3 rounded-xl border px-4 py-3 text-sm transition-colors ${wrap}`}>
                  {isApproved ? (
                    <CheckIcon className="size-5 text-emerald-600 shrink-0 mt-0.5" />
                  ) : isRejected ? (
                    <XIcon className="size-5 text-muted-foreground shrink-0 mt-0.5" />
                  ) : isError ? (
                    <AlertCircleIcon className="size-5 text-red-500 shrink-0 mt-0.5" />
                  ) : (
                    <AlertCircleIcon className="size-5 text-amber-500 shrink-0 mt-0.5" />
                  )}
                  <div className="flex-1 min-w-0">
                    {isApproved ? (
                      <>
                        <p className="font-semibold text-emerald-900 dark:text-emerald-200">Plan approved</p>
                        <p className="text-emerald-700 dark:text-emerald-400 text-xs mt-0.5">Starting implementation…</p>
                      </>
                    ) : isRejected ? (
                      <>
                        <p className="font-semibold text-foreground">Plan rejected</p>
                        <p className="text-muted-foreground text-xs mt-0.5">The task has been cancelled.</p>
                      </>
                    ) : isError ? (
                      <>
                        <p className="font-semibold text-red-900 dark:text-red-200">Couldn’t submit your decision</p>
                        <p className="text-red-700 dark:text-red-400 text-xs mt-0.5 break-all">{pendingPlan.errorMsg}</p>
                      </>
                    ) : (
                      <>
                        <p className="font-semibold text-amber-900 dark:text-amber-200">Plan ready for review</p>
                        <p className="text-amber-700 dark:text-amber-400 text-xs mt-0.5">Approve to start implementation, or reject to cancel.</p>
                      </>
                    )}
                  </div>
                  {!isApproved && !isRejected && (
                    <div className="flex items-center gap-2 shrink-0">
                      <button
                        onClick={() => rejectPlan(pendingPlan.taskId)}
                        disabled={busy}
                        className="px-3 py-1.5 rounded-lg text-xs font-medium border border-border bg-background hover:bg-accent text-foreground transition-colors disabled:opacity-60 disabled:cursor-not-allowed inline-flex items-center gap-1.5"
                      >
                        {st === "rejecting" && <Loader2 className="size-3 animate-spin" />}
                        {isError ? "Retry reject" : "Reject"}
                      </button>
                      <button
                        onClick={() => approvePlan(pendingPlan.taskId)}
                        disabled={busy}
                        className="px-3 py-1.5 rounded-lg text-xs font-medium bg-primary hover:bg-primary/90 text-primary-foreground transition-colors disabled:opacity-60 disabled:cursor-not-allowed inline-flex items-center gap-1.5"
                      >
                        {st === "approving" && <Loader2 className="size-3 animate-spin" />}
                        {isError ? "Retry approve" : "Approve Plan"}
                      </button>
                    </div>
                  )}
                </div>
              </div>
            );
          })()}
          {(activeBanners.length > 0 || activeCard) && (
            <div className="px-4 pt-2 pb-1 space-y-2">
              {activeBanners.map((b) => (
                <InstanceBanner
                  key={b.seq}
                  kind={b.kind}
                  slug={b.slug}
                  etaSeconds={b.etaSeconds}
                  capabilities={b.capabilities}
                />
              ))}
              {activeCard && (
                <InstanceCard
                  kind={activeCard.kind}
                  prompt={activeCard.prompt}
                  actions={activeCard.actions}
                  failureCode={activeCard.failureCode}
                  variant={activeCard.variant}
                  slug={activeCard.slug}
                  etaSeconds={activeCard.etaSeconds}
                  startedAtMs={activeCard.startedAtMs}
                  onAction={async (a) => {
                    // Cards close on any action so the user isn't
                    // stuck looking at a stale card after acting.
                    setActiveCard(null);
                    // UI-only action_ids (menu navigation): close the
                    // card and stop. Don't POST back to /api/assistant
                    // — that would re-trigger the same context (e.g.
                    // re-emit the failure card if the instance is
                    // still failed). User wanted out of this card,
                    // not a fresh round-trip.
                    if (a.action_id?.startsWith("menu:")) {
                      return;
                    }
                    if (a.link) {
                      // Direct navigation (e.g. "/chat" Main Menu).
                      window.location.assign(a.link);
                      return;
                    }
                    if (a.action_id) {
                      // Send the action_id back as a chat message —
                      // assistant_endpoint switches on it. Drain the
                      // response stream so the resulting events
                      // (instance_provisioning, instance_terminating,
                      // etc.) reach the dispatcher and update the UI.
                      try {
                        const res = await fetch("/api/assistant", {
                          method: "POST",
                          credentials: "include",
                          headers: { "Content-Type": "application/json" },
                          body: JSON.stringify({
                            threadId: activeThreadId,
                            commands: [{
                              type: "add-message",
                              message: { role: "user", parts: [{ type: "text", text: a.action_id }] },
                            }],
                            attachments: [],
                            retry: false,
                            mode: taskMode,
                            projectId: activeProjectId,
                          }),
                        });
                        await drainAssistantResponse(res);
                      } catch { /* best-effort; user can retry */ }
                    }
                  }}
                />
              )}
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

const ThreadItem = memo(function ThreadItem({
  thread, isActive, isRenaming, renameValue, renameInputRef,
  onSelect, onStartRename, onRenameChange, onRenameCommit, onRenameCancel, onDelete,
}: ThreadItemProps) {
  const [hovered, setHovered] = useState(false);

  if (isRenaming) {
    return (
      <div className="flex items-center gap-1 px-2 py-1.5 rounded-lg" style={{ background: "oklch(0.17 0.008 265)" }}>
        <input
          ref={renameInputRef}
          value={renameValue}
          onChange={(e) => onRenameChange(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") onRenameCommit(); if (e.key === "Escape") onRenameCancel(); }}
          className="flex-1 min-w-0 bg-transparent text-xs text-foreground outline-none"
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
      className={`group relative flex items-center rounded-lg transition-all duration-100 ${
        isActive
          ? "text-foreground"
          : "text-muted-foreground hover:text-foreground/90"
      }`}
      style={isActive ? { background: "oklch(0.18 0.012 265)" } : undefined}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      {isActive && (
        <div className="absolute left-0 top-1/2 -translate-y-1/2 w-0.5 h-5 rounded-r-full"
          style={{ background: "linear-gradient(to bottom, #ffffff, #c7c7c7)" }} />
      )}
      <button onClick={onSelect} className="flex-1 text-left px-3 py-2 min-w-0">
        <span className="block text-xs truncate font-medium pr-11">{thread.title}</span>
      </button>

      {/* Fixed-width right slot: timestamp shown when idle, rename/delete shown on hover or active.
          Both overlay the same space so the title never reflows on hover. */}
      <div className="absolute right-1.5 top-1/2 -translate-y-1/2 w-10 h-6 flex items-center justify-end pointer-events-none">
        {timeLabel && !hovered && !isActive && (
          <span className="text-[10px] text-muted-foreground/50 tabular-nums">{timeLabel}</span>
        )}
        {(hovered || isActive) && (
          <div className="flex items-center gap-0.5 pointer-events-auto">
            <button
              onClick={(e) => { e.stopPropagation(); onStartRename(); }}
              className="p-1 rounded hover:bg-white/8 text-muted-foreground/60 hover:text-foreground transition-colors"
              title="Rename"
            >
              <PencilIcon className="size-3" />
            </button>
            <button
              onClick={(e) => { e.stopPropagation(); onDelete(); }}
              className="p-1 rounded hover:bg-destructive/20 text-muted-foreground/60 hover:text-destructive transition-colors"
              title="Delete"
            >
              <TrashIcon className="size-3" />
            </button>
          </div>
        )}
      </div>
    </div>
  );
});

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
