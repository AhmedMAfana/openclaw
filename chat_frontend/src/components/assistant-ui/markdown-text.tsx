import "@assistant-ui/react-markdown/styles/dot.css";
import {
  type CodeHeaderProps,
  MarkdownTextPrimitive,
  unstable_memoizeMarkdownComponents as memoizeMarkdownComponents,
  useIsMarkdownCodeBlock,
} from "@assistant-ui/react-markdown";
import ReactMarkdown from "react-markdown";
import type { Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { type FC, memo, useState } from "react";
import { CheckIcon, CopyIcon } from "lucide-react";
import { TooltipIconButton } from "@/components/assistant-ui/tooltip-icon-button";
import { cn } from "@/lib/utils";

// ── Context-aware MarkdownText (used inside MessagePrimitive.Parts) ────────────

const MarkdownTextImpl = () => {
  return (
    <MarkdownTextPrimitive
      remarkPlugins={[remarkGfm]}
      className="aui-md"
      components={defaultComponents}
    />
  );
};

export const MarkdownText = memo(MarkdownTextImpl);

const CodeHeader: FC<CodeHeaderProps> = ({ language, code }) => {
  const { isCopied, copyToClipboard } = useCopyToClipboard();
  return (
    <div className="aui-code-header-root mt-2.5 flex items-center justify-between rounded-t-lg border border-border/50 border-b-0 bg-muted/50 px-3 py-1.5 text-xs">
      <span className="font-medium text-muted-foreground lowercase">{language}</span>
      <TooltipIconButton tooltip="Copy" onClick={() => { if (!isCopied && code) copyToClipboard(code); }}>
        {!isCopied ? <CopyIcon /> : <CheckIcon />}
      </TooltipIconButton>
    </div>
  );
};

const useCopyToClipboard = ({ copiedDuration = 3000 }: { copiedDuration?: number } = {}) => {
  const [isCopied, setIsCopied] = useState(false);
  const copyToClipboard = (value: string) => {
    if (!value) return;
    navigator.clipboard.writeText(value).then(() => {
      setIsCopied(true);
      setTimeout(() => setIsCopied(false), copiedDuration);
    });
  };
  return { isCopied, copyToClipboard };
};

const defaultComponents = memoizeMarkdownComponents({
  h1: ({ className, ...props }) => <h1 className={cn("mb-2 font-semibold text-base first:mt-0 last:mb-0", className)} {...props} />,
  h2: ({ className, ...props }) => <h2 className={cn("mt-3 mb-1.5 font-semibold text-sm first:mt-0 last:mb-0", className)} {...props} />,
  h3: ({ className, ...props }) => <h3 className={cn("mt-2.5 mb-1 font-semibold text-sm first:mt-0 last:mb-0", className)} {...props} />,
  p: ({ className, ...props }) => <p className={cn("my-2.5 leading-normal first:mt-0 last:mb-0", className)} {...props} />,
  a: ({ className, ...props }) => <a className={cn("text-primary underline underline-offset-2 hover:text-primary/80", className)} {...props} />,
  ul: ({ className, ...props }) => <ul className={cn("my-2 ml-4 list-disc marker:text-muted-foreground [&>li]:mt-1", className)} {...props} />,
  ol: ({ className, ...props }) => <ol className={cn("my-2 ml-4 list-decimal marker:text-muted-foreground [&>li]:mt-1", className)} {...props} />,
  li: ({ className, ...props }) => <li className={cn("leading-normal", className)} {...props} />,
  blockquote: ({ className, ...props }) => <blockquote className={cn("my-2.5 border-muted-foreground/30 border-l-2 pl-3 text-muted-foreground italic", className)} {...props} />,
  table: ({ className, ...props }) => <table className={cn("my-2 w-full border-separate border-spacing-0", className)} {...props} />,
  th: ({ className, ...props }) => <th className={cn("bg-muted px-2 py-1 text-left font-medium first:rounded-tl-lg last:rounded-tr-lg", className)} {...props} />,
  td: ({ className, ...props }) => <td className={cn("border-muted-foreground/20 border-b border-l px-2 py-1 last:border-r", className)} {...props} />,
  pre: ({ className, ...props }) => <pre className={cn("overflow-x-auto rounded-t-none rounded-b-lg border border-border/50 border-t-0 bg-muted/30 p-3 text-xs leading-relaxed", className)} {...props} />,
  code: function Code({ className, ...props }) {
    const isCodeBlock = useIsMarkdownCodeBlock();
    return (
      <code
        className={cn(!isCodeBlock && "rounded-md border border-border/50 bg-muted/50 px-1.5 py-0.5 font-mono text-[0.85em]", className)}
        {...props}
      />
    );
  },
  CodeHeader,
  hr: ({ className, ...props }) => <hr className={cn("my-2 border-muted-foreground/20", className)} {...props} />,
});

// ── StandaloneMarkdown — zero assistant-ui context dependency ─────────────────
// Used in AssistantMessage to bypass the PartByIndexProvider → SmoothContextProvider
// → useSmoothStatus chain that causes React error #185 (unstable hook count).
// Detects fenced code blocks via the `language-*` className that react-markdown
// injects — no useIsMarkdownCodeBlock hook needed.

const standaloneComponents: Components = {
  h1: ({ className, ...props }) => <h1 className={cn("mb-2 font-semibold text-base first:mt-0 last:mb-0", className)} {...props} />,
  h2: ({ className, ...props }) => <h2 className={cn("mt-3 mb-1.5 font-semibold text-sm first:mt-0 last:mb-0", className)} {...props} />,
  h3: ({ className, ...props }) => <h3 className={cn("mt-2.5 mb-1 font-semibold text-sm first:mt-0 last:mb-0", className)} {...props} />,
  p: ({ className, ...props }) => <p className={cn("my-2.5 leading-normal first:mt-0 last:mb-0", className)} {...props} />,
  a: ({ className, ...props }) => <a className={cn("text-primary underline underline-offset-2 hover:text-primary/80", className)} {...props} />,
  ul: ({ className, ...props }) => <ul className={cn("my-2 ml-4 list-disc marker:text-muted-foreground [&>li]:mt-1", className)} {...props} />,
  ol: ({ className, ...props }) => <ol className={cn("my-2 ml-4 list-decimal marker:text-muted-foreground [&>li]:mt-1", className)} {...props} />,
  li: ({ className, ...props }) => <li className={cn("leading-normal", className)} {...props} />,
  blockquote: ({ className, ...props }) => <blockquote className={cn("my-2.5 border-muted-foreground/30 border-l-2 pl-3 text-muted-foreground italic", className)} {...props} />,
  table: ({ className, ...props }) => <table className={cn("my-2 w-full border-separate border-spacing-0 overflow-auto", className)} {...props} />,
  th: ({ className, ...props }) => <th className={cn("bg-muted px-2 py-1 text-left font-medium first:rounded-tl-lg last:rounded-tr-lg", className)} {...props} />,
  td: ({ className, ...props }) => <td className={cn("border-muted-foreground/20 border-b border-l px-2 py-1 last:border-r", className)} {...props} />,
  pre: ({ className, children, ...props }) => (
    <pre className={cn("overflow-x-auto rounded-b-lg border border-border/50 bg-muted/30 p-3 text-xs leading-relaxed", className)} {...props}>
      {children}
    </pre>
  ),
  code: ({ className, children, ...props }) => {
    // react-markdown sets className="language-*" on fenced code blocks, not inline code
    const isBlock = !!className?.startsWith("language-");
    return (
      <code
        className={cn(
          !isBlock && "rounded-md border border-border/50 bg-muted/50 px-1.5 py-0.5 font-mono text-[0.85em]",
          className,
        )}
        {...props}
      >
        {children}
      </code>
    );
  },
  hr: ({ className, ...props }) => <hr className={cn("my-2 border-muted-foreground/20", className)} {...props} />,
};

// Memo-keyed on its own text — earlier paragraphs of a streaming message stay
// stable while only the last one re-parses per tick, so ReactMarkdown+remarkGfm
// don't re-run across the entire message on every chunk.
const MarkdownParagraph = memo(function MarkdownParagraph({ text }: { text: string }) {
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={standaloneComponents}>
      {text}
    </ReactMarkdown>
  );
});

export const StandaloneMarkdown = memo(function StandaloneMarkdown({ text }: { text: string }) {
  // Split on blank lines so each paragraph is its own memoized subtree. During
  // streaming only the last paragraph's text prop changes → React skips the rest.
  const paragraphs = text.split(/\n{2,}/);
  return (
    <div className="aui-md">
      {paragraphs.map((p, i) => (
        <MarkdownParagraph key={i} text={p} />
      ))}
    </div>
  );
});
