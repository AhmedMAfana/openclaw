import { memo } from "react";
import { CheckIcon, ChevronDownIcon, LoaderIcon, XCircleIcon } from "lucide-react";
import type { ToolCallMessagePartComponent, ToolCallMessagePartStatus } from "@assistant-ui/react";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";

const ToolFallbackImpl: ToolCallMessagePartComponent = ({ toolName, argsText, result, status }) => {
  const statusType = status?.type ?? "complete";
  const isRunning = statusType === "running";
  const isCancelled = status?.type === "incomplete" && (status as ToolCallMessagePartStatus & { reason?: string }).reason === "cancelled";
  const Icon = isRunning ? LoaderIcon : isCancelled ? XCircleIcon : CheckIcon;

  return (
    <Collapsible className={cn("aui-tool-fallback-root w-full rounded-lg border py-3", isCancelled && "border-muted-foreground/30 bg-muted/30")}>
      <CollapsibleTrigger className="flex w-full items-center gap-2 px-4 text-sm">
        <Icon className={cn("size-4 shrink-0", isRunning && "animate-spin")} />
        <span className={cn("grow text-left", isCancelled && "text-muted-foreground line-through")}>
          Used tool: <b>{toolName}</b>
        </span>
        <ChevronDownIcon className="size-4 transition-transform group-data-[state=closed]:-rotate-90" />
      </CollapsibleTrigger>
      <CollapsibleContent className="overflow-hidden">
        <div className="mt-3 flex flex-col gap-2 border-t pt-2">
          {argsText && (
            <div className="px-4">
              <pre className="whitespace-pre-wrap text-xs">{argsText}</pre>
            </div>
          )}
          {result !== undefined && !isCancelled && (
            <div className="border-t border-dashed px-4 pt-2">
              <p className="font-semibold text-xs">Result:</p>
              <pre className="whitespace-pre-wrap text-xs">
                {typeof result === "string" ? result : JSON.stringify(result, null, 2)}
              </pre>
            </div>
          )}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
};

export const ToolFallback = memo(ToolFallbackImpl) as unknown as ToolCallMessagePartComponent;
ToolFallback.displayName = "ToolFallback";
