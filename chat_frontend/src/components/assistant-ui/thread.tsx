import { StandaloneMarkdown } from "@/components/assistant-ui/markdown-text";
import { TooltipIconButton } from "@/components/assistant-ui/tooltip-icon-button";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { useThinking } from "@/lib/thinking-context";
import {
  ActionBarPrimitive,
  AttachmentPrimitive,
  AuiIf,
  BranchPickerPrimitive,
  ComposerPrimitive,
  ErrorPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useAuiState,
  useThreadRuntime,
} from "@assistant-ui/react";
import {
  ArrowDownIcon,
  ArrowUpIcon,
  CheckIcon,
  ChevronDownIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  CopyIcon,
  ExternalLinkIcon,
  PaperclipIcon,
  PencilIcon,
  RefreshCwIcon,
  SquareIcon,
  XIcon,
} from "lucide-react";
import { type FC, type ReactNode, Component, useState, useRef, useEffect } from "react";
import { useTaskMode } from "@/lib/task-mode-context";

// ── Per-message error boundary — isolates a single broken message ─────────────

class MessageErrorBoundary extends Component<
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
  render() {
    if (this.state.crashed) {
      return (
        <div className="mx-auto w-full max-w-(--thread-max-width) py-2 px-4">
          <span className="text-xs text-muted-foreground/40 italic select-none">
            [message unavailable]
          </span>
        </div>
      );
    }
    return this.props.children;
  }
}

// ── Tinkering animation shown while waiting for first token ──────────────────

const TinkeringIndicator: FC = () => (
  <div className="flex items-center gap-2 py-1 text-sm text-muted-foreground">
    <svg
      className="animate-spin size-4 text-primary"
      xmlns="http://www.w3.org/2000/svg"
      fill="none"
      viewBox="0 0 24 24"
    >
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
    </svg>
    <span>Tinkering...</span>
  </div>
);

// ── Thinking panel shows tool steps while Claude works ───────────────────────

const ThinkingPanel: FC = () => {
  const { steps } = useThinking();
  const [open, setOpen] = useState(true);
  if (steps.length === 0) return null;
  return (
    <div className="mb-3 rounded-lg border border-border/50 bg-muted/30 text-xs text-muted-foreground overflow-hidden">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-1.5 px-3 py-2 hover:bg-muted/50 transition-colors text-left"
      >
        <span className="text-primary/70">{open ? "▾" : "▸"}</span>
        <span className="font-medium">Thinking ({steps.length} {steps.length === 1 ? "step" : "steps"})</span>
      </button>
      {open && (
        <ul className="px-3 pb-2 space-y-0.5">
          {steps.map((s, i) => (
            <li key={i} className="flex items-center gap-1.5 truncate">
              <span className="text-primary/50">•</span>
              <span className="truncate font-mono">{s}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
};

export const Thread: FC = () => {
  return (
    <ThreadPrimitive.Root
      className="aui-root aui-thread-root @container flex h-full flex-col bg-background"
      style={{
        ["--thread-max-width" as string]: "44rem",
        ["--composer-radius" as string]: "24px",
        ["--composer-padding" as string]: "10px",
      }}
    >
      <ThreadPrimitive.Viewport
        turnAnchor="top"
        className="aui-thread-viewport relative flex flex-1 flex-col overflow-x-auto overflow-y-scroll scroll-smooth px-4 pt-4"
      >
        <ThreadPrimitive.Empty>
          <ThreadWelcome />
        </ThreadPrimitive.Empty>

        <ThreadPrimitive.Messages>
          {() => (
            <MessageErrorBoundary>
              <ThreadMessage />
            </MessageErrorBoundary>
          )}
        </ThreadPrimitive.Messages>

        <ThreadPrimitive.ViewportFooter className="sticky bottom-0 mx-auto mt-auto flex w-full max-w-(--thread-max-width) flex-col gap-4 overflow-visible rounded-t-(--composer-radius) bg-background pb-4 md:pb-6">
          <ThreadScrollToBottom />
          <Composer />
        </ThreadPrimitive.ViewportFooter>
      </ThreadPrimitive.Viewport>
    </ThreadPrimitive.Root>
  );
};

const ThreadMessage: FC = () => {
  const role = useAuiState((s) => s.message.role);
  const isEditing = useAuiState((s) => s.message.composer.isEditing);
  if (isEditing) return <EditComposer />;
  if (role === "user") return <UserMessage />;
  return <AssistantMessage />;
};

const ThreadScrollToBottom: FC = () => {
  return (
    <ThreadPrimitive.ScrollToBottom asChild>
      <TooltipIconButton
        tooltip="Scroll to bottom"
        variant="outline"
        className="absolute -top-12 z-10 self-center rounded-full p-4 disabled:invisible"
      >
        <ArrowDownIcon />
      </TooltipIconButton>
    </ThreadPrimitive.ScrollToBottom>
  );
};

const SUGGESTIONS = [
  { icon: "🚀", label: "Deploy a project", prompt: "Help me deploy a new project to production" },
  { icon: "🐛", label: "Debug an issue", prompt: "Help me debug a problem in my codebase" },
  { icon: "🔍", label: "Review my code", prompt: "Review my code for issues and improvements" },
  { icon: "⚙️", label: "Set up CI/CD", prompt: "Set up a CI/CD pipeline for my project" },
];

const ThreadWelcome: FC = () => {
  const threadRuntime = useThreadRuntime();

  return (
    <div className="mx-auto my-auto flex w-full max-w-(--thread-max-width) grow flex-col items-center justify-center px-6 pb-12">
      {/* Avatar */}
      <div className="mb-6 size-14 rounded-2xl flex items-center justify-center shadow-xl shadow-primary/20"
        style={{ background: "linear-gradient(135deg, oklch(0.62 0.22 265), oklch(0.55 0.22 295))" }}>
        <svg viewBox="0 0 24 24" fill="none" className="size-7" xmlns="http://www.w3.org/2000/svg">
          <path d="M12 2L16 10H8L12 2Z" fill="white" opacity="0.9"/>
          <path d="M8 10L4 18H12L8 10Z" fill="white" opacity="0.6"/>
          <path d="M16 10L20 18H12L16 10Z" fill="white" opacity="0.75"/>
        </svg>
      </div>

      <h1 className="text-3xl font-semibold tracking-tight text-foreground mb-2">
        How can I help you?
      </h1>
      <p className="text-base text-muted-foreground mb-8 text-center max-w-sm">
        I'm your AI DevOps agent. I can build, deploy, debug, and review code end-to-end.
      </p>

      {/* Suggestion chips — use runtime API instead of DOM manipulation */}
      <div className="grid grid-cols-2 gap-2.5 w-full max-w-md">
        {SUGGESTIONS.map((s) => (
          <button
            key={s.label}
            type="button"
            onClick={() => {
              threadRuntime.composer.setText(s.prompt);
              threadRuntime.composer.send();
            }}
            className="group flex items-start gap-2.5 rounded-xl border border-border/60 p-3.5 text-left transition-all duration-150 hover:border-primary/40 hover:shadow-sm"
            style={{ background: "oklch(0.12 0.008 265)" }}
          >
            <span className="text-base shrink-0 mt-0.5">{s.icon}</span>
            <span className="text-xs font-medium text-foreground/80 group-hover:text-foreground leading-snug transition-colors">{s.label}</span>
          </button>
        ))}
      </div>
    </div>
  );
};

const AttachmentItem: FC = () => (
  <AttachmentPrimitive.Root className="relative flex items-center gap-1.5 rounded-lg border bg-muted px-2 py-1 text-xs">
    <AttachmentPrimitive.unstable_Thumb className="size-8 rounded object-cover" />
    <span className="max-w-[100px] truncate text-foreground">
      <AttachmentPrimitive.Name />
    </span>
    <AttachmentPrimitive.Remove asChild>
      <button
        type="button"
        className="ml-1 rounded-full p-0.5 text-muted-foreground hover:bg-background hover:text-foreground"
        aria-label="Remove attachment"
      >
        <XIcon className="size-3" />
      </button>
    </AttachmentPrimitive.Remove>
  </AttachmentPrimitive.Root>
);

const ComposerAttachmentPreview: FC = () => (
  // eslint-disable-next-line @typescript-eslint/no-deprecated
  <ComposerPrimitive.Attachments components={{ Attachment: AttachmentItem }} />
);

const Composer: FC = () => {
  const { mode, setMode } = useTaskMode();

  return (
    <ComposerPrimitive.Root className="relative flex w-full flex-col">
      <ComposerPrimitive.AttachmentDropzone
        className="relative flex w-full flex-col rounded-2xl border border-border/60 transition-all duration-200 focus-within:border-primary/50 focus-within:shadow-lg focus-within:shadow-primary/10 data-[drag-over]:border-primary/60 data-[drag-over]:shadow-lg data-[drag-over]:shadow-primary/20"
        style={{ background: "oklch(0.12 0.008 265)" }}
      >
        {/* Attachment preview strip */}
        <div className="flex flex-wrap gap-2 px-4 pt-3 empty:hidden">
          <ComposerAttachmentPreview />
        </div>

        <ComposerPrimitive.Input
          placeholder="Message TAGH DevOps..."
          className="max-h-40 min-h-12 w-full resize-none bg-transparent px-4 py-3.5 text-sm outline-none placeholder:text-muted-foreground/50 leading-relaxed"
          rows={1}
          autoFocus
          aria-label="Message input"
        />

        <div className="flex items-center justify-between px-3 pb-3">
          {/* Mode toggle — left side */}
          <div className="flex items-center gap-0.5 rounded-full border border-border/50 p-0.5" style={{ background: "oklch(0.09 0.008 265)" }}>
            <button
              type="button"
              onClick={() => setMode("quick")}
              title="Quick: start coding immediately, no approval step"
              className={cn(
                "px-3 py-1 rounded-full text-[11px] font-medium transition-all duration-150",
                mode === "quick"
                  ? "bg-primary text-primary-foreground shadow-sm shadow-primary/30"
                  : "text-muted-foreground hover:text-foreground"
              )}
            >
              Quick
            </button>
            <button
              type="button"
              onClick={() => setMode("plan")}
              title="Plan: generates a plan for you to approve before coding starts"
              className={cn(
                "px-3 py-1 rounded-full text-[11px] font-medium transition-all duration-150",
                mode === "plan"
                  ? "bg-primary text-primary-foreground shadow-sm shadow-primary/30"
                  : "text-muted-foreground hover:text-foreground"
              )}
            >
              Plan
            </button>
          </div>

          {/* Right side: attach + send/stop */}
          <div className="flex items-center gap-1.5">
            <ComposerPrimitive.AddAttachment asChild>
              <TooltipIconButton
                tooltip="Attach file (images, PDF, .txt, .md)"
                side="top"
                type="button"
                variant="ghost"
                size="icon"
                className="size-8 rounded-full text-muted-foreground/60 hover:text-foreground"
                aria-label="Attach file"
              >
                <PaperclipIcon className="size-4" />
              </TooltipIconButton>
            </ComposerPrimitive.AddAttachment>

            <AuiIf condition={(s) => !s.thread.isRunning}>
              <ComposerPrimitive.Send asChild>
                <button
                  type="button"
                  className="size-8 rounded-xl flex items-center justify-center shadow-md shadow-primary/25 transition-all duration-150 hover:scale-105 hover:shadow-primary/40 disabled:opacity-40 disabled:scale-100 disabled:shadow-none"
                  style={{ background: "linear-gradient(135deg, oklch(0.62 0.22 265), oklch(0.55 0.22 295))" }}
                  aria-label="Send message"
                >
                  <ArrowUpIcon className="size-4 text-white" />
                </button>
              </ComposerPrimitive.Send>
            </AuiIf>
            <AuiIf condition={(s) => s.thread.isRunning}>
              <ComposerPrimitive.Cancel asChild>
                <button
                  type="button"
                  className="size-8 rounded-xl flex items-center justify-center shadow-md transition-all duration-150 hover:scale-105"
                  style={{ background: "linear-gradient(135deg, oklch(0.62 0.22 265), oklch(0.55 0.22 295))" }}
                  aria-label="Stop generating"
                >
                  <SquareIcon className="size-3 fill-white text-white" />
                </button>
              </ComposerPrimitive.Cancel>
            </AuiIf>
          </div>
        </div>
      </ComposerPrimitive.AttachmentDropzone>

      <p className="mt-2 text-center text-[10px] text-muted-foreground/35 select-none">
        TAGH DevOps can make mistakes. Review important outputs.
      </p>
    </ComposerPrimitive.Root>
  );
};

// ── Worker progress card ──────────────────────────────────────────────────────

interface CardStep {
  name: string;
  status: "pending" | "running" | "done" | "failed" | "skipped";
  detail?: string;
}
interface CardData {
  title: string;
  elapsed: number;
  overall_status: "running" | "done" | "failed";
  steps: CardStep[];
  footer?: string;
  session_id?: string;
  stream_buffer?: string;
  buttons?: Array<Array<{ label: string; action_id: string }>>;
}

const AgentLogPanel: FC<{ text: string; isRunning: boolean }> = ({ text, isRunning }) => {
  const [expanded, setExpanded] = useState(true);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (scrollRef.current && expanded) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [text, expanded]);

  return (
    <div className="mt-3 border-t border-border/50 pt-2">
      <button
        onClick={() => setExpanded((e) => !e)}
        className="flex items-center gap-1 text-xs text-foreground/60 hover:text-foreground/90 transition-colors w-full text-left"
      >
        {/* collapsed = right (→), expanded = down (↓) */}
        <ChevronDownIcon className={cn("size-3 transition-transform shrink-0", !expanded && "rotate-90")} />
        <span className="font-medium">Agent log</span>
        {isRunning && (
          <span className={cn("ml-1 inline-block size-1.5 rounded-full bg-primary", expanded ? "animate-pulse" : "opacity-70")} />
        )}
      </button>
      {expanded && (
        <div
          ref={scrollRef}
          className="mt-1.5 text-xs font-mono text-foreground/75 whitespace-pre-wrap max-h-44 overflow-y-auto rounded bg-black/25 dark:bg-black/50 p-2 leading-relaxed"
        >
          {text}
        </div>
      )}
    </div>
  );
};

const StepIcon: FC<{ status: CardStep["status"] }> = ({ status }) => {
  if (status === "done" || status === "skipped") return (
    <div className="size-4 rounded-full bg-green-500/15 border border-green-500/40 flex items-center justify-center shrink-0 mt-0.5">
      <CheckIcon className="size-2.5 text-green-600 dark:text-green-400" />
    </div>
  );
  if (status === "running") return (
    <svg className="animate-spin size-4 text-primary shrink-0 mt-0.5" fill="none" viewBox="0 0 24 24">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
    </svg>
  );
  if (status === "failed") return (
    <div className="size-4 rounded-full bg-destructive/15 border border-destructive/40 flex items-center justify-center shrink-0 mt-0.5">
      <span className="text-[9px] text-destructive font-bold leading-none">✕</span>
    </div>
  );
  // pending
  return <div className="size-4 rounded-full border border-border shrink-0 mt-0.5" />;
};

const WorkerProgressCard: FC<{ card: CardData }> = ({ card }) => {
  // Local cancelled state — immediately updates card when user clicks Stop,
  // without waiting for the backend stream (which gets cut and never sends a final card).
  const [localCancelled, setLocalCancelled] = useState(false);

  const handleCancel = async () => {
    setLocalCancelled(true);
    try {
      await fetch(`/api/threads/${card.session_id}/cancel`, { method: "POST", credentials: "include" });
    } catch { /* best-effort */ }
  };

  const total = card.steps.length;
  const done = card.steps.filter((s) => s.status === "done" || s.status === "skipped").length;
  const failed = card.steps.filter((s) => s.status === "failed").length;
  const pct = total > 0 ? Math.round(((done + failed) / total) * 100) : 0;
  const isDone = card.overall_status === "done";
  // isFailed only on terminal failure or user-cancelled — NOT on partial step failures while running,
  // because the orchestrator may self-heal and keep going after a step failure.
  const isFailed = card.overall_status === "failed" || localCancelled;
  // isStillRunning: the job hasn't reached a terminal state yet — Stop button should be visible.
  const isStillRunning = card.overall_status === "running";

  return (
    <div className={cn(
      "rounded-xl border p-4 text-sm my-1",
      isDone && "border-green-500/20",
      isFailed && "border-destructive/20",
      !isDone && !isFailed && "border-border",
    )}>
      {/* Header */}
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          {isDone ? (
            <div className="size-4 rounded-full bg-green-500 flex items-center justify-center">
              <CheckIcon className="size-2.5 text-white" />
            </div>
          ) : isFailed ? (
            <div className="size-4 rounded-full bg-destructive" />
          ) : (
            <svg className="animate-spin size-4 text-primary" fill="none" viewBox="0 0 24 24">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
            </svg>
          )}
          <span className="font-semibold text-foreground">{card.title}</span>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-[11px] font-mono px-1.5 py-0.5 rounded bg-muted/60 text-foreground/50">{card.elapsed}s</span>
          {isStillRunning && card.session_id && (
            <button
              onClick={handleCancel}
              className="text-xs px-2 py-0.5 rounded border border-border text-foreground/50 hover:border-destructive hover:text-destructive transition-colors"
              title="Stop this job"
            >
              Stop
            </button>
          )}
        </div>
      </div>

      {/* Progress bar */}
      <div className="h-1.5 rounded-full bg-border mb-3 overflow-hidden">
        <div
          className={cn(
            "h-full rounded-full transition-all duration-700",
            isDone ? "bg-green-500" : isFailed ? "bg-destructive" : "bg-primary",
          )}
          style={{ width: `${isDone ? 100 : pct}%` }}
        />
      </div>

      {/* Steps */}
      <div className="space-y-2">
        {card.steps.map((step, i) => (
          <div key={i} className="flex items-start gap-2">
            <StepIcon status={step.status} />
            <div className="flex-1 min-w-0">
              <span className={cn(
                "text-[13px] leading-snug",
                (step.status === "done" || step.status === "skipped") && "text-foreground/90",
                step.status === "running" && "text-foreground font-medium",
                step.status === "pending" && "text-foreground/45",
                step.status === "failed" && "text-destructive",
              )}>
                {step.name}
              </span>
              {step.detail && (
                <div className="mt-0.5 text-[11px] text-foreground/55 leading-snug break-words">
                  {step.detail.startsWith("https://") || step.detail.startsWith("http://") ? (
                    <a
                      href={step.detail}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-primary/80 hover:text-primary underline underline-offset-2 transition-colors"
                    >
                      {step.detail}
                    </a>
                  ) : step.detail}
                </div>
              )}
            </div>
          </div>
        ))}
      </div>

      {card.footer && (
        <div className="mt-3 pt-3 border-t border-border/50">
          {card.footer.startsWith("https://") || card.footer.startsWith("http://") ? (
            <a
              href={card.footer}
              target="_blank"
              rel="noopener noreferrer"
              onClick={(e) => e.stopPropagation()}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-primary text-primary-foreground text-xs font-medium hover:bg-primary/90 transition-colors"
            >
              Open App
              <ExternalLinkIcon className="size-3" />
            </a>
          ) : (
            <span className="text-xs text-muted-foreground break-all">{card.footer}</span>
          )}
        </div>
      )}

      {card.stream_buffer && (
        <AgentLogPanel text={card.stream_buffer} isRunning={!isDone && !isFailed} />
      )}

      {card.buttons && card.buttons.length > 0 && (() => {
        // Web chat only renders task-level decision buttons. Project lifecycle
        // actions (relink, bootstrap, docker_up/down) are reached via the agent —
        // the user just asks in natural language. Nav buttons belong in bots.
        const WEB_ALLOWED_PREFIXES = [
          "retry_task:",
          "discard_task:",
          "approve_plan:",
          "reject_plan:",
          "approve_diff:",
          "reject_diff:",
          "create_pr:",
        ];
        const webButtons = card.buttons!.flat().filter((btn) =>
          WEB_ALLOWED_PREFIXES.some((p) => btn.action_id.startsWith(p))
        );
        if (webButtons.length === 0) return null;
        return (
        <div className="mt-3 pt-3 border-t border-border/50 flex flex-wrap gap-2">
          {webButtons.map((btn) => (
            <button
              key={btn.action_id}
              onClick={async () => {
                if (!card.session_id) return;
                try {
                  await fetch(`/api/threads/${card.session_id}/action`, {
                    method: "POST",
                    credentials: "include",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ action_id: btn.action_id }),
                  });
                } catch { /* best-effort */ }
              }}
              className={cn(
                "px-3 py-1.5 rounded-lg text-xs font-medium transition-colors",
                btn.action_id.includes("discard") || btn.action_id.includes("cancel")
                  ? "border border-destructive/40 text-destructive hover:bg-destructive/10"
                  : "border border-border/60 text-foreground/80 hover:bg-muted"
              )}
            >
              {btn.label}
            </button>
          ))}
        </div>
        );
      })()}
    </div>
  );
};

function formatMsgTime(date: Date | undefined): string {
  if (!date) return "";
  return date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

const AssistantMessage: FC = () => {
  const isRunning = useAuiState((s) => s.message.status?.type === "running");
  const createdAt = useAuiState((s) => (s.message as { createdAt?: Date }).createdAt);
  // Read text directly from state — bypasses MessagePrimitive.Parts and the
  // PartByIndexProvider → SmoothContextProvider → useSmoothStatus chain that
  // caused React error #185 (conditional zustand hook call = unstable hook count).
  const text = useAuiState((s) => {
    const textPart = s.message.content.find((p) => p.type === "text");
    return (textPart as { type: "text"; text: string })?.text ?? "";
  });
  const isEmpty = !text;
  // __LOADING__ = worker placeholder written before first progress_card heartbeat.
  // Render as a spinner so it's visible after page refresh but doesn't show raw text.
  const isLoadingPlaceholder = text === "__LOADING__";
  const progressCard = (() => {
    if (!text.startsWith("__PROGRESS_CARD__")) return null;
    try { return JSON.parse(text.slice("__PROGRESS_CARD__".length)) as CardData; }
    catch { return null; }
  })();

  return (
    <MessagePrimitive.Root
      className="fade-in slide-in-from-bottom-1 group/msg relative mx-auto w-full max-w-(--thread-max-width) animate-in py-3 duration-150"
      data-role="assistant"
    >
      <div className="wrap-break-word px-2 text-foreground leading-relaxed">
        {isRunning && isEmpty && !progressCard && <TinkeringIndicator />}
        {isRunning && !progressCard && <ThinkingPanel />}
        {progressCard ? (
          <WorkerProgressCard card={progressCard} />
        ) : isLoadingPlaceholder ? (
          <TinkeringIndicator />
        ) : (
          <StandaloneMarkdown text={text} />
        )}
        <MessagePrimitive.Error>
          <ErrorPrimitive.Root className="mt-2 rounded-md border border-destructive bg-destructive/10 p-3 text-destructive text-sm">
            <ErrorPrimitive.Message className="line-clamp-2" />
          </ErrorPrimitive.Root>
        </MessagePrimitive.Error>
      </div>
      {/* Fixed-height row so icons appearing/disappearing never shift the layout */}
      <div className="mt-1 ml-2 h-7 flex items-center gap-2">
        <BranchPicker />
        <AssistantActionBar />
        {!isRunning && createdAt && (
          <span className="ml-auto pr-1 text-[11px] text-muted-foreground/50 tabular-nums select-none">
            {formatMsgTime(createdAt)}
          </span>
        )}
      </div>
    </MessagePrimitive.Root>
  );
};

const AssistantActionBar: FC = () => {
  return (
    <ActionBarPrimitive.Root
      hideWhenRunning
      autohide="not-last"
      autohideFloat="single-branch"
      className="col-start-3 row-start-2 -ml-1 flex gap-1 text-muted-foreground transition-opacity duration-200 data-[floating]:opacity-0 data-[floating]:group-hover/msg:opacity-100"
    >
      <ActionBarPrimitive.Copy asChild>
        <TooltipIconButton tooltip="Copy">
          <AuiIf condition={(s) => s.message.isCopied}>
            <CheckIcon />
          </AuiIf>
          <AuiIf condition={(s) => !s.message.isCopied}>
            <CopyIcon />
          </AuiIf>
        </TooltipIconButton>
      </ActionBarPrimitive.Copy>
      <ActionBarPrimitive.Reload asChild>
        <TooltipIconButton tooltip="Refresh">
          <RefreshCwIcon />
        </TooltipIconButton>
      </ActionBarPrimitive.Reload>
    </ActionBarPrimitive.Root>
  );
};

const UserMessage: FC = () => {
  const createdAt = useAuiState((s) => (s.message as { createdAt?: Date }).createdAt);
  return (
    <MessagePrimitive.Root
      className="fade-in slide-in-from-bottom-1 mx-auto grid w-full max-w-(--thread-max-width) animate-in auto-rows-auto grid-cols-[minmax(72px,1fr)_auto] content-start gap-y-2 px-2 py-3 duration-150 [&:where(>*)]:col-start-2"
      data-role="user"
    >
      <div className="relative col-start-2 min-w-0">
        <div className="wrap-break-word peer rounded-2xl bg-muted px-4 py-2.5 text-foreground empty:hidden">
          <MessagePrimitive.Parts />
        </div>
        <div className="absolute top-1/2 left-0 -translate-x-full -translate-y-1/2 pr-2 peer-empty:hidden">
          <UserActionBar />
        </div>
      </div>
      <BranchPicker className="col-span-full col-start-1 row-start-3 -mr-1 justify-end" />
      {createdAt && (
        <div className="col-start-2 flex justify-end pr-1">
          <span className="text-[11px] text-muted-foreground/40 tabular-nums select-none">
            {formatMsgTime(createdAt)}
          </span>
        </div>
      )}
    </MessagePrimitive.Root>
  );
};

const UserActionBar: FC = () => {
  return (
    <ActionBarPrimitive.Root hideWhenRunning autohide="not-last" className="flex flex-col items-end">
      <ActionBarPrimitive.Edit asChild>
        <TooltipIconButton tooltip="Edit" className="p-4">
          <PencilIcon />
        </TooltipIconButton>
      </ActionBarPrimitive.Edit>
    </ActionBarPrimitive.Root>
  );
};

const EditComposer: FC = () => {
  return (
    <MessagePrimitive.Root className="mx-auto flex w-full max-w-(--thread-max-width) flex-col px-2 py-3">
      <ComposerPrimitive.Root className="ml-auto flex w-full max-w-[85%] flex-col rounded-2xl bg-muted">
        <ComposerPrimitive.Input
          className="min-h-14 w-full resize-none bg-transparent p-4 text-foreground text-sm outline-none"
          autoFocus
        />
        <div className="mx-3 mb-3 flex items-center gap-2 self-end">
          <ComposerPrimitive.Cancel asChild>
            <Button variant="ghost" size="sm">Cancel</Button>
          </ComposerPrimitive.Cancel>
          <ComposerPrimitive.Send asChild>
            <Button size="sm">Update</Button>
          </ComposerPrimitive.Send>
        </div>
      </ComposerPrimitive.Root>
    </MessagePrimitive.Root>
  );
};

const BranchPicker: FC<BranchPickerPrimitive.Root.Props> = ({ className, ...rest }) => {
  return (
    <BranchPickerPrimitive.Root
      hideWhenSingleBranch
      className={cn("mr-2 -ml-2 inline-flex items-center text-muted-foreground text-xs", className)}
      {...rest}
    >
      <BranchPickerPrimitive.Previous asChild>
        <TooltipIconButton tooltip="Previous">
          <ChevronLeftIcon />
        </TooltipIconButton>
      </BranchPickerPrimitive.Previous>
      <span className="font-medium">
        <BranchPickerPrimitive.Number /> / <BranchPickerPrimitive.Count />
      </span>
      <BranchPickerPrimitive.Next asChild>
        <TooltipIconButton tooltip="Next">
          <ChevronRightIcon />
        </TooltipIconButton>
      </BranchPickerPrimitive.Next>
    </BranchPickerPrimitive.Root>
  );
};
