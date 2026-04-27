// Interactive card for failure / cap-exceeded / generic confirm /
// provisioning.
//
// Renders above the message composer when the backend emits one of:
//
//   instance_failed                  → kind="failed"
//   instance_limit_exceeded          → kind="cap_exceeded" (per_user_cap)
//                                    → kind="cap_exceeded" (platform_capacity)
//   confirm                          → kind="confirm" (e.g. /terminate prompt)
//   instance_provisioning            → kind="provisioning"  (plan v2 Change 2 —
//                                      replaces the thin banner pill so
//                                      "the platform is working for me"
//                                      reads at a glance, with a live
//                                      elapsed counter against the ETA.)
//
// CLAUDE.md "No Dead Ends" rule: every TERMINAL card MUST render at
// least one navigation / cancel button. Provisioning is not terminal —
// it auto-replaces on the next event — so it is exempt.
//
// Visual language matches WorkerProgressCard (rounded-xl + border +
// status-tinted accent + small chip on the header right) so the
// platform's cards feel cohesive whether they're showing progress,
// failure, cap-exceeded, or confirm.

import { useEffect, useState } from "react";
import { AlertTriangleIcon, MessageSquareIcon, LockIcon, ChevronRightIcon } from "lucide-react";
import type { CardAction } from "../../types/stream-events";

export type CardKind = "failed" | "cap_exceeded" | "confirm" | "provisioning";

export interface InstanceCardProps {
  kind: CardKind;
  /** Plain-language headline. Unused for `provisioning`. */
  prompt: string;
  /** Two to four buttons. The Cancel / Main Menu button is mandatory
   *  for terminal kinds; `provisioning` ignores this. */
  actions: CardAction[];
  /** Failure-code chip for `kind="failed"`. */
  failureCode?: string;
  /** Variant chip for `kind="cap_exceeded"`. */
  variant?: "per_user_cap" | "platform_capacity";
  /** Instance slug for `kind="provisioning"`. */
  slug?: string;
  /** Cold-boot ETA in seconds for `kind="provisioning"`. */
  etaSeconds?: number;
  /** Wall-clock start (Date.now() at first emit) for the elapsed ticker. */
  startedAtMs?: number;
  /** Optional title-lookup so cap_exceeded can render real chat titles
   *  instead of bare "#36". Keys are stringified chat IDs. */
  threadTitles?: Record<string, string>;
  /** Per-user cap (e.g. 3) for the cap_exceeded chip. */
  cap?: number;
  /** Click handler — gets the action's id (or link) so the parent can
   *  POST it back through the same chat-text protocol the backend
   *  switch matches on (e.g. ``end_session_confirm:42``). */
  onAction: (action: CardAction) => void;
}

const HEADER: Record<CardKind, { label: string; tone: string }> = {
  failed: { label: "Environment failed", tone: "text-red-400" },
  cap_exceeded: { label: "Chat limit reached", tone: "text-amber-400" },
  confirm: { label: "Are you sure?", tone: "text-foreground" },
  provisioning: { label: "Spinning up your environment", tone: "text-blue-400" },
};

function ProvisioningBody({
  slug,
  etaSeconds,
  startedAtMs,
}: {
  slug?: string;
  etaSeconds?: number;
  startedAtMs?: number;
}) {
  // Live elapsed ticker. Honest counter, no fake step animation —
  // step-level worker push is deferred (plan v2 "out of scope").
  const [elapsed, setElapsed] = useState<number>(0);
  useEffect(() => {
    const start = startedAtMs ?? Date.now();
    const id = setInterval(() => {
      setElapsed(Math.floor((Date.now() - start) / 1000));
    }, 1000);
    return () => clearInterval(id);
  }, [startedAtMs]);

  const eta = etaSeconds ?? 90;
  const overrun = elapsed > eta;

  return (
    <div className="mt-2 space-y-2">
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <div
          className="size-2 rounded-full bg-blue-400 animate-pulse"
          aria-hidden
        />
        <span>
          {overrun
            ? `${elapsed}s elapsed (over ETA — still working, hang tight)`
            : `${elapsed}s elapsed · ETA ~${eta}s`}
        </span>
      </div>
      {slug ? (
        <div className="font-mono text-[11px] text-muted-foreground/80">
          {slug}
        </div>
      ) : null}
      <p className="text-sm text-foreground/85 leading-relaxed">
        I'll share the URL the moment it's live. Your next message will
        also see the live env state.
      </p>
    </div>
  );
}

function HeaderIcon({ kind }: { kind: CardKind }) {
  if (kind === "provisioning") {
    return (
      <svg className="animate-spin size-4 text-blue-400" fill="none" viewBox="0 0 24 24" aria-hidden>
        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
        <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
      </svg>
    );
  }
  if (kind === "failed") return <AlertTriangleIcon className="size-4 text-red-400 shrink-0" aria-hidden />;
  if (kind === "cap_exceeded") return <LockIcon className="size-4 text-amber-400 shrink-0" aria-hidden />;
  return <AlertTriangleIcon className="size-4 text-foreground/70 shrink-0" aria-hidden />;
}

/** Pull a chat ID from action.link "/chat?thread=36" or action.action_id "open:36". */
function _chatIdFromAction(a: CardAction): string | null {
  if (a.link) {
    const m = a.link.match(/thread=(\d+)/);
    if (m) return m[1];
  }
  if (a.action_id) {
    const m = a.action_id.match(/(\d+)$/);
    if (m) return m[1];
  }
  return null;
}

function CapExceededBody({
  prompt,
  variant,
  actions,
  threadTitles,
  onAction,
}: {
  prompt: string;
  variant?: "per_user_cap" | "platform_capacity";
  actions: CardAction[];
  threadTitles?: Record<string, string>;
  onAction: (a: CardAction) => void;
}) {
  // Per-user cap: split the chat-link actions out into a list, the
  // "Main Menu" stays as a button at the bottom. Platform-capacity
  // variant has no chat list — just the prompt + Main Menu.
  const chatActions = variant === "per_user_cap"
    ? actions.filter((a) => _chatIdFromAction(a) != null)
    : [];
  const otherActions = actions.filter((a) => !chatActions.includes(a));
  return (
    <>
      <p className="text-sm text-foreground/85 leading-relaxed mt-2">{prompt}</p>
      {chatActions.length > 0 ? (
        <div className="mt-3 rounded-lg border border-border/70 overflow-hidden">
          {chatActions.map((a, i) => {
            const cid = _chatIdFromAction(a)!;
            const title = threadTitles?.[cid] || `Chat #${cid}`;
            return (
              <button
                key={i}
                type="button"
                onClick={() => onAction(a)}
                className="group flex items-center justify-between gap-3 w-full px-3 py-2 text-left bg-background/30 hover:bg-amber-500/5 border-b border-border/40 last:border-b-0 transition-colors"
              >
                <span className="flex items-center gap-2 min-w-0">
                  <MessageSquareIcon className="size-3.5 text-muted-foreground shrink-0" aria-hidden />
                  <span className="text-sm text-foreground truncate">{title}</span>
                  <span className="text-[10px] font-mono text-muted-foreground/70 shrink-0">#{cid}</span>
                </span>
                <ChevronRightIcon className="size-4 text-muted-foreground/50 group-hover:text-amber-400 transition-colors shrink-0" aria-hidden />
              </button>
            );
          })}
        </div>
      ) : null}
      {otherActions.length > 0 ? (
        <div className="mt-3 flex flex-wrap gap-2 justify-end">
          {otherActions.map((a, i) => (
            <button
              key={i}
              type="button"
              onClick={() => onAction(a)}
              className="rounded-md border border-border hover:bg-muted text-xs font-medium px-3 py-1.5 transition-colors"
            >
              {a.label}
            </button>
          ))}
        </div>
      ) : null}
    </>
  );
}

const CARD_BORDER: Record<CardKind, string> = {
  failed: "border-red-500/25",
  cap_exceeded: "border-amber-500/25",
  confirm: "border-border",
  provisioning: "border-blue-500/25",
};

export function InstanceCard({
  kind,
  prompt,
  actions,
  failureCode,
  variant,
  slug,
  etaSeconds,
  startedAtMs,
  threadTitles,
  cap,
  onAction,
}: InstanceCardProps) {
  const h = HEADER[kind];
  // Right-side chip mirrors WorkerProgressCard's elapsed-counter pill —
  // gives every card a consistent header shape. Cap-exceeded shows the
  // active/cap ratio; failed shows the failure code; confirm is bare.
  const _activeCount = actions.filter((a) => _chatIdFromAction(a) != null).length;
  const chip =
    kind === "cap_exceeded" && variant === "per_user_cap"
      ? `${_activeCount}/${cap ?? _activeCount}`
      : kind === "failed" && failureCode
      ? failureCode
      : null;
  return (
    <div className={`rounded-xl border ${CARD_BORDER[kind]} bg-card p-3 sm:p-4 text-sm my-1 w-full min-w-0`}>
      <div className="flex items-start justify-between gap-2 mb-2">
        <div className="flex items-center gap-2 min-w-0 flex-1">
          <HeaderIcon kind={kind} />
          <span className={`font-semibold ${h.tone} break-words [overflow-wrap:anywhere] min-w-0`}>
            {h.label}
          </span>
        </div>
        {chip ? (
          <span className="text-[11px] font-mono px-1.5 py-0.5 rounded-md bg-muted/70 text-foreground/70 tabular-nums shrink-0">
            {chip}
          </span>
        ) : null}
      </div>
      {kind === "provisioning" ? (
        <ProvisioningBody slug={slug} etaSeconds={etaSeconds} startedAtMs={startedAtMs} />
      ) : kind === "cap_exceeded" ? (
        <CapExceededBody
          prompt={prompt}
          variant={variant}
          actions={actions}
          threadTitles={threadTitles}
          onAction={onAction}
        />
      ) : (
        <>
          <p className="text-sm text-foreground/85 leading-relaxed mt-1">{prompt}</p>
          {actions.length > 0 ? (
            <div className="mt-3 flex flex-wrap gap-2">
              {actions.map((a, i) => (
                <button
                  key={i}
                  type="button"
                  onClick={() => onAction(a)}
                  className={
                    a.style === "danger"
                      ? "rounded-md bg-red-500/15 hover:bg-red-500/25 text-red-300 text-xs font-medium px-3 py-1.5 transition-colors"
                      : a.style === "primary"
                      ? "rounded-md bg-blue-500 hover:bg-blue-600 text-white text-xs font-medium px-3 py-1.5 transition-colors"
                      : "rounded-md border border-border hover:bg-muted text-xs font-medium px-3 py-1.5 transition-colors"
                  }
                >
                  {a.label}
                </button>
              ))}
            </div>
          ) : null}
        </>
      )}
    </div>
  );
}
