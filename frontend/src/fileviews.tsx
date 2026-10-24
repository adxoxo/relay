// Views for the Files page: a small Markdown renderer, a lightweight code view,
// a two-way line diff, and size and kind labels. Every renderer builds React
// nodes from text; nothing is inserted as HTML.

import { useMemo, type ReactNode } from "react";
import type { FileKind } from "./types";

// Decimal units, so a size label always agrees with the byte limits (10,000,000 and 50,000,000).
export function fmtSize(bytes: number | null | undefined): string {
  if (bytes == null) return "";
  if (bytes < 1000) return `${bytes} B`;
  if (bytes < 1000000) return `${(bytes / 1000).toFixed(1)} KB`;
  return `${(bytes / 1000000).toFixed(1)} MB`;
}

export const fmtInt = (n: number) => n.toLocaleString("en-US");

export function fmtWhen(epoch: number | null | undefined): string {
  if (!epoch) return "";
  const d = new Date(epoch * 1000);
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const today = new Date();
  if (d.toDateString() === today.toDateString()) return `Today ${time}`;
  return `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}`;
}

export function kindLabel(kind: FileKind | undefined, lang?: string | null, active = false): string {
  if (active && lang) return `${lang}, shown as text`;
  switch (kind) {
    case "markdown":
      return "Markdown";
    case "code":
      return lang ? `Code · ${lang}` : "Code";
    case "text":
      return "Text";
    case "image":
      return "Image";
    case "video":
      return "Video";
    case "binary":
      return "Binary";
    default:
      return "File";
  }
}

// -- Markdown (a small subset, as in the approved mockup) -----------------------

const INLINE_RE = /`([^`]+)`|\*\*([^*]+)\*\*|(!?)\[([^\]]*)\]\(([^)\s]+)\)/g;

function inline(text: string, key: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let n = 0;
  INLINE_RE.lastIndex = 0;
  for (let m = INLINE_RE.exec(text); m; m = INLINE_RE.exec(text)) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const k = `${key}-${n++}`;
    if (m[1] !== undefined) out.push(<code key={k}>{m[1]}</code>);
    else if (m[2] !== undefined) out.push(<strong key={k}>{m[2]}</strong>);
    else if (m[3] === "!") out.push(<span key={k} className="md-img">[image: {m[4] || m[5]}]</span>);
    else if (/^(https?:|mailto:)/i.test(m[5]))
      out.push(
        <a key={k} className="md-link" href={m[5]} target="_blank" rel="noopener noreferrer">
          {m[4]}
        </a>,
      );
    else
      out.push(
        <span key={k} className="md-link local" title={m[5]}>
          {m[4]}
        </span>,
      );
    last = INLINE_RE.lastIndex;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

const BLOCK_START = /^(```|#{1,6}\s|>\s?|\s*[-*+]\s+|\s*\d+[.)]\s+|\||(-{3,}|\*{3,})\s*$)/;

export function Markdown({ source, label }: { source: string; label: string }) {
  const blocks = useMemo(() => renderBlocks(source), [source]);
  return (
    <article className="md" aria-label={label}>
      {blocks}
    </article>
  );
}

function renderBlocks(src: string): ReactNode[] {
  const L = src.replace(/\r\n/g, "\n").split("\n");
  const out: ReactNode[] = [];
  let i = 0;
  let b = 0;
  const cells = (row: string) =>
    row
      .trim()
      .replace(/^\||\|$/g, "")
      .split("|")
      .map((c) => c.trim());
  while (i < L.length) {
    const l = L[i];
    const key = `b${b++}`;
    if (/^```/.test(l)) {
      const body: string[] = [];
      i++;
      while (i < L.length && !/^```/.test(L[i])) body.push(L[i++]);
      i++;
      out.push(
        <pre key={key}>
          <code>{body.join("\n")}</code>
        </pre>,
      );
      continue;
    }
    const h = l.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      const level = Math.min(h[1].length + 1, 5);
      const Tag = `h${level}` as "h2" | "h3" | "h4" | "h5";
      out.push(<Tag key={key}>{inline(h[2], key)}</Tag>);
      i++;
      continue;
    }
    if (/^(-{3,}|\*{3,})\s*$/.test(l)) {
      out.push(<hr key={key} />);
      i++;
      continue;
    }
    if (/^\|/.test(l.trim()) && /^\|?\s*:?-{3,}/.test((L[i + 1] || "").trim())) {
      const head = cells(l);
      i += 2;
      const rows: string[][] = [];
      while (i < L.length && /^\|/.test(L[i].trim())) rows.push(cells(L[i++]));
      out.push(
        <div className="tbl" key={key}>
          <table>
            <thead>
              <tr>
                {head.map((c, j) => (
                  <th key={j}>{inline(c, `${key}h${j}`)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r, ri) => (
                <tr key={ri}>
                  {r.map((c, j) => (
                    <td key={j}>{inline(c, `${key}r${ri}c${j}`)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }
    if (/^>\s?/.test(l)) {
      const body: string[] = [];
      while (i < L.length && /^>\s?/.test(L[i])) body.push(L[i++].replace(/^>\s?/, ""));
      out.push(
        <blockquote key={key}>
          <p>{inline(body.join(" "), key)}</p>
        </blockquote>,
      );
      continue;
    }
    if (/^\s*[-*+]\s+/.test(l)) {
      const items: string[] = [];
      while (i < L.length && /^\s*[-*+]\s+/.test(L[i])) items.push(L[i++].replace(/^\s*[-*+]\s+/, ""));
      out.push(
        <ul key={key}>
          {items.map((x, j) => {
            const t = x.match(/^\[( |x|X)\]\s+(.*)$/);
            return t ? (
              <li key={j} className="task">
                <span aria-hidden="true">{t[1] === " " ? "☐" : "☑"}</span>{" "}
                <span className="sr-only">{t[1] === " " ? "Not done:" : "Done:"}</span>
                {inline(t[2], `${key}-${j}`)}
              </li>
            ) : (
              <li key={j}>{inline(x, `${key}-${j}`)}</li>
            );
          })}
        </ul>,
      );
      continue;
    }
    if (/^\s*\d+[.)]\s+/.test(l)) {
      const items: string[] = [];
      while (i < L.length && /^\s*\d+[.)]\s+/.test(L[i])) items.push(L[i++].replace(/^\s*\d+[.)]\s+/, ""));
      out.push(
        <ol key={key}>
          {items.map((x, j) => (
            <li key={j}>{inline(x, `${key}-${j}`)}</li>
          ))}
        </ol>,
      );
      continue;
    }
    if (/^\s*$/.test(l)) {
      i++;
      continue;
    }
    const para: string[] = [];
    while (i < L.length && !/^\s*$/.test(L[i]) && !(para.length && BLOCK_START.test(L[i]))) para.push(L[i++]);
    out.push(<p key={key}>{inline(para.join(" "), key)}</p>);
  }
  return out;
}

// -- Code view -------------------------------------------------------------------

const HIGHLIGHT_MAX = 200_000; // characters; larger files show plain text
const LINE_SPAN_MAX = 4000; // lines; larger files use one text node per column

const KEYWORDS: Record<string, RegExp> = {
  js: /\b(import|from|export|default|const|let|var|function|return|for|while|if|else|switch|case|break|continue|new|class|extends|async|await|try|catch|finally|throw|typeof|instanceof|interface|type|enum|implements|public|private|readonly|null|undefined|true|false)\b/,
  py: /\b(from|import|as|def|class|return|if|elif|else|for|while|in|not|and|or|is|with|try|except|finally|raise|yield|lambda|async|await|pass|None|True|False)\b/,
  sh: /\b(if|then|else|elif|fi|for|while|do|done|case|esac|function|return|export|local|in)\b/,
  go: /\b(package|import|func|return|if|else|for|range|var|const|type|struct|interface|go|defer|nil|true|false)\b/,
};

function langKey(lang: string | null | undefined): { kw: RegExp | null; comment: string } | null {
  switch (lang) {
    case "TypeScript":
    case "JavaScript":
    case "Vue":
    case "Svelte":
    case "Astro":
      return { kw: KEYWORDS.js, comment: "\\/\\/.*$" };
    case "Python":
      return { kw: KEYWORDS.py, comment: "#.*$" };
    case "Shell":
    case "Dockerfile":
    case "Makefile":
    case "YAML":
    case "TOML":
    case "INI":
      return { kw: lang === "Shell" ? KEYWORDS.sh : null, comment: "#.*$" };
    case "Go":
      return { kw: KEYWORDS.go, comment: "\\/\\/.*$" };
    case "JSON":
    case "CSS":
    case "SCSS":
    case "Less":
    case "Rust":
    case "Java":
    case "Kotlin":
    case "Swift":
    case "C":
    case "C++":
    case "C#":
    case "Dart":
    case "PHP":
      return { kw: null, comment: "\\/\\/.*$" };
    default:
      return null;
  }
}

function highlightLine(line: string, spec: { kw: RegExp | null; comment: string }): ReactNode[] {
  const strings = "\"(?:\\\\.|[^\"\\\\])*\"|'(?:\\\\.|[^'\\\\])*'|`(?:\\\\.|[^`\\\\])*`";
  const re = new RegExp(`(${spec.comment})|(${strings})${spec.kw ? `|${spec.kw.source}` : ""}`, "g");
  const out: ReactNode[] = [];
  let last = 0;
  let n = 0;
  for (let m = re.exec(line); m; m = re.exec(line)) {
    if (m[0].length === 0) {
      re.lastIndex++;
      continue;
    }
    if (m.index > last) out.push(line.slice(last, m.index));
    const cls = m[1] ? "tok-c" : m[2] ? "tok-s" : "tok-k";
    out.push(
      <span key={n++} className={cls}>
        {m[0]}
      </span>,
    );
    last = re.lastIndex;
  }
  if (last < line.length) out.push(line.slice(last));
  return out;
}

interface CodeViewProps {
  text: string;
  lang?: string | null;
  start?: number;
  label: string;
  continued?: boolean; // the first line continues from the previous page
  continues?: boolean; // the last line continues on the next page
}

export function CodeView({ text, lang, start = 1, label, continued, continues }: CodeViewProps) {
  const lines = useMemo(() => {
    const body = text.endsWith("\n") ? text.slice(0, -1) : text;
    return body.split("\n");
  }, [text]);
  const spec = text.length <= HIGHLIGHT_MAX ? langKey(lang) : null;
  const wide = start + lines.length > 9999;
  if (lines.length > LINE_SPAN_MAX) {
    // Two text nodes: fast for a 10 MB file on a phone.
    const numbers = lines.map((_, i) => String(start + i)).join("\n");
    return (
      <div className={`code plain${wide ? " wide" : ""}`} tabIndex={0} aria-label={label}>
        <pre className="gutter" aria-hidden="true">
          {numbers}
        </pre>
        <pre className="body">{lines.join("\n")}</pre>
      </div>
    );
  }
  return (
    <pre
      className={`code${wide ? " wide" : ""}`}
      tabIndex={0}
      aria-label={label}
      style={{ counterReset: `ln ${start - 1}` }}
    >
      {lines.map((l, i) => (
        <span className="line" key={i}>
          {i === 0 && continued ? (
            <span className="cont-mark" title="This line continues from the previous page">
              ↪{" "}
            </span>
          ) : null}
          {spec ? highlightLine(l, spec) : l}
          {i === lines.length - 1 && continues ? (
            <span className="cont-mark" title="This line continues on the next page">
              {" "}
              ↩ continues on the next page
            </span>
          ) : null}
        </span>
      ))}
    </pre>
  );
}

// -- Two-way line diff for the conflict compare ----------------------------------------

export type DiffRow = { t: " " | "-" | "+" | "gap"; s: string };

const DIFF_CELLS_MAX = 4_000_000;

// "-" = only in the newer version on disk, "+" = only in your version.
export function diffLines(disk: string, mine: string): DiffRow[] | null {
  const A = disk.replace(/\r\n/g, "\n").split("\n");
  const B = mine.split("\n");
  let pre = 0;
  while (pre < A.length && pre < B.length && A[pre] === B[pre]) pre++;
  let suf = 0;
  while (suf < A.length - pre && suf < B.length - pre && A[A.length - 1 - suf] === B[B.length - 1 - suf]) suf++;
  const a = A.slice(pre, A.length - suf);
  const b = B.slice(pre, B.length - suf);
  const n = a.length;
  const m = b.length;
  if ((n + 1) * (m + 1) > DIFF_CELLS_MAX) return null;
  const T = Array.from({ length: n + 1 }, () => new Int32Array(m + 1));
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--) T[i][j] = a[i] === b[j] ? T[i + 1][j + 1] + 1 : Math.max(T[i + 1][j], T[i][j + 1]);
  const rows: DiffRow[] = A.slice(0, pre).map((s) => ({ t: " ", s }));
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      rows.push({ t: " ", s: a[i] });
      i++;
      j++;
    } else if (T[i + 1][j] >= T[i][j + 1]) rows.push({ t: "-", s: a[i++] });
    else rows.push({ t: "+", s: b[j++] });
  }
  while (i < n) rows.push({ t: "-", s: a[i++] });
  while (j < m) rows.push({ t: "+", s: b[j++] });
  for (const s of A.slice(A.length - suf)) rows.push({ t: " ", s });
  // Keep changed lines with one line of context; fold the rest.
  const keep = rows.map((r, k) => r.t !== " " || rows.slice(Math.max(0, k - 1), k + 2).some((x) => x.t !== " "));
  const out: DiffRow[] = [];
  rows.forEach((r, k) => {
    if (keep[k]) out.push(r);
    else if (out.length === 0 || out[out.length - 1].t !== "gap") out.push({ t: "gap", s: "" });
  });
  return out;
}
