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

import { useEffect, useState } from "react";
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
  /** Click handler — gets the action's id (or link) so the parent can
   *  POST it back through the same chat-text protocol the backend
   *  switch matches on (e.g. ``end_session_confirm:42``). */
  onAction: (action: CardAction) => void;
}

const HEADER: Record<CardKind, { label: string; tone: string }> = {
  failed: { label: "Environment failed", tone: "text-red-500" },
  cap_exceeded: { label: "Can't start a new environment", tone: "text-amber-500" },
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

export function InstanceCard({
  kind,
  prompt,
  actions,
  failureCode,
  variant,
  slug,
  etaSeconds,
  startedAtMs,
  onAction,
}: InstanceCardProps) {
  const h = HEADER[kind];
  return (
    <div className="rounded-lg border border-border bg-card p-4 shadow-sm">
      <div className="flex items-start justify-between gap-3 mb-2">
        <div className="flex items-center gap-2">
          {kind === "provisioning" ? (
            <div
              className="size-3 rounded-full border-2 border-blue-400 border-t-transparent animate-spin"
              aria-hidden
            />
          ) : null}
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
      </div>
      {kind === "provisioning" ? (
        <ProvisioningBody
          slug={slug}
          etaSeconds={etaSeconds}
          startedAtMs={startedAtMs}
        />
      ) : (
        <p className="text-sm text-foreground/90 leading-relaxed">{prompt}</p>
      )}
      {kind !== "provisioning" && actions.length > 0 ? (
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
    </div>
  );
}
