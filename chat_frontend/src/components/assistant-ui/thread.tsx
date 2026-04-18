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
} from "@assistant-ui/react";
import {
  ArrowDownIcon,
  ArrowUpIcon,
  CheckIcon,
  ChevronDownIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  CopyIcon,
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

const ThreadWelcome: FC = () => {
  return (
    <div className="mx-auto my-auto flex w-full max-w-(--thread-max-width) grow flex-col">
      <div className="flex w-full grow flex-col items-center justify-center">
        <div className="flex size-full flex-col justify-center px-4">
          <h1 className="font-semibold text-2xl">Hello there!</h1>
          <p className="text-muted-foreground text-xl">How can I help you today?</p>
        </div>
      </div>
    </div>
  );
};

const ComposerAttachmentPreview: FC = () => (
  <ComposerPrimitive.Attachments>
    <ComposerPrimitive.AttachmentByIndex>
      <AttachmentPrimitive.Root className="relative flex items-center gap-1.5 rounded-lg border bg-muted px-2 py-1 text-xs">
        <AttachmentPrimitive.unstable_Thumb className="size-8 rounded object-cover" />
        <AttachmentPrimitive.Name className="max-w-[100px] truncate text-foreground" />
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
    </ComposerPrimitive.AttachmentByIndex>
  </ComposerPrimitive.Attachments>
);

const Composer: FC = () => {
  const { mode, setMode } = useTaskMode();

  return (
    <ComposerPrimitive.Root className="relative flex w-full flex-col">
      <ComposerPrimitive.AttachmentDropzone className="relative flex w-full flex-col rounded-(--composer-radius) border bg-background transition-shadow focus-within:border-ring/75 focus-within:ring-2 focus-within:ring-ring/20 data-[drag-over]:border-blue-400 data-[drag-over]:ring-2 data-[drag-over]:ring-blue-400/30">
        {/* Attachment preview strip */}
        <div className="flex flex-wrap gap-2 px-3 pt-2 empty:hidden">
          <ComposerAttachmentPreview />
        </div>

        <ComposerPrimitive.Input
          placeholder="Send a message..."
          className="max-h-32 min-h-10 w-full resize-none bg-transparent px-3 py-2 text-sm outline-none placeholder:text-muted-foreground/80"
          rows={1}
          autoFocus
          aria-label="Message input"
        />

        <div className="flex items-center justify-between px-2 pb-2">
          {/* Mode toggle pills — left side */}
          <div className="flex items-center gap-0.5 rounded-full border border-border bg-muted/40 p-0.5">
            <button
              type="button"
              onClick={() => setMode("quick")}
              title="Quick: start coding immediately, no approval step"
              className={cn(
                "px-2.5 py-1 rounded-full text-xs font-medium transition-colors",
                mode === "quick"
                  ? "bg-background text-foreground shadow-sm"
                  : "text-muted-foreground hover:text-foreground"
              )}
            >
              ⚡ Quick
            </button>
            <button
              type="button"
              onClick={() => setMode("plan")}
              title="Plan: generates a plan for you to approve before coding starts"
              className={cn(
                "px-2.5 py-1 rounded-full text-xs font-medium transition-colors",
                mode === "plan"
                  ? "bg-background text-foreground shadow-sm"
                  : "text-muted-foreground hover:text-foreground"
              )}
            >
              📋 Plan
            </button>
          </div>

          {/* Right side: attach button + send/stop */}
          <div className="flex items-center gap-1">
            {/* Paperclip — opens file picker (images + text files) */}
            <ComposerPrimitive.AddAttachment asChild>
              <TooltipIconButton
                tooltip="Attach file (images, PDF, .txt, .md)"
                side="top"
                type="button"
                variant="ghost"
                size="icon"
                className="size-8 rounded-full text-muted-foreground hover:text-foreground"
                aria-label="Attach file"
              >
                <PaperclipIcon className="size-4" />
              </TooltipIconButton>
            </ComposerPrimitive.AddAttachment>

            <AuiIf condition={(s) => !s.thread.isRunning}>
              <ComposerPrimitive.Send asChild>
                <TooltipIconButton
                  tooltip="Send message"
                  side="bottom"
                  type="button"
                  variant="default"
                  size="icon"
                  className="size-8 rounded-full"
                  aria-label="Send message"
                >
                  <ArrowUpIcon className="size-4" />
                </TooltipIconButton>
              </ComposerPrimitive.Send>
            </AuiIf>
            <AuiIf condition={(s) => s.thread.isRunning}>
              <ComposerPrimitive.Cancel asChild>
                <Button
                  type="button"
                  variant="default"
                  size="icon"
                  className="size-8 rounded-full"
                  aria-label="Stop generating"
                >
                  <SquareIcon className="size-3 fill-current" />
                </Button>
              </ComposerPrimitive.Cancel>
            </AuiIf>
          </div>
        </div>
      </ComposerPrimitive.AttachmentDropzone>
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
}

const AgentLogPanel: FC<{ text: string; isRunning: boolean }> = ({ text, isRunning }) => {
  const [expanded, setExpanded] = useState(false);
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
        className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors w-full text-left"
      >
        <ChevronDownIcon className={cn("size-3 transition-transform shrink-0", !expanded && "-rotate-90")} />
        <span>Agent log</span>
        {isRunning && expanded && (
          <span className="ml-auto inline-block size-1.5 rounded-full bg-primary animate-pulse" />
        )}
      </button>
      {expanded && (
        <div
          ref={scrollRef}
          className="mt-1.5 text-xs font-mono text-foreground/80 whitespace-pre-wrap max-h-44 overflow-y-auto rounded bg-black/25 dark:bg-black/50 p-2 leading-relaxed"
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
  // isFailed drives card styling: overall_status=failed OR user cancelled OR any step failed.
  // NOTE: individual step failures do NOT hide the Stop button — the orchestrator keeps running
  // after partial failures, so we must keep the Stop button visible until overall_status resolves.
  const isFailed = card.overall_status === "failed" || localCancelled || failed > 0;
  // isStillRunning: the job hasn't reached a terminal state yet — Stop button should be visible.
  const isStillRunning = card.overall_status === "running";

  return (
    <div className={cn(
      "rounded-xl border p-4 text-sm my-1",
      isDone && "border-green-500/30 bg-green-50/40 dark:bg-green-950/20",
      isFailed && "border-destructive/30 bg-destructive/5",
      !isDone && !isFailed && "border-border bg-muted/20",
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
          <span className="text-xs font-mono text-muted-foreground">{card.elapsed}s</span>
          {isStillRunning && card.session_id && (
            <button
              onClick={handleCancel}
              className="text-xs px-2 py-0.5 rounded border border-border text-muted-foreground hover:border-destructive hover:text-destructive transition-colors"
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
                "text-sm",
                (step.status === "done" || step.status === "skipped") && "text-foreground",
                step.status === "running" && "text-foreground font-medium",
                step.status === "pending" && "text-muted-foreground",
                step.status === "failed" && "text-destructive",
              )}>
                {step.name}
              </span>
              {step.detail && (
                <span className="ml-2 text-xs text-muted-foreground">{step.detail}</span>
              )}
            </div>
          </div>
        ))}
      </div>

      {card.footer && (
        <div className="mt-3 pt-3 border-t border-border/50">
          {card.footer.startsWith("https://") ? (
            <a
              href={card.footer}
              target="_blank"
              rel="noopener noreferrer"
              onClick={(e) => e.stopPropagation()}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-primary text-primary-foreground text-xs font-medium hover:bg-primary/90 transition-colors"
            >
              Open App ↗
            </a>
          ) : (
            <span className="text-xs text-muted-foreground break-all">{card.footer}</span>
          )}
        </div>
      )}

      {card.stream_buffer && (
        <AgentLogPanel text={card.stream_buffer} isRunning={!isDone && !isFailed} />
      )}
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
