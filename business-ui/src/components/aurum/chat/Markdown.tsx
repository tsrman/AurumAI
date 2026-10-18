"use client";

import * as React from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import type { Element, Root, Text } from "hast";
import { cn } from "@/lib/utils";
import { MermaidDiagram } from "./MermaidDiagram";

// A fenced code block's rendered <code> children are either a single
// string or a mix of strings/inline elements — for a plain ```mermaid
// fence (no inline markdown inside code spans) this is always a plain
// string in practice, but this stays correct if that ever changes.
function extractText(node: React.ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(extractText).join("");
  if (React.isValidElement(node)) {
    const props = node.props as { children?: React.ReactNode };
    return extractText(props.children);
  }
  return "";
}

// Some models (for example DeepSeek) write LaTeX with \[ ... \] and \( ... \) (the
// classic LaTeX convention) rather than remark-math's $$ ... $$ / $ ... $,
// so without this they render as literal escaped-bracket text. Converted
// only once both delimiters of a pair are present — an unclosed \[ during
// streaming is left as plain text until it closes, which is fine.
function convertLatexDelimiters(text: string): string {
  return text
    .replace(/\\\[([\s\S]+?)\\\]/g, (_match, expr: string) => `$$${expr}$$`)
    .replace(/\\\(([\s\S]+?)\\\)/g, (_match, expr: string) => `$${expr}$`);
}

// Appended to the raw markdown source while a message is still streaming, then
// swapped for a <cursor-marker> element by rehypeCursor below — lets the caret
// sit inline at the true end of the (partial) parsed content, instead of after
// a whole extra block like a trailing <div>.
const CURSOR_TOKEN = "";

function isElement(node: Root["children"][number]): node is Element {
  return node.type === "element";
}

// Finds the (single) text node anywhere in the tree containing CURSOR_TOKEN and
// splits it into [before, <cursor-marker>, after]. Searching by content match
// rather than assuming the token ends up in the structurally-last text node —
// GFM's table/autolink tokenizers can reshuffle trailing inline content during
// speculative parsing of a not-yet-complete line, so "last node in the tree"
// isn't reliably where the token lands.
function rehypeCursor() {
  return (tree: Root) => {
    const visit = (node: Root | Element): boolean => {
      for (let i = 0; i < node.children.length; i++) {
        const child = node.children[i];
        if (child.type === "text" && child.value.includes(CURSOR_TOKEN)) {
          const idx = child.value.indexOf(CURSOR_TOKEN);
          const before = child.value.slice(0, idx);
          const after = child.value.slice(idx + CURSOR_TOKEN.length);
          const cursorNode: Element = {
            type: "element",
            tagName: "cursor-marker",
            properties: {},
            children: [],
          };
          const replacement: (Element | Text)[] = [];
          if (before) replacement.push({ type: "text", value: before });
          replacement.push(cursorNode);
          if (after) replacement.push({ type: "text", value: after });
          node.children.splice(i, 1, ...replacement);
          return true;
        }
        if (isElement(child) && visit(child)) return true;
      }
      return false;
    };
    visit(tree);
  };
}

const components: Components = {
  p: ({ children }) => (
    <p className="text-[14.5px] leading-[1.72] text-aurum-text">{children}</p>
  ),
  strong: ({ children }) => (
    <strong className="font-semibold text-aurum-text">{children}</strong>
  ),
  em: ({ children }) => <em className="italic">{children}</em>,
  del: ({ children }) => <del className="text-aurum-text-3">{children}</del>,
  a: ({ children, href }) => (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="text-aurum-accent underline underline-offset-2 hover:text-aurum-accent-ink"
    >
      {children}
    </a>
  ),
  h1: ({ children }) => (
    <h1 className="text-xl font-semibold text-aurum-text">{children}</h1>
  ),
  h2: ({ children }) => (
    <h2 className="text-lg font-semibold text-aurum-text">{children}</h2>
  ),
  h3: ({ children }) => (
    <h3 className="text-base font-semibold text-aurum-text">{children}</h3>
  ),
  h4: ({ children }) => (
    <h4 className="text-[14.5px] font-semibold text-aurum-text">
      {children}
    </h4>
  ),
  ul: ({ children }) => (
    <ul className="list-disc space-y-1 pl-5 text-[14.5px] leading-[1.72] text-aurum-text">
      {children}
    </ul>
  ),
  ol: ({ children }) => (
    <ol className="list-decimal space-y-1 pl-5 text-[14.5px] leading-[1.72] text-aurum-text">
      {children}
    </ol>
  ),
  li: ({ children }) => <li className="pl-1">{children}</li>,
  blockquote: ({ children }) => (
    <blockquote className="border-l-2 border-aurum-border pl-3 text-aurum-text-2 italic">
      {children}
    </blockquote>
  ),
  hr: () => <hr className="border-aurum-border" />,
  code: ({ className, children, ...props }) => {
    const isBlock = /language-/.test(className ?? "");
    if (!isBlock) {
      return (
        <code
          className="rounded bg-aurum-surface-2 px-1 py-0.5 font-mono text-[13px] text-aurum-text"
          {...props}
        >
          {children}
        </code>
      );
    }
    return (
      <code className={cn("font-mono text-sm text-white/90", className)} {...props}>
        {children}
      </code>
    );
  },
  pre: ({ children }) => {
    // A fenced code block's rendered tree is <pre><code className="language-x">...
    // — intercept language-mermaid here (before it hits the plain code/pre
    // styling below) and render an actual diagram instead of a code box.
    // Every other language is untouched and keeps rendering exactly as before.
    const child = Array.isArray(children) ? children[0] : children;
    if (
      React.isValidElement(child) &&
      typeof (child.props as { className?: string }).className === "string" &&
      (child.props as { className?: string }).className!.includes("language-mermaid")
    ) {
      const source = extractText((child.props as { children?: React.ReactNode }).children);
      return <MermaidDiagram source={source} />;
    }
    return (
      <pre className="max-h-96 overflow-auto rounded-card bg-[#1e1e1e] p-4">
        {children}
      </pre>
    );
  },
  table: ({ children }) => (
    <div className="overflow-hidden rounded-card border border-aurum-border bg-aurum-surface shadow-card">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">{children}</table>
      </div>
    </div>
  ),
  thead: ({ children }) => <thead className="bg-aurum-surface-2">{children}</thead>,
  tbody: ({ children }) => <tbody>{children}</tbody>,
  tr: ({ children }) => (
    <tr className="border-t border-aurum-border-soft first:border-t-0">
      {children}
    </tr>
  ),
  th: ({ children }) => (
    <th
      scope="col"
      className="px-4 py-2 text-left font-semibold text-aurum-text-2"
    >
      {children}
    </th>
  ),
  td: ({ children }) => <td className="px-4 py-2 text-aurum-text">{children}</td>,
  // @ts-expect-error -- custom hast tag inserted by rehypeCursor, not a real HTML element
  "cursor-marker": () => <span className="aurum-caret" aria-hidden="true" />,
};

export interface MarkdownProps {
  content: string;
  className?: string;
  streaming?: boolean;
}

export function Markdown({ content, className, streaming }: MarkdownProps) {
  const converted = convertLatexDelimiters(content);
  const source = streaming ? converted + CURSOR_TOKEN : converted;
  return (
    <div className={cn("space-y-3", className)}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={streaming ? [rehypeKatex, rehypeCursor] : [rehypeKatex]}
        components={components}
      >
        {source}
      </ReactMarkdown>
    </div>
  );
}
