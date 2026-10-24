// Files page: a workspace switch, a folder browser (a tree on wide screens, a
// folder list with a path on phones), file-name search, and a file pane with
// Markdown/text/code views, explicit edits with revision conflicts, a paged
// view for large text, image zoom, native video, and downloads.
//
// Data comes only from the authenticated /api/files routes. Unsaved changes
// are guarded on every way out: another file, Back (in-app and the browser
// or phone Back), the workspace switch, the app navigation, and page unload.

import { useCallback, useEffect, useMemo, useRef, useState, type MutableRefObject, type ReactNode } from "react";
import {
  ApiError,
  fileRawUrl,
  getFileMeta,
  getFilePage,
  getFileText,
  listFolder,
  saveFileText,
  searchFiles,
} from "./api";
import { CodeView, Markdown, diffLines, fmtInt, fmtSize, fmtWhen, kindLabel } from "./fileviews";
import {
  ArchiveIcon,
  BackIcon,
  CheckIcon,
  ChevronRightIcon,
  CodeFileIcon,
  DownloadIcon,
  FileIcon,
  FolderIcon,
  ImageIcon,
  LargeFileIcon,
  SearchIcon,
  VideoIcon,
} from "./icons";
import { Modal } from "./Modal";
import { useToast } from "./toast";
import type { EnvView, FileEntry, FileKind, FileMeta, FilePage, FileText, FolderListing, SearchView } from "./types";

const ENV_KEY = "relay.files.workspace";
const POLL_MS = 5000;
const SEARCH_DEBOUNCE_MS = 250;
const NARROW = "(max-width: 759px)";

type Listing = { state: "loading" } | { state: "error"; message: string } | { state: "ok"; data: FolderListing };

interface Ask {
  title: string;
  body: string;
  ok: string;
  cancel: string;
  danger?: boolean;
  resolve: (ok: boolean) => void;
}

interface OpenFile {
  env: string;
  path: string;
  name: string;
  meta: FileMeta | null;
  error: string | null;
}

interface Conflict {
  text: string;
  revision: string;
  mtime: number;
  compare: boolean;
}

type Notice = { kind: "saved" | "info" | "error"; title?: string; text: string } | null;

const isAbort = (err: unknown) => err instanceof DOMException && err.name === "AbortError";

function errorText(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.message || fallback;
  return fallback;
}

function iconFor(kind: FileKind | undefined, dir = false): ReactNode {
  if (dir) return <FolderIcon />;
  if (kind === "code") return <CodeFileIcon />;
  if (kind === "image") return <ImageIcon />;
  if (kind === "video") return <VideoIcon />;
  if (kind === "binary") return <ArchiveIcon />;
  return <FileIcon />;
}

function parentOf(path: string): string {
  const i = path.lastIndexOf("/");
  return i < 0 ? "" : path.slice(0, i);
}

function baseName(path: string): string {
  const i = path.lastIndexOf("/");
  return i < 0 ? path : path.slice(i + 1);
}

const normalize = (t: string) => t.replace(/\r\n/g, "\n");

interface FilesPageProps {
  envs: EnvView[];
  guardRef: MutableRefObject<(() => Promise<boolean>) | null>;
}

export function FilesPage({ envs, guardRef }: FilesPageProps) {
  const toast = useToast();
  const [env, setEnv] = useState<string>(() => {
    const saved = localStorage.getItem(ENV_KEY);
    return saved && envs.some((e) => e.id === saved) ? saved : envs[0]?.id ?? "work1";
  });
  const [hidden, setHidden] = useState(false);
  const [folder, setFolder] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [listings, setListings] = useState<Record<string, Listing>>({});
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState<{ state: "idle" | "loading" | "ok" | "error"; data?: SearchView; message?: string }>({
    state: "idle",
  });
  const [open, setOpen] = useState<OpenFile | null>(null);
  const [doc, setDoc] = useState<FileText | null>(null);
  const [page, setPage] = useState<FilePage | null>(null);
  const [pageBusy, setPageBusy] = useState(false);
  const [pageStale, setPageStale] = useState<{ revision: string; mtime: number | null } | null>(null);
  const [mdTab, setMdTab] = useState<"preview" | "text">("preview");
  const [editTab, setEditTab] = useState<"write" | "preview">("write");
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [base, setBase] = useState("");
  const [baseRevision, setBaseRevision] = useState("");
  const [conflict, setConflict] = useState<Conflict | null>(null);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const [ask, setAsk] = useState<Ask | null>(null);
  const [narrow, setNarrow] = useState(() => matchMedia(NARROW).matches);

  const envLabel = envs.find((e) => e.id === env)?.label ?? env;
  const dirty = editing && draft !== base;

  // Refs for async callbacks.
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;
  const editingRef = useRef(editing);
  editingRef.current = editing;
  // Changes on every edit start and every reset (Cancel, Discard, Save, close),
  // so a late conflict read can never arm a later or finished edit.
  const editGenRef = useRef(0);
  const openRef = useRef(open);
  openRef.current = open;
  const pushedRef = useRef(false);
  // Every async result of the open file must belong to the current selection.
  // The generation changes on each open, close, and workspace switch; the
  // controller aborts the requests of the previous selection.
  const selRef = useRef(0);
  const ctlRef = useRef<AbortController | null>(null);
  const pageReqRef = useRef(0);
  const isCurrent = (gen: number) => selRef.current === gen;
  const newSelection = useCallback(() => {
    ctlRef.current?.abort();
    ctlRef.current = new AbortController();
    selRef.current += 1;
    setPageBusy(false);
    return { gen: selRef.current, signal: ctlRef.current.signal };
  }, []);
  const revisionRef = useRef<string | null>(null);
  revisionRef.current = page?.revision ?? doc?.revision ?? open?.meta?.revision ?? null;

  useEffect(() => {
    const mq = matchMedia(NARROW);
    const on = () => setNarrow(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);

  useEffect(() => {
    localStorage.setItem(ENV_KEY, env);
  }, [env]);

  // -- confirmation dialog ---------------------------------------------------
  const askUser = useCallback(
    (a: Omit<Ask, "resolve">) => new Promise<boolean>((resolve) => setAsk({ ...a, resolve })),
    [],
  );

  const resetEdit = useCallback(() => {
    editGenRef.current += 1;
    editingRef.current = false;
    setEditing(false);
    setDraft("");
    setBase("");
    setBaseRevision("");
    setConflict(null);
    setSaving(false);
  }, []);

  // Resolves true when it is safe to leave the open file.
  const guard = useCallback(async (): Promise<boolean> => {
    if (!dirtyRef.current) return true;
    const name = openRef.current?.name ?? "this file";
    const ok = await askUser({
      title: "Discard unsaved changes?",
      body: `Your changes to ${name} are not saved. They will be lost.`,
      ok: "Discard changes",
      cancel: "Keep editing",
      danger: true,
    });
    if (ok) resetEdit();
    return ok;
  }, [askUser, resetEdit]);

  useEffect(() => {
    guardRef.current = guard;
    return () => {
      guardRef.current = null;
    };
  }, [guard, guardRef]);

  useEffect(() => {
    if (!dirty) return;
    const onUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", onUnload);
    return () => window.removeEventListener("beforeunload", onUnload);
  }, [dirty]);

  // -- folders -----------------------------------------------------------------
  const listingKey = (path: string) => `${env}\n${hidden ? 1 : 0}\n${path}`;

  const listingsRef = useRef(listings);
  listingsRef.current = listings;
  const inflight = useRef(new Set<string>());
  const folderRef = useRef(folder);
  folderRef.current = folder;
  const expandedRef = useRef(expanded);
  expandedRef.current = expanded;

  const loadFolder = useCallback(
    async (path: string, force = false) => {
      const key = `${env}\n${hidden ? 1 : 0}\n${path}`;
      const cur = listingsRef.current[key];
      if (inflight.current.has(key) || (!force && cur?.state === "ok")) return;
      inflight.current.add(key);
      setListings((prev) => (prev[key]?.state === "ok" ? prev : { ...prev, [key]: { state: "loading" } }));
      try {
        const data = await listFolder(env, path, hidden);
        setListings((prev) => ({ ...prev, [key]: { state: "ok", data } }));
      } catch (err) {
        setListings((prev) => ({ ...prev, [key]: { state: "error", message: errorText(err, "Cannot reach relay.") } }));
      } finally {
        inflight.current.delete(key);
      }
    },
    [env, hidden],
  );

  // Load what the view shows when the workspace or the hidden toggle changes.
  useEffect(() => {
    void loadFolder("");
    void loadFolder(folderRef.current);
    for (const p of expandedRef.current) void loadFolder(p);
  }, [loadFolder]);

  // A folder shows its cached list at once and refreshes it in the background,
  // so files that an agent adds appear when you come back.
  useEffect(() => {
    void loadFolder(folder, true);
  }, [folder, loadFolder]);

  // -- search ------------------------------------------------------------------
  const q = query.trim();
  useEffect(() => {
    if (!q) {
      setSearch({ state: "idle" });
      return;
    }
    const ctl = new AbortController();
    let live = true; // a result for an older query or workspace never lands
    setSearch((s) => ({ state: "loading", data: s.data }));
    const t = window.setTimeout(() => {
      searchFiles(env, q, hidden, ctl.signal)
        .then((data) => {
          if (live) setSearch({ state: "ok", data });
        })
        .catch((err) => {
          if (!live || isAbort(err)) return;
          setSearch({ state: "error", message: errorText(err, "Cannot reach relay.") });
        });
    }, SEARCH_DEBOUNCE_MS);
    return () => {
      live = false;
      window.clearTimeout(t);
      ctl.abort();
    };
  }, [q, env, hidden]);

  // -- open a file ---------------------------------------------------------------
  const loadFile = useCallback(
    async (envId: string, path: string) => {
      const { gen, signal } = newSelection();
      setOpen({ env: envId, path, name: baseName(path), meta: null, error: null });
      setDoc(null);
      setPage(null);
      setPageStale(null);
      try {
        const meta = await getFileMeta(envId, path, signal);
        if (!isCurrent(gen)) return;
        setOpen({ env: envId, path, name: meta.name, meta, error: null });
        if (meta.mode === "edit") {
          const text = await getFileText(envId, path, signal);
          if (isCurrent(gen)) setDoc(text);
        } else if (meta.mode === "paged") {
          const p = await getFilePage(envId, path, 0, meta.revision, signal);
          if (isCurrent(gen)) setPage(p);
        }
      } catch (err) {
        if (!isCurrent(gen) || isAbort(err)) return;
        setOpen((o) => (o ? { ...o, error: errorText(err, "Cannot open this file.") } : o));
      }
    },
    [newSelection],
  );

  const openFile = useCallback(
    async (path: string) => {
      const cur = openRef.current;
      if (cur && cur.env === env && cur.path === path) return;
      if (!(await guard())) return;
      resetEdit();
      setNotice(null);
      setMdTab("preview");
      setEditTab("write");
      setFolder(parentOf(path));
      setExpanded((prev) => {
        const next = new Set(prev);
        const parts = path.split("/");
        for (let k = 1; k < parts.length; k++) next.add(parts.slice(0, k).join("/"));
        return next;
      });
      if (matchMedia(NARROW).matches && !pushedRef.current) {
        history.pushState({ relayFile: true }, "");
        pushedRef.current = true;
      }
      if (matchMedia(NARROW).matches) window.scrollTo(0, 0);
      await loadFile(env, path);
    },
    [env, guard, loadFile, resetEdit],
  );

  const closeFile = useCallback(() => {
    newSelection();
    resetEdit();
    setOpen(null);
    setDoc(null);
    setPage(null);
    setPageStale(null);
    setNotice(null);
  }, [newSelection, resetEdit]);

  // In-app Back: consume the history entry so the phone Back stays in step.
  const back = useCallback(async () => {
    if (!(await guard())) return;
    if (pushedRef.current) {
      history.back();
    } else {
      closeFile();
    }
  }, [closeFile, guard]);

  // Phone or browser Back while a file is open.
  useEffect(() => {
    const onPop = () => {
      if (!pushedRef.current || !openRef.current) return;
      pushedRef.current = false;
      if (dirtyRef.current) {
        history.pushState({ relayFile: true }, "");
        pushedRef.current = true;
        void guard().then((ok) => {
          if (ok) history.back();
        });
        return;
      }
      closeFile();
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, [closeFile, guard]);

  // -- external changes ------------------------------------------------------------
  const loadConflict = useCallback(async () => {
    const o = openRef.current;
    if (!o || !editingRef.current) return;
    const gen = selRef.current;
    const editGen = editGenRef.current;
    // Only the same file, the same edit, and a draft with changes may get a conflict.
    const stillEditing = () => isCurrent(gen) && editGen === editGenRef.current && editingRef.current && dirtyRef.current;
    try {
      const disk = await getFileText(o.env, o.path, ctlRef.current?.signal);
      if (!stillEditing()) return;
      setConflict((c) => ({ text: disk.text, revision: disk.revision, mtime: disk.mtime, compare: c?.compare ?? false }));
      setDoc((d) => (d ? { ...d, size: disk.size, mtime: disk.mtime } : d));
    } catch (err) {
      if (!stillEditing() || isAbort(err)) return;
      setNotice({ kind: "error", text: errorText(err, "relay cannot read the newer version.") });
    }
  }, []);

  const onExternalChange = useCallback(
    async (meta: FileMeta, gen: number) => {
      const o = openRef.current;
      if (!o || !isCurrent(gen)) return;
      if (page || o.meta?.mode === "paged") {
        setPageStale({ revision: meta.revision, mtime: meta.mtime });
        return;
      }
      if (meta.mode !== o.meta?.mode) {
        // The file crossed a size limit or changed type: open it again.
        if (dirtyRef.current) {
          await loadConflict();
          return;
        }
        resetEdit();
        await loadFile(o.env, o.path);
        const now = openRef.current;
        if (now && now.env === o.env && now.path === o.path)
          setNotice({ kind: "info", title: "Updated from disk.", text: `The file changed at ${fmtWhen(meta.mtime)}.` });
        return;
      }
      if (meta.mode !== "edit") {
        setOpen((cur) => (cur ? { ...cur, meta } : cur));
        setNotice({ kind: "info", title: "Updated from disk.", text: `The file changed at ${fmtWhen(meta.mtime)}.` });
        return;
      }
      if (editing && dirtyRef.current) {
        if (conflict?.revision === meta.revision) return;
        await loadConflict();
        return;
      }
      try {
        const text = await getFileText(o.env, o.path, ctlRef.current?.signal);
        if (!isCurrent(gen)) return;
        setDoc(text);
        setOpen((cur) => (cur ? { ...cur, meta: text } : cur));
        if (editing) {
          setDraft(normalize(text.text));
          setBase(normalize(text.text));
          setBaseRevision(text.revision);
          setNotice({
            kind: "info",
            title: "Updated from disk.",
            text: `The file changed at ${fmtWhen(text.mtime)}. You had no changes, so relay loaded the new version.`,
          });
        } else {
          setNotice({
            kind: "info",
            title: "Updated from disk.",
            text: `The file changed at ${fmtWhen(text.mtime)}, by an agent or another program. relay shows the new version.`,
          });
        }
      } catch {
        /* the next poll tries again */
      }
    },
    [conflict, editing, loadConflict, loadFile, page, resetEdit],
  );

  useEffect(() => {
    if (!open?.meta || saving) return;
    const { env: e, path } = open;
    const t = window.setInterval(() => {
      if (document.visibilityState !== "visible") return;
      const gen = selRef.current;
      getFileMeta(e, path, ctlRef.current?.signal)
        .then((meta) => {
          if (!isCurrent(gen)) return;
          const known = pageStale?.revision ?? conflict?.revision ?? revisionRef.current;
          if (known && meta.revision !== known) void onExternalChange(meta, gen);
        })
        .catch(() => {});
    }, POLL_MS);
    return () => window.clearInterval(t);
  }, [open, saving, pageStale, conflict, onExternalChange]);

  // -- edit and save -----------------------------------------------------------------
  const startEdit = () => {
    if (!doc) return;
    editGenRef.current += 1;
    editingRef.current = true;
    const t = normalize(doc.text);
    setDraft(t);
    setBase(t);
    setBaseRevision(doc.revision);
    setEditing(true);
    setEditTab("write");
    setNotice(null);
    window.setTimeout(() => document.getElementById("file-editor")?.focus({ preventScroll: true }), 0);
  };

  const save = async (revision: string) => {
    // Never save outside an edit: the draft is only meaningful while editing.
    if (!open || !doc || !editingRef.current) return;
    const gen = selRef.current;
    setSaving(true);
    const payload = doc.line_ending === "crlf" ? draft.replace(/\n/g, "\r\n") : draft;
    try {
      const res = await saveFileText(open.env, open.path, payload, revision);
      if (!isCurrent(gen)) {
        toast.show(`Saved ${open.name}.`); // the save happened; the view has moved on
        return;
      }
      setDoc({ ...doc, text: payload, revision: res.revision, size: res.size, mtime: res.mtime });
      setOpen((o) => (o && o.meta ? { ...o, meta: { ...o.meta, revision: res.revision, size: res.size, mtime: res.mtime } } : o));
      resetEdit();
      setNotice({
        kind: "saved",
        text: `Saved at ${fmtWhen(res.mtime)}${res.hard_links_detached ? ". Other hard links to this file keep the old content" : ""}.`,
      });
      toast.show(`Saved ${open.name}.`);
    } catch (err) {
      if (!isCurrent(gen)) return;
      setSaving(false);
      if (err instanceof ApiError && err.status === 409 && err.code === "revision_changed") {
        await loadConflict();
      } else if (err instanceof ApiError && err.status === 413) {
        setNotice({ kind: "error", text: `${err.message} Your text is ${fmtSize(new Blob([payload]).size)}.` });
      } else {
        setNotice({ kind: "error", text: errorText(err, "Cannot reach relay. Your changes are still here.") });
      }
    }
  };

  const cancelEdit = async () => {
    if (await guard()) resetEdit();
  };

  const useNewer = async () => {
    if (!conflict || !doc || !editingRef.current) return;
    const ok = await askUser({
      title: "Use the newer version?",
      body: "relay loads the version on disk. Your changes will be lost.",
      ok: "Use the newer version",
      cancel: "Back",
      danger: true,
    });
    if (!ok) return;
    const t = normalize(conflict.text);
    setDoc({ ...doc, text: conflict.text, revision: conflict.revision, mtime: conflict.mtime });
    setDraft(t);
    setBase(t);
    setBaseRevision(conflict.revision);
    setConflict(null);
    toast.show("Loaded the newer version. Your changes were discarded.");
  };

  const overwrite = async () => {
    if (!conflict || !editingRef.current) return;
    const ok = await askUser({
      title: "Overwrite the newer version?",
      body: "The changes that were saved on disk after you started will be lost.",
      ok: "Overwrite",
      cancel: "Back",
      danger: true,
    });
    if (!ok) return;
    // Overwrite only the version that this compare showed. A newer save is another conflict.
    const rev = conflict.revision;
    setConflict(null);
    await save(rev);
  };

  // -- paged view ------------------------------------------------------------------------
  const goPage = async (n: number, revision?: string) => {
    if (!open) return;
    // Only the latest page request of the current selection may land.
    const gen = selRef.current;
    const req = ++pageReqRef.current;
    const latest = () => isCurrent(gen) && req === pageReqRef.current;
    setPageBusy(true);
    try {
      const p = await getFilePage(open.env, open.path, n, revision, ctlRef.current?.signal);
      if (!latest()) return;
      setPage(p);
      setPageStale(null);
      setOpen((o) => (o && o.meta ? { ...o, meta: { ...o.meta, revision: p.revision, size: p.size, mtime: p.mtime } } : o));
    } catch (err) {
      if (!latest() || isAbort(err)) return;
      if (err instanceof ApiError && err.status === 409 && err.code === "revision_changed") {
        const body = err.body as { revision?: string; mtime?: number } | null;
        setPageStale({ revision: body?.revision ?? "", mtime: body?.mtime ?? null });
      } else {
        toast.error(errorText(err, "Cannot load this page."));
      }
    } finally {
      if (latest()) setPageBusy(false);
    }
  };

  // -- workspace switch ------------------------------------------------------------------
  const switchEnv = async (id: string) => {
    if (id === env) return;
    if (!(await guard())) return;
    closeFile();
    if (pushedRef.current) {
      pushedRef.current = false;
      history.back();
    }
    setFolder("");
    setExpanded(new Set());
    setEnv(id);
  };

  const toggleDir = (path: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else {
        next.add(path);
        void loadFolder(path, true);
      }
      return next;
    });
  };

  // -- render ----------------------------------------------------------------------------
  const listingFor = (path: string): Listing | undefined => listings[listingKey(path)];
  const searching = q.length > 0;
  const openPath = open && open.env === env ? open.path : null;

  const crumbs = useMemo(() => {
    const parts = folder ? folder.split("/") : [];
    return [{ label: "projects", path: "" }, ...parts.map((p, k) => ({ label: p, path: parts.slice(0, k + 1).join("/") }))];
  }, [folder]);

  const entryMeta = (e: FileEntry, top: boolean): string => {
    if (e.type === "dir") {
      if (top && !e.link) return "Project folder";
      const n = e.items;
      const items = n == null ? "" : n >= 1000 ? " · 1,000+ items" : ` · ${n} ${n === 1 ? "item" : "items"}`;
      return `${e.link ? "Folder link" : "Folder"}${items}`;
    }
    if (e.type === "file") return `${kindLabel(e.kind, e.lang)} · ${fmtSize(e.size)} · ${fmtWhen(e.mtime)}`;
    if (e.type === "link_outside") return "Link outside this workspace · not opened";
    if (e.type === "link_broken") return "Broken link";
    if (e.type === "other") return "Special file · not opened";
    return "Link";
  };

  const renderEntries = () => {
    const l = listingFor(folder);
    if (!l || l.state === "loading") return <p className="list-note">Loading…</p>;
    if (l.state === "error")
      return (
        <div className="list-note" role="alert">
          {l.message}{" "}
          <button className="btn sm" onClick={() => void loadFolder(folder, true)}>
            Try again
          </button>
        </div>
      );
    if (l.data.entries.length === 0) return <p className="list-note">This folder is empty.</p>;
    return (
      <>
        <ul className="entries">
          {l.data.entries.map((e) => {
            const openable = e.type === "dir" || e.type === "file";
            return (
              <li key={e.name}>
                <button
                  className="entry"
                  disabled={!openable}
                  aria-current={e.path === openPath ? "true" : undefined}
                  onClick={() => {
                    if (e.type === "dir") setFolder(e.path);
                    else if (e.type === "file") void openFile(e.path);
                  }}
                >
                  <span className={`ico${e.type === "dir" ? " dir" : ""}`}>{iconFor(e.kind, e.type === "dir")}</span>
                  <span className="txt">
                    <strong>{e.name}</strong>
                    <small>{entryMeta(e, folder === "")}</small>
                  </span>
                  {openable ? <ChevronRightIcon /> : null}
                </button>
              </li>
            );
          })}
        </ul>
        {l.data.truncated ? <p className="list-note">relay shows the first 5,000 entries of this folder.</p> : null}
      </>
    );
  };

  const renderTree = (path: string, depth: number): ReactNode => {
    const l = listingFor(path);
    if (!l || l.state === "loading")
      return (
        <li role="none" className="tree-note" style={{ paddingLeft: 8 + depth * 16 }}>
          Loading…
        </li>
      );
    if (l.state === "error")
      return (
        <li role="none" className="tree-note" style={{ paddingLeft: 8 + depth * 16 }}>
          {l.message}
        </li>
      );
    if (l.data.entries.length === 0 && depth > 0)
      return (
        <li role="none" className="tree-note" style={{ paddingLeft: 8 + depth * 16 }}>
          Empty folder
        </li>
      );
    return l.data.entries.map((e) => {
      const pad = { paddingLeft: 8 + depth * 16 };
      if (e.type === "dir") {
        const isOpen = expanded.has(e.path);
        return (
          <li role="none" key={e.path}>
            <button className="node" role="treeitem" aria-expanded={isOpen} style={pad} onClick={() => toggleDir(e.path)}>
              <ChevronRightIcon className="chev" />
              <FolderIcon />
              <span className="label">{e.name}</span>
            </button>
            {isOpen ? <ul role="group">{renderTree(e.path, depth + 1)}</ul> : null}
          </li>
        );
      }
      const cur = e.path === openPath;
      return (
        <li role="none" key={e.path}>
          <button
            className="node"
            role="treeitem"
            style={pad}
            disabled={e.type !== "file"}
            aria-current={cur ? "true" : undefined}
            title={e.type === "file" ? undefined : entryMeta(e, false)}
            onClick={() => void openFile(e.path)}
          >
            <span className="spacer" />
            {iconFor(e.kind)}
            <span className="label">{e.name}</span>
            {cur && dirty ? <span className="dot-dirty" title="Unsaved changes" /> : null}
          </button>
        </li>
      );
    });
  };

  const highlightName = (name: string) => {
    const i = name.toLowerCase().indexOf(q.toLowerCase());
    if (i < 0) return name;
    return (
      <>
        {name.slice(0, i)}
        <mark>{name.slice(i, i + q.length)}</mark>
        {name.slice(i + q.length)}
      </>
    );
  };

  const renderSearch = () => {
    const data = search.data;
    if (search.state === "error")
      return (
        <p className="search-meta" role="alert">
          {search.message}
        </p>
      );
    if (!data) return <p className="search-meta">Searching…</p>;
    const meta =
      data.total === 0
        ? `0 files in ${envLabel}`
        : `${fmtInt(data.total)}${data.reason === "result_limit" ? "+" : ""} ${data.total === 1 ? "file" : "files"} in ${envLabel} · names only${data.total > data.limit ? ` · first ${data.limit} shown` : ""}`;
    return (
      <>
        <p className="search-meta" aria-live="polite">
          {search.state === "loading" ? "Searching… " : ""}
          {meta}
          {data.partial && data.reason !== "result_limit" ? " · search stopped early, results can be incomplete" : ""}
        </p>
        {data.results.length ? (
          <ul className="results" aria-label="Search results">
            {data.results.map((r) => (
              <li key={r.path}>
                <button className="entry" aria-current={r.path === openPath ? "true" : undefined} onClick={() => void openFile(r.path)}>
                  <span className="ico">{iconFor(r.kind)}</span>
                  <span className="txt">
                    <strong>{highlightName(r.name)}</strong>
                    <small>
                      {r.parent || "projects"} · {kindLabel(r.kind, r.lang)}
                    </small>
                  </span>
                  <ChevronRightIcon />
                </button>
              </li>
            ))}
          </ul>
        ) : (
          <div className="search-empty">
            <strong>
              No file names match “{q}” in {envLabel}.
            </strong>
            Search looks at file names only, not file contents. Try another part of the name, or the other workspace.
          </div>
        )}
      </>
    );
  };

  return (
    <section className={`view-files${open && narrow ? " file-open" : ""}`}>
      <div className="page-head">
        <div>
          <h1>Files</h1>
          <p>Read and edit project files in your workspaces.</p>
        </div>
        <div className="ws-switch">
          <span className="lbl" id="files-ws-label">
            Workspace
          </span>
          <div className="seg" role="group" aria-labelledby="files-ws-label">
            {envs.map((e) => (
              <button key={e.id} aria-pressed={e.id === env} onClick={() => void switchEnv(e.id)}>
                {e.label}
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className={`files-layout${open ? " has-file" : ""}`}>
        <aside className="panel files-browser" aria-label="Project folders">
          <div className="fb-head">
            <div>
              <h2>{envLabel}</h2>
              <p>~/projects</p>
            </div>
            <button className="btn sm" aria-pressed={hidden} onClick={() => setHidden((h) => !h)}>
              Show hidden
            </button>
          </div>
          <div className={`fb-search${searching ? " has-q" : ""}`} role="search">
            <SearchIcon />
            <label className="sr-only" htmlFor="file-search">
              Search file names in this workspace
            </label>
            <input
              id="file-search"
              type="search"
              placeholder={`Search file names in ${envLabel}`}
              autoComplete="off"
              spellCheck={false}
              autoCapitalize="off"
              value={query}
              maxLength={200}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape" && query) {
                  e.preventDefault();
                  setQuery("");
                }
              }}
            />
            {searching ? (
              <button
                type="button"
                className="clear"
                onClick={() => {
                  setQuery("");
                  document.getElementById("file-search")?.focus();
                }}
              >
                Clear
              </button>
            ) : null}
          </div>
          {searching ? (
            renderSearch()
          ) : (
            <>
              <nav className="crumbs" aria-label="Folder path">
                {crumbs.map((c, k) => (
                  <span key={c.path} className="crumb">
                    {k ? (
                      <span className="sep" aria-hidden="true">
                        /
                      </span>
                    ) : null}
                    <button aria-current={k === crumbs.length - 1 ? "page" : undefined} onClick={() => setFolder(c.path)}>
                      {c.label}
                    </button>
                  </span>
                ))}
              </nav>
              <div className="entries-wrap">{renderEntries()}</div>
              <ul className="tree" role="tree" aria-label="Project files">
                {renderTree("", 0)}
              </ul>
            </>
          )}
        </aside>

        <section className="panel file-pane" aria-label="File">
          {open ? (
            <FilePane
              open={open}
              envLabel={envs.find((e) => e.id === open.env)?.label ?? open.env}
              doc={doc}
              page={page}
              pageBusy={pageBusy}
              pageStale={pageStale}
              mdTab={mdTab}
              editTab={editTab}
              editing={editing}
              dirty={dirty}
              draft={draft}
              baseRevision={baseRevision}
              conflict={conflict}
              saving={saving}
              notice={notice}
              onBack={() => void back()}
              onMdTab={setMdTab}
              onEditTab={setEditTab}
              onEdit={startEdit}
              onCancel={() => void cancelEdit()}
              onSave={() => void save(baseRevision)}
              onDraft={setDraft}
              onCompare={() => setConflict((c) => (c ? { ...c, compare: !c.compare } : c))}
              onUseNewer={() => void useNewer()}
              onOverwrite={() => void overwrite()}
              onPage={(n, rev) => void goPage(n, rev)}
              onRetry={() => void loadFile(open.env, open.path)}
            />
          ) : (
            <div className="fp-empty">
              <div>
                <div className="ico">
                  <FileIcon />
                </div>
                <h3>Pick a file</h3>
                <p>Choose a file in the tree to read or edit it.</p>
              </div>
            </div>
          )}
        </section>
      </div>

      <Modal
        open={ask != null}
        onClose={() => {
          ask?.resolve(false);
          setAsk(null);
        }}
        titleId="files-ask-title"
        dismissLabel={ask?.cancel ?? "Cancel"}
      >
        {ask ? (
          <div className="modal-body dlg-body">
            <h2 id="files-ask-title">{ask.title}</h2>
            <p>{ask.body}</p>
            <div className="dlg-actions">
              <button
                className="btn"
                onClick={() => {
                  ask.resolve(false);
                  setAsk(null);
                }}
              >
                {ask.cancel}
              </button>
              <button
                className={`btn ${ask.danger ? "danger" : "dark"}`}
                onClick={() => {
                  ask.resolve(true);
                  setAsk(null);
                }}
              >
                {ask.ok}
              </button>
            </div>
          </div>
        ) : null}
      </Modal>
    </section>
  );
}

// -- the file pane --------------------------------------------------------------------

interface FilePaneProps {
  open: OpenFile;
  envLabel: string;
  doc: FileText | null;
  page: FilePage | null;
  pageBusy: boolean;
  pageStale: { revision: string; mtime: number | null } | null;
  mdTab: "preview" | "text";
  editTab: "write" | "preview";
  editing: boolean;
  dirty: boolean;
  draft: string;
  baseRevision: string;
  conflict: Conflict | null;
  saving: boolean;
  notice: Notice;
  onBack: () => void;
  onMdTab: (t: "preview" | "text") => void;
  onEditTab: (t: "write" | "preview") => void;
  onEdit: () => void;
  onCancel: () => void;
  onSave: () => void;
  onDraft: (t: string) => void;
  onCompare: () => void;
  onUseNewer: () => void;
  onOverwrite: () => void;
  onPage: (n: number, revision?: string) => void;
  onRetry: () => void;
}

const EDIT_BLOCKED: Record<string, string> = {
  invalid_utf8: "relay edits UTF-8 text only. This file has bytes that are not UTF-8.",
  mixed_line_endings: "This file mixes line endings. relay does not edit it, so the endings stay unchanged.",
  read_only: "The file or its folder is read-only.",
};

function FilePane(props: FilePaneProps) {
  const { open, envLabel, doc, page, pageStale, editing, dirty, conflict, saving, notice } = props;
  const meta: FileMeta | null = page ?? doc ?? open.meta;
  const parent = parentOf(open.path);
  const where = `${envLabel} · ${parent || "projects"}`;
  const download = fileRawUrl(open.env, open.path, { download: true });

  const head = (
    <div className="fp-head">
      <button className="back" onClick={props.onBack} aria-label={`Back to ${baseName(parent) || "projects"}`}>
        <BackIcon />
      </button>
      <div className="fp-title">
        <strong>{open.name}</strong>
        <small>{where}</small>
      </div>
      {meta ? (
        <span className="pill plain">
          {meta.mode === "paged"
            ? "Text, paged view"
            : meta.reason === "too_large"
              ? "Over the paged limit, download only"
              : kindLabel(meta.kind, meta.lang, meta.active)}
        </span>
      ) : null}
    </div>
  );

  if (open.error)
    return (
      <>
        {head}
        <div className="banner conflict" role="alert">
          <strong>relay cannot open this file.</strong>
          <p>{open.error}</p>
          <div className="acts">
            <button className="btn sm" onClick={props.onRetry}>
              Try again
            </button>
          </div>
        </div>
      </>
    );
  if (!meta || (meta.mode === "edit" && !doc) || (meta.mode === "paged" && !page))
    return (
      <>
        {head}
        <div className="fp-body">
          <p className="media-note" role="status">
            Loading…
          </p>
        </div>
      </>
    );

  const statusLine = (
    <span className="st">
      {meta.mode === "paged"
        ? `${fmtSize(meta.size)} (${fmtInt(meta.size)} bytes) · saved ${fmtWhen(meta.mtime)} · revision ${meta.revision.slice(0, 6)}`
        : `Saved ${fmtWhen(meta.mtime)} · ${fmtSize(meta.size)} · revision ${meta.revision.slice(0, 6)}`}
    </span>
  );

  const noticeBanner =
    notice && notice.kind !== "saved" ? (
      <div className={`banner ${notice.kind === "error" ? "conflict" : "info"}`} role={notice.kind === "error" ? "alert" : "status"}>
        {notice.title ? <strong>{notice.title}</strong> : null}
        <p>{notice.text}</p>
      </div>
    ) : null;

  // Text up to the edit limit.
  if (meta.mode === "edit" && doc) {
    const isMd = doc.kind === "markdown";
    const tab = editing ? props.editTab : props.mdTab;
    let status: ReactNode;
    if (saving) status = <span className="st saving">Saving…</span>;
    else if (editing && dirty) status = <span className="st dirty">Unsaved changes</span>;
    else if (editing) status = <span className="st">Editing · no changes yet</span>;
    else if (notice?.kind === "saved")
      status = (
        <span className="st saved">
          <CheckIcon /> {notice.text}
        </span>
      );
    else status = statusLine;
    let body: ReactNode;
    if (editing && !(isMd && tab === "preview")) {
      body = (
        <>
          <label className="sr-only" htmlFor="file-editor">
            File text of {open.name}
          </label>
          <textarea
            id="file-editor"
            className="editor"
            spellCheck={false}
            autoCapitalize="off"
            autoComplete="off"
            autoCorrect="off"
            value={props.draft}
            onChange={(e) => props.onDraft(e.target.value)}
          />
        </>
      );
    } else if (isMd && tab === "preview") {
      body = <Markdown source={editing ? props.draft : doc.text} label={editing ? "Preview of your draft" : "Markdown preview"} />;
    } else {
      body = <CodeView text={doc.text} lang={doc.kind === "code" ? doc.lang : null} label={`Text of ${open.name}`} />;
    }
    return (
      <>
        {head}
        <div className="fp-bar">
          {isMd ? (
            <div className="seg" role="group" aria-label="View">
              {(editing
                ? ([
                    ["write", "Write"],
                    ["preview", "Preview"],
                  ] as const)
                : ([
                    ["preview", "Preview"],
                    ["text", "Text"],
                  ] as const)
              ).map(([k, label]) => (
                <button
                  key={k}
                  aria-pressed={tab === k}
                  onClick={() => (editing ? props.onEditTab(k as "write" | "preview") : props.onMdTab(k as "preview" | "text"))}
                >
                  {label}
                </button>
              ))}
            </div>
          ) : null}
          <span className="grow" />
          {editing ? (
            <>
              <button className="btn" onClick={props.onCancel} disabled={saving}>
                Cancel
              </button>
              <button className="btn dark" onClick={props.onSave} disabled={!dirty || saving || conflict != null}>
                {saving ? "Saving…" : "Save"}
              </button>
            </>
          ) : doc.editable ? (
            <button className="btn dark" onClick={props.onEdit}>
              Edit
            </button>
          ) : (
            <>
              <span className="pill plain">View only</span>
              <a className="btn" href={download} download={open.name}>
                <DownloadIcon />
                Download
              </a>
              <p className="edit-note">{EDIT_BLOCKED[doc.edit_blocked ?? ""] ?? "relay cannot edit this file."}</p>
            </>
          )}
        </div>
        <div className="fp-status" role="status">
          {status}
        </div>
        {conflict && editing ? (
          <div className="banner conflict" role="alert">
            <strong>This file changed while you edited it.</strong>
            <p>
              A newer version was saved at {fmtWhen(conflict.mtime)}, by an agent or another program. relay did not save your
              changes.
            </p>
            <div className="acts">
              <button className="btn sm" onClick={props.onCompare} aria-expanded={conflict.compare}>
                {conflict.compare ? "Hide compare" : "Compare"}
              </button>
              <button className="btn sm" onClick={props.onUseNewer}>
                Use the newer version
              </button>
              <button className="btn sm danger" onClick={props.onOverwrite}>
                Overwrite with mine
              </button>
            </div>
            {conflict.compare ? <Diff disk={conflict.text} mine={props.draft} /> : null}
          </div>
        ) : null}
        {noticeBanner}
        {doc.invalid_utf8 ? (
          <div className="banner note">
            <p>This file has bytes that are not UTF-8. relay shows them as � and opens the file as view only.</p>
          </div>
        ) : null}
        {doc.active ? (
          <div className="banner note">
            <p>
              Shown as text.{" "}
              {doc.lang === "SVG"
                ? "An SVG file can contain scripts, so relay does not draw it in its own page. Download it to view the image."
                : "relay does not run HTML or XML files in its own page. Open the running site from Previews."}
            </p>
          </div>
        ) : null}
        <div className="fp-body">{body}</div>
      </>
    );
  }

  // Paged view for large text.
  if (meta.mode === "paged" && page) {
    const pager = (bottom: boolean) => (
      <div className={`pager${bottom ? " bottom" : ""}`} role="group" aria-label="Pages">
        <button
          className="btn sm"
          disabled={page.page === 0 || pageStale != null || props.pageBusy}
          onClick={() => props.onPage(page.page - 1, page.revision)}
        >
          ‹ Previous
        </button>
        <span className="range" aria-live={bottom ? undefined : "polite"}>
          Lines {fmtInt(page.start_line)}–{fmtInt(page.end_line)} of {fmtInt(page.total_lines)}
          <small>
            Page {fmtInt(page.page + 1)} of {fmtInt(page.pages)}
          </small>
        </span>
        <button
          className="btn sm"
          disabled={page.page >= page.pages - 1 || pageStale != null || props.pageBusy}
          onClick={() => {
            props.onPage(page.page + 1, page.revision);
            if (bottom) document.querySelector(".file-pane")?.scrollIntoView({ block: "start" });
          }}
        >
          Next ›
        </button>
      </div>
    );
    return (
      <>
        {head}
        <div className="fp-bar">
          <span className="pill plain">View only</span>
          <span className="grow" />
          <button className="btn" disabled aria-describedby="edit-note">
            Edit
          </button>
          <a className="btn" href={download} download={open.name}>
            <DownloadIcon />
            Download
          </a>
          <p className="edit-note" id="edit-note">
            Full editing is available up to {fmtSize(meta.limits.edit)}. This file is {fmtSize(meta.size)}.
          </p>
        </div>
        <div className="fp-status" role="status">
          {statusLine}
        </div>
        {pageStale ? (
          <div className="banner note" role="alert">
            <strong>This file changed while you read it.</strong>
            <p>
              A new version was saved{pageStale.mtime ? ` at ${fmtWhen(pageStale.mtime)}` : ""}. Line numbers can shift, so paging
              stopped until you reload.
            </p>
            <div className="acts">
              <button className="btn sm dark" onClick={() => props.onPage(page.page)}>
                Reload this page
              </button>
              <button className="btn sm" onClick={() => props.onPage(0)}>
                Reload from line 1
              </button>
            </div>
          </div>
        ) : null}
        {page.invalid_utf8 ? (
          <div className="banner note">
            <p>This page has bytes that are not UTF-8. relay shows them as �.</p>
          </div>
        ) : null}
        <div className="fp-body">
          {pager(false)}
          <CodeView
            text={page.text}
            start={page.start_line}
            label={`Lines ${page.start_line} to ${page.end_line} of ${open.name}`}
            continued={page.continued}
            continues={page.continues}
          />
          {pager(true)}
          <p className="media-note">
            View only. relay loads one page of at most {page.page_lines} lines and {fmtSize(page.page_bytes)} at a time.
          </p>
        </div>
      </>
    );
  }

  const viewBar = (
    <div className="fp-bar">
      <span className="pill plain">View only</span>
      <span className="grow" />
      <a className="btn" href={download} download={open.name}>
        <DownloadIcon />
        Download
      </a>
    </div>
  );

  if (meta.mode === "image")
    return (
      <>
        {head}
        {viewBar}
        {noticeBanner}
        <div className="fp-body">
          <ImageView key={`${open.env}:${open.path}:${meta.revision}`} src={fileRawUrl(open.env, open.path, { revision: meta.revision })} meta={meta} download={download} />
        </div>
      </>
    );

  if (meta.mode === "video")
    return (
      <>
        {head}
        {viewBar}
        {noticeBanner}
        <div className="fp-body">
          <VideoView key={`${open.env}:${open.path}:${meta.revision}`} src={fileRawUrl(open.env, open.path, { revision: meta.revision })} meta={meta} download={download} />
        </div>
      </>
    );

  // Download only: binary or over the paged limit.
  return (
    <>
      {head}
      {viewBar}
      <div className="fp-body">
        {meta.reason === "too_large" ? (
          <Fallback
            icon={<LargeFileIcon />}
            title="Too large to open as text."
            text={`${fmtSize(meta.size)} (${fmtInt(meta.size)} bytes). Text files up to ${fmtSize(meta.limits.paged)} open in a paged viewer. Larger files are download only.`}
            download={download}
            name={open.name}
          />
        ) : (
          <Fallback
            icon={<ArchiveIcon />}
            title="No preview for this file type."
            text={`${fmtSize(meta.size)}. Binary files never open in the text editor.`}
            download={download}
            name={open.name}
          />
        )}
      </div>
    </>
  );
}

function Fallback({ icon, title, text, download, name }: { icon: ReactNode; title: string; text: string; download: string; name: string }) {
  return (
    <div className="fallback">
      <div className="ico">{icon}</div>
      <h3>{title}</h3>
      <p>{text}</p>
      <a className="btn dark" href={download} download={name}>
        <DownloadIcon />
        Download
      </a>
    </div>
  );
}

function Diff({ disk, mine }: { disk: string; mine: string }) {
  const rows = useMemo(() => diffLines(disk, mine), [disk, mine]);
  return (
    <div className="diff" tabIndex={0} aria-label="Compare the newer version with your version">
      <div className="legend">− only in the newer version on disk · + only in your version</div>
      {rows == null ? (
        <div className="gap">The versions differ in too many lines to compare here. Download the file to compare it in another app.</div>
      ) : (
        rows.map((r, k) =>
          r.t === "gap" ? (
            <div key={k} className="gap">
              ⋯
            </div>
          ) : (
            <div key={k} className={r.t === "-" ? "del" : r.t === "+" ? "add" : undefined}>
              {r.t} {r.s}
            </div>
          ),
        )
      )}
    </div>
  );
}

function ImageView({ src, meta, download }: { src: string; meta: FileMeta; download: string }) {
  const [zoom, setZoom] = useState<"fit" | number>("fit");
  const [failed, setFailed] = useState(false);
  const [dims, setDims] = useState<{ w: number; h: number; shown: number } | null>(null);
  const img = useRef<HTMLImageElement>(null);
  const measure = () => {
    const el = img.current;
    if (el && el.naturalWidth) setDims({ w: el.naturalWidth, h: el.naturalHeight, shown: el.clientWidth });
  };
  useEffect(() => {
    const el = img.current;
    if (!el) return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  if (failed) {
    const ext = meta.name.split(".").pop()?.toUpperCase() ?? "this";
    return (
      <Fallback
        icon={<ImageIcon />}
        title={`This browser cannot show ${ext} images.`}
        text={`Download ${meta.name} to open it in another app.`}
        download={download}
        name={meta.name}
      />
    );
  }
  const current = zoom === "fit" ? (dims ? dims.shown / dims.w : 1) : zoom;
  const step = (dir: 1 | -1) => setZoom(Math.min(4, Math.max(0.1, dir > 0 ? current * 1.25 : current / 1.25)));
  return (
    <>
      <div className="media-bar" role="group" aria-label="Zoom">
        <button className="btn sm" aria-pressed={zoom === "fit"} onClick={() => setZoom("fit")}>
          Fit
        </button>
        <button className="btn sm" aria-pressed={zoom === 1} onClick={() => setZoom(1)}>
          100%
        </button>
        <button className="btn sm" aria-label="Zoom out" onClick={() => step(-1)}>
          −
        </button>
        <button className="btn sm" aria-label="Zoom in" onClick={() => step(1)}>
          +
        </button>
        <span className="zoom-label" aria-live="polite">
          {zoom === "fit" ? `Fit · ${Math.round(current * 100)}%` : `${Math.round(current * 100)}%`}
        </span>
      </div>
      <div className="img-stage">
        <img
          ref={img}
          src={src}
          alt={meta.name}
          draggable={false}
          className={zoom === "fit" ? "fit" : undefined}
          style={zoom === "fit" || !dims ? undefined : { width: Math.round(dims.w * zoom) }}
          onLoad={measure}
          onError={() => setFailed(true)}
        />
      </div>
      <p className="media-note">
        View only{dims ? ` · ${dims.w} × ${dims.h}` : ""} · {meta.mime} · {fmtSize(meta.size)}
      </p>
    </>
  );
}

function VideoView({ src, meta, download }: { src: string; meta: FileMeta; download: string }) {
  const [failed, setFailed] = useState(false);
  const [duration, setDuration] = useState<number | null>(null);
  if (failed)
    return (
      <Fallback
        icon={<VideoIcon />}
        title="This browser cannot play this video."
        text="Download the file to play it in another app."
        download={download}
        name={meta.name}
      />
    );
  return (
    <>
      <div className="video-wrap">
        <video
          controls
          preload="metadata"
          playsInline
          src={src}
          onLoadedMetadata={(e) => {
            const d = e.currentTarget.duration;
            if (Number.isFinite(d)) setDuration(d);
          }}
          onError={() => setFailed(true)}
        />
      </div>
      <p className="media-note">
        View only · {meta.mime} · {fmtSize(meta.size)}
        {duration != null ? ` · ${duration.toFixed(1)} s` : ""} · playback uses this browser's codecs. The video streams in parts
        (HTTP Range).
      </p>
    </>
  );
}
