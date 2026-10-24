// Inline SVG icons ported from the mockup symbol sheet, plus distinct kind glyphs.
// All icons inherit stroke from currentColor per the shared svg rule in styles.css.

import type { SVGProps } from "react";
import type { SessionKind } from "./types";

type IconProps = SVGProps<SVGSVGElement>;

function base(props: IconProps, children: React.ReactNode) {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" {...props}>
      {children}
    </svg>
  );
}

export const CloseIcon = (p: IconProps) => base(p, <path d="m6 6 12 12M6 18 18 6" />);

export const MoreIcon = (p: IconProps) =>
  base(p, <path d="M5 12h.01M12 12h.01M19 12h.01" strokeWidth={3} strokeLinecap="round" />);

export const SplitIcon = (p: IconProps) =>
  base(p, <path d="M4 5h16v14H4zM12 5v14" />);

export const GridIcon = (p: IconProps) =>
  base(
    p,
    <>
      <rect x="3" y="3" width="7" height="7" rx="2" />
      <rect x="14" y="3" width="7" height="7" rx="2" />
      <rect x="3" y="14" width="7" height="7" rx="2" />
      <rect x="14" y="14" width="7" height="7" rx="2" />
    </>,
  );

export const ActivityIcon = (p: IconProps) => base(p, <path d="M3 12h4l3-8 4 16 3-8h4" />);

export const BellIcon = (p: IconProps) =>
  base(
    p,
    <>
      <path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9" />
      <path d="M10 21h4" />
    </>,
  );

export const TerminalIcon = (p: IconProps) =>
  base(
    p,
    <>
      <rect x="3" y="4" width="18" height="16" rx="3" />
      <path d="m7 9 3 3-3 3m6 0h4" />
    </>,
  );

export const CheckIcon = (p: IconProps) => base(p, <path d="m5 12 4 4L19 6" />);

export const ClockIcon = (p: IconProps) =>
  base(
    p,
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 2" />
    </>,
  );

export const ShieldIcon = (p: IconProps) =>
  base(
    p,
    <>
      <path d="m12 3 8 3v6c0 4-4 7-8 9-4-2-8-5-8-9V6z" />
      <path d="m8 12 3 3 5-6" />
    </>,
  );

export const ArrowIcon = (p: IconProps) => base(p, <path d="M5 12h14m-5-5 5 5-5 5" />);

export const ChatIcon = (p: IconProps) =>
  base(
    p,
    <>
      <path d="M21 11a8 8 0 0 1-8 8H7l-4 3V5a2 2 0 0 1 2-2h10a6 6 0 0 1 6 6z" />
      <path d="M7 8h10M7 12h7" />
    </>,
  );

export const PauseIcon = (p: IconProps) => base(p, <path d="M9 5v14M15 5v14" />);

export const StopIcon = (p: IconProps) =>
  base(p, <rect x="6" y="6" width="12" height="12" rx="2" />);

export const PlayIcon = (p: IconProps) => base(p, <path d="m8 4 12 8-12 8z" />);

export const BackIcon = (p: IconProps) => base(p, <path d="m14 5-7 7 7 7" />);

export const PlusIcon = (p: IconProps) => base(p, <path d="M12 5v14M5 12h14" />);

export const SendIcon = (p: IconProps) => base(p, <path d="m12 19 0-14m-6 6 6-6 6 6" />);

export const SettingsIcon = (p: IconProps) =>
  base(
    p,
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="m9 3-1 3-3 1-2 3 2 2-1 3 2 3 3-1 2 3 4-1 1-3 3-1 2-3-2-2 1-3-2-3-3 1-2-3z" />
    </>,
  );

export const LinkIcon = (p: IconProps) =>
  base(
    p,
    <>
      <path d="m10 14 4-4m-6 2-3 3a4 4 0 0 0 6 6l3-3m-4-12 3-3a4 4 0 0 1 6 6l-3 3" />
    </>,
  );

export const RefreshIcon = (p: IconProps) =>
  base(p, <path d="M20 7v5h-5M4 17v-5h5M6 7a7 7 0 0 1 12-1l2 3M4 15l2 3a7 7 0 0 0 12-1" />);

export const SparkIcon = (p: IconProps) =>
  base(p, <path d="m12 2 3 7 7 3-7 3-3 7-3-7-7-3 7-3z" />);

export const ChevronRightIcon = (p: IconProps) => base(p, <path d="m9 6 6 6-6 6" />);

export const ExternalIcon = (p: IconProps) =>
  base(
    p,
    <>
      <path d="M14 4h6v6" />
      <path d="M20 4 10 14" />
      <path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5" />
    </>,
  );

// Files and Previews icons (from the approved Files and Previews mockup).

export const FolderIcon = (p: IconProps) =>
  base(p, <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />);

export const FileIcon = (p: IconProps) =>
  base(
    p,
    <>
      <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" />
      <path d="M14 3v5h5M9 13h6M9 17h4" />
    </>,
  );

export const CodeFileIcon = (p: IconProps) =>
  base(
    p,
    <>
      <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" />
      <path d="M14 3v5h5M10 12l-2 2 2 2M14 12l2 2-2 2" />
    </>,
  );

export const ImageIcon = (p: IconProps) =>
  base(
    p,
    <>
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <circle cx="9" cy="10" r="2" />
      <path d="m21 16-5-5-9 9" />
    </>,
  );

export const VideoIcon = (p: IconProps) =>
  base(
    p,
    <>
      <rect x="3" y="5" width="13" height="14" rx="2" />
      <path d="m16 10 5-3v10l-5-3z" />
    </>,
  );

export const ArchiveIcon = (p: IconProps) =>
  base(
    p,
    <>
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path d="M10 4v4h4V4M12 11v2M12 15v2" />
    </>,
  );

export const LargeFileIcon = (p: IconProps) =>
  base(
    p,
    <>
      <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" />
      <path d="M14 3v5h5M12 11v4M12 18h.01" />
    </>,
  );

export const DownloadIcon = (p: IconProps) => base(p, <path d="M12 4v11M7 10l5 5 5-5M5 20h14" />);

export const SearchIcon = (p: IconProps) =>
  base(
    p,
    <>
      <circle cx="11" cy="11" r="7" />
      <path d="m20 20-4-4" />
    </>,
  );

export const PhoneIcon = (p: IconProps) =>
  base(
    p,
    <>
      <rect x="7" y="3" width="10" height="18" rx="2" />
      <path d="M11 18h2" />
    </>,
  );

export const LaptopIcon = (p: IconProps) =>
  base(
    p,
    <>
      <rect x="4" y="5" width="16" height="11" rx="2" />
      <path d="M2 19h20" />
    </>,
  );

export const ServerIcon = (p: IconProps) =>
  base(
    p,
    <>
      <rect x="4" y="4" width="16" height="7" rx="2" />
      <rect x="4" y="13" width="16" height="7" rx="2" />
      <path d="M8 7.5h.01M8 16.5h.01" />
    </>,
  );

// Kind glyphs: distinct small marks for Claude, Codex, Shell.

export const ClaudeGlyph = (p: IconProps) =>
  base(
    p,
    <>
      <path d="M12 3v18M4.5 7.5l15 9M19.5 7.5l-15 9" />
    </>,
  );

export const CodexGlyph = (p: IconProps) =>
  base(
    p,
    <>
      <path d="M9 5a4 4 0 1 0 0 8M15 5a4 4 0 1 1 0 8M9 5h6M9 13h6M12 13v6" />
    </>,
  );

export const ShellGlyph = (p: IconProps) =>
  base(
    p,
    <>
      <path d="m5 8 4 4-4 4M12 16h7" />
    </>,
  );

export function KindGlyph({ kind, ...props }: IconProps & { kind: SessionKind }) {
  if (kind === "claude") return <ClaudeGlyph {...props} />;
  if (kind === "codex") return <CodexGlyph {...props} />;
  if (kind === "shell") return <ShellGlyph {...props} />;
  return <TerminalIcon {...props} />;
}
