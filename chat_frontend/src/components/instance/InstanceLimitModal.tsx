/**
 * InstanceLimitModal — full-screen overlay shown when the user hits the
 * per-user instance cap. Lists their running instances (via UserInstanceList)
 * so they can terminate one or navigate to an existing chat.
 */
import { XIcon, AlertTriangleIcon } from "lucide-react";
import { UserInstanceList } from "./UserInstanceList";

interface Props {
  userId: number;
  cap: number;
  onOpenChat: (chatId: number) => void;
  onClose: () => void;
}

export function InstanceLimitModal({ userId, cap, onOpenChat, onClose }: Props) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm">
      <div className="w-full max-w-lg rounded-2xl border border-amber-500/30 bg-card shadow-2xl flex flex-col max-h-[85vh]">
        {/* Header */}
        <div className="flex items-start justify-between gap-4 p-5 border-b border-border shrink-0">
          <div className="flex items-center gap-3">
            <div className="flex size-9 shrink-0 items-center justify-center rounded-full bg-amber-500/15">
              <AlertTriangleIcon className="size-4 text-amber-400" />
            </div>
            <div>
              <h2 className="text-sm font-semibold text-foreground">Instance limit reached</h2>
              <p className="text-xs text-muted-foreground mt-0.5">
                You have {cap} active workspace{cap !== 1 ? "s" : ""} — the maximum allowed.
                Terminate one to start a new chat.
              </p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="shrink-0 p-1.5 rounded-lg text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
          >
            <XIcon className="size-4" />
          </button>
        </div>

        {/* Instance list */}
        <div className="flex-1 overflow-y-auto p-5">
          <UserInstanceList
            userId={userId}
            onOpenChat={(chatId) => { onOpenChat(chatId); onClose(); }}
            onTerminated={() => {
              // After terminating, auto-close so they can retry the new chat
              onClose();
            }}
          />
        </div>
      </div>
    </div>
  );
}
