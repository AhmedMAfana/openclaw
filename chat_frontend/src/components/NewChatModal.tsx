// Mandatory project-pick modal for "New conversation" (plan v2 Change 1).
//
// The bug it kills: a user who clicks "New chat" without a project
// pre-selected ends up with `web_chat_sessions.project_id = NULL`. The
// LLM then runs the no-project-bound system-prompt addendum and (until
// plan v2) gaslights the user with "I'll spin up your env" — but the
// platform CAN'T auto-provision without a project, so nothing happens.
//
// This modal makes it structurally impossible to create a chat without
// a project: the chat row isn't POSTed until the user picks. Cancel
// just dismisses; no DB row is created.
//
// Design note: sorted with active container-mode projects first so the
// most-likely pick is the top row. Only project name + a small mode
// badge are shown — keeps the modal a single tap, not a form.

import { useEffect, useRef } from "react";

interface ProjectOption {
  id: number;
  name: string;
  /** "container" | "docker" | "host" — drives the small badge. */
  mode?: string;
  status?: string;
}

interface NewChatModalProps {
  projects: ProjectOption[];
  onPick: (projectId: number) => void;
  onCancel: () => void;
}

function modeBadgeClasses(mode?: string): string {
  if (mode === "container") return "bg-blue-500/15 text-blue-300";
  if (mode === "docker") return "bg-violet-500/15 text-violet-300";
  if (mode === "host") return "bg-emerald-500/15 text-emerald-300";
  return "bg-muted text-muted-foreground";
}

export function NewChatModal({ projects, onPick, onCancel }: NewChatModalProps) {
  const dialogRef = useRef<HTMLDivElement | null>(null);

  // Sort: active container > active others > inactive.
  const sorted = [...projects].sort((a, b) => {
    const score = (p: ProjectOption) => {
      const active = p.status === "active" ? 1 : 0;
      const container = p.mode === "container" ? 1 : 0;
      return active * 2 + container;
    };
    return score(b) - score(a);
  });

  // ESC closes; focus trap basics — the first project gets initial focus.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    const first = dialogRef.current?.querySelector<HTMLButtonElement>(
      "button[data-project-id]"
    );
    first?.focus();
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Pick a project to start a new chat"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
      onClick={(e) => {
        if (e.target === e.currentTarget) onCancel();
      }}
    >
      <div
        ref={dialogRef}
        className="w-full max-w-md rounded-xl border border-border bg-card p-5 shadow-2xl"
      >
        <div className="mb-3">
          <h2 className="text-base font-semibold text-foreground">
            Start a new chat
          </h2>
          <p className="mt-1 text-xs text-muted-foreground">
            Pick a project — your environment will spin up the moment you
            send the first message.
          </p>
        </div>

        {sorted.length === 0 ? (
          <div className="py-6 text-center text-sm text-muted-foreground">
            You don't have any projects yet. Add one from Settings.
          </div>
        ) : (
          <ul className="max-h-[60vh] space-y-1 overflow-y-auto">
            {sorted.map((p) => (
              <li key={p.id}>
                <button
                  type="button"
                  data-project-id={p.id}
                  onClick={() => onPick(p.id)}
                  className="flex w-full items-center justify-between gap-3 rounded-lg border border-transparent bg-background/40 px-3 py-2.5 text-left transition-colors hover:border-blue-500/40 hover:bg-blue-500/5 focus:border-blue-500/60 focus:outline-none"
                >
                  <span className="text-sm font-medium text-foreground">
                    {p.name}
                  </span>
                  <span className="flex items-center gap-1.5">
                    {p.status && p.status !== "active" ? (
                      <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
                        {p.status}
                      </span>
                    ) : null}
                    {p.mode ? (
                      <span
                        className={`rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${modeBadgeClasses(p.mode)}`}
                      >
                        {p.mode}
                      </span>
                    ) : null}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}

        <div className="mt-4 flex justify-end">
          <button
            type="button"
            onClick={onCancel}
            className="rounded-md border border-border px-3 py-1.5 text-xs font-medium text-foreground/80 hover:bg-muted"
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
