// Non-interactive banners surfaced for container-mode chats.
//
// Five kinds, all rendered as a single inline pill above the message
// composer:
//
//   provisioning     "Starting up your environment — about 90s"
//   upstream_degraded "Preview URL temporarily unavailable (cloudflare)"
//   busy              "This chat is busy finishing a previous step"
//   terminating       "Ending environment — teardown in progress"
//   retry_started     "Retrying — starting a fresh environment"
//
// Closes the Phase 10 frontend-handler gap surfaced live by the
// pipeline-fitness audit (`stream_event_contract` HIGH findings) on
// 2026-04-24. Every kind has a backend `controller.add_data` event
// site documented in
// specs/001-per-chat-instances/contracts/stream-events.schema.json.

export type BannerKind =
  | "provisioning"
  | "upstream_degraded"
  | "busy"
  | "terminating"
  | "retry_started";

export interface BannerProps {
  kind: BannerKind;
  slug?: string;
  /** ETA seconds for `provisioning`. */
  etaSeconds?: number;
  /** ``{capability: upstream}`` map for `upstream_degraded`. */
  capabilities?: Record<string, string>;
}

const STYLE: Record<BannerKind, { dot: string; bg: string; label: string }> = {
  provisioning: {
    dot: "bg-blue-500 animate-pulse",
    bg: "bg-blue-500/10 border-blue-500/30",
    label: "Starting environment",
  },
  upstream_degraded: {
    dot: "bg-amber-500 animate-pulse",
    bg: "bg-amber-500/10 border-amber-500/30",
    label: "Preview URL degraded",
  },
  busy: {
    dot: "bg-amber-500",
    bg: "bg-amber-500/10 border-amber-500/30",
    label: "Chat busy",
  },
  terminating: {
    dot: "bg-zinc-400 animate-pulse",
    bg: "bg-zinc-500/10 border-zinc-500/30",
    label: "Ending environment",
  },
  retry_started: {
    dot: "bg-emerald-500 animate-pulse",
    bg: "bg-emerald-500/10 border-emerald-500/30",
    label: "Retrying",
  },
};

export function InstanceBanner({ kind, slug, etaSeconds, capabilities }: BannerProps) {
  const s = STYLE[kind];
  const detail = (() => {
    switch (kind) {
      case "provisioning":
        return etaSeconds ? `about ${etaSeconds}s` : "in progress";
      case "upstream_degraded":
        if (!capabilities || Object.keys(capabilities).length === 0) return "checking…";
        return Object.entries(capabilities)
          .map(([cap, up]) => `${cap} via ${up}`)
          .join(", ");
      case "busy":
        return "finish previous step before sending more";
      case "terminating":
        return "teardown will finish in the background";
      case "retry_started":
        return "fresh environment coming up";
    }
  })();

  return (
    <div
      role="status"
      className={`flex items-center gap-2 rounded-md border px-3 py-1.5 text-xs ${s.bg}`}
    >
      <span className={`size-2 rounded-full ${s.dot}`} aria-hidden />
      <span className="font-medium">{s.label}</span>
      <span className="text-muted-foreground">— {detail}</span>
      {slug ? (
        <span className="ml-auto font-mono text-[10px] text-muted-foreground/70">
          {slug}
        </span>
      ) : null}
    </div>
  );
}
