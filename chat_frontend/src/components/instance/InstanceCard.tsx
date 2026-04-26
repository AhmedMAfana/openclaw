// Interactive card for failure / cap-exceeded / generic confirm.
//
// Renders above the message composer when the backend emits one of:
//
//   instance_failed                  → kind="failed"
//   instance_limit_exceeded          → kind="cap_exceeded" (per_user_cap)
//                                    → kind="cap_exceeded" (platform_capacity)
//   confirm                          → kind="confirm" (e.g. /terminate prompt)
//
// CLAUDE.md "No Dead Ends" rule: every card MUST render at least one
// navigation / cancel button. The shape comes from the backend's
// `actions` array per the JSON Schema; this component just renders.

import type { CardAction } from "../../types/stream-events";

export type CardKind = "failed" | "cap_exceeded" | "confirm";

export interface InstanceCardProps {
  kind: CardKind;
  /** Plain-language headline. */
  prompt: string;
  /** Two to four buttons. The Cancel / Main Menu button is mandatory. */
  actions: CardAction[];
  /** Failure-code chip for `kind="failed"`. */
  failureCode?: string;
  /** Variant chip for `kind="cap_exceeded"`. */
  variant?: "per_user_cap" | "platform_capacity";
  /** Click handler — gets the action's id (or link) so the parent can
   *  POST it back through the same chat-text protocol the backend
   *  switch matches on (e.g. ``end_session_confirm:42``). */
  onAction: (action: CardAction) => void;
}

const HEADER: Record<CardKind, { label: string; tone: string }> = {
  failed: { label: "Environment failed", tone: "text-red-500" },
  cap_exceeded: { label: "Can't start a new environment", tone: "text-amber-500" },
  confirm: { label: "Are you sure?", tone: "text-foreground" },
};

export function InstanceCard({
  kind,
  prompt,
  actions,
  failureCode,
  variant,
  onAction,
}: InstanceCardProps) {
  const h = HEADER[kind];
  return (
    <div className="rounded-lg border border-border bg-card p-4 shadow-sm">
      <div className="flex items-start justify-between gap-3 mb-2">
        <div>
          <div className={`text-sm font-semibold ${h.tone}`}>{h.label}</div>
          {failureCode ? (
            <div className="mt-0.5 inline-block rounded bg-muted px-1.5 py-0.5 text-[10px] font-mono uppercase tracking-wide text-muted-foreground">
              {failureCode}
            </div>
          ) : null}
          {variant ? (
            <div className="mt-0.5 inline-block rounded bg-muted px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
              {variant.replace(/_/g, " ")}
            </div>
          ) : null}
        </div>
      </div>
      <p className="text-sm text-foreground/90 leading-relaxed">{prompt}</p>
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
    </div>
  );
}
