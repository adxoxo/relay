"""Confined project files for the Files page.

Every request names a path relative to an environment's ``projects_root``
(``/home/node/projects`` in the container). relay reads the files on the host
through the bind mount, but it resolves each path in the container's path
space, never in the host's:

- Each component is opened relative to the previous directory descriptor with
  ``O_NOFOLLOW``, so the kernel never follows a link on relay's behalf. A
  component that is swapped between ``lstat`` and ``open`` is detected by its
  inode and refused.
- relay reads every symlink itself. A relative target continues from the
  link's directory. An absolute target is a container path: it must lie under
  ``projects_root`` and it is never read as a host path.
- ``..`` above the root, a target outside the root, another filesystem, or
  more than 40 links refuse the request.
- Only regular files open. A FIFO opens with ``O_NONBLOCK`` and ``fstat`` then
  refuses it, as it refuses a socket or a device.

A save writes a temporary file in the target's directory, flushes it with
``fsync``, copies the owner and the mode bits, and renames it over the target
with ``os.replace``. A symlink therefore keeps pointing at its (new) target.
``os.replace`` gives the path a new inode, so another hard link to the old
file keeps the old content.

All functions here are blocking. The routes run them in a worker thread.
"""

from __future__ import annotations

import codecs
import hashlib
import os
import secrets
import stat
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Iterator

from .config import EnvConfig, Settings
from .errors import ApiError

PAGE_LINES = 200
PAGE_BYTES = 256 * 1024
SNIFF_BYTES = 8192
MAX_LINKS = 40
MAX_PATH = 4096
LIST_MAX = 5000            # entries per folder listing
COUNT_MAX = 1000           # items counted inside one subfolder
COUNT_DIRS_MAX = 300       # subfolders that get an item count
LINK_RESOLVE_MAX = 500     # symlinks classified per listing
SEARCH_RESULTS = 50
SEARCH_COLLECT = 1000
SEARCH_WALK_MAX = 60_000   # directory entries visited per search
SEARCH_SECONDS = 3.0
SEARCH_SKIP_DIRS = frozenset({".git", "node_modules"})
STREAM_CHUNK = 256 * 1024
PAGE_INDEX_CACHE = 4

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

MARKDOWN_EXT = frozenset({"md", "markdown", "mdx"})
CODE_LANG = {
    "ts": "TypeScript", "tsx": "TypeScript", "js": "JavaScript", "jsx": "JavaScript", "mjs": "JavaScript",
    "cjs": "JavaScript", "py": "Python", "json": "JSON", "jsonc": "JSON", "css": "CSS", "scss": "SCSS",
    "less": "Less", "html": "HTML", "htm": "HTML", "xhtml": "HTML", "xml": "XML", "svg": "SVG",
    "sh": "Shell", "bash": "Shell", "zsh": "Shell", "yaml": "YAML", "yml": "YAML", "toml": "TOML",
    "ini": "INI", "cfg": "INI", "sql": "SQL", "go": "Go", "rs": "Rust", "rb": "Ruby", "java": "Java",
    "kt": "Kotlin", "swift": "Swift", "c": "C", "h": "C", "cpp": "C++", "hpp": "C++", "cs": "C#",
    "php": "PHP", "lua": "Lua", "vue": "Vue", "svelte": "Svelte", "astro": "Astro", "graphql": "GraphQL",
    "dart": "Dart", "r": "R", "pl": "Perl",
}
CODE_NAMES = {"dockerfile": "Dockerfile", "makefile": "Makefile"}
TEXT_EXT = frozenset({"txt", "log", "csv", "tsv", "env", "gitignore", "lock", "conf", "rst", "text", "out", "properties"})
# Active formats: shown as text, never rendered in the relay origin.
ACTIVE_EXT = frozenset({"html", "htm", "xhtml", "xml", "svg", "xsl", "xslt"})
IMAGE_MIME = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp",
    "avif": "image/avif", "bmp": "image/bmp", "ico": "image/x-icon", "heic": "image/heic", "heif": "image/heif",
    "tif": "image/tiff", "tiff": "image/tiff",
}
VIDEO_MIME = {"mp4": "video/mp4", "m4v": "video/mp4", "webm": "video/webm", "ogv": "video/ogg", "mov": "video/quicktime"}
INLINE_MIME = frozenset(IMAGE_MIME.values()) | frozenset(VIDEO_MIME.values())


# -- paths -------------------------------------------------------------------

def split_path(rel: Any) -> list[str]:
    """Validate a request path and return its components ('' is the root)."""
    if not isinstance(rel, str) or len(rel) > MAX_PATH or "\x00" in rel:
        raise ApiError(400, "invalid_path", "The path is not valid.")
    if rel.startswith("/"):
        raise ApiError(400, "invalid_path", "Use a path relative to the projects folder, not an absolute path.")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise ApiError(400, "invalid_path", "The path must not contain '..'.")
    if any(len(p.encode("utf-8", "surrogateescape")) > 255 for p in parts):
        raise ApiError(400, "invalid_path", "A path component is too long.")
    return parts


def _outside() -> ApiError:
    return ApiError(403, "outside_root", "The path leads outside the projects folder of this workspace.")


def _not_found() -> ApiError:
    return ApiError(404, "not_found", "The file or folder does not exist.")


def _changed() -> ApiError:
    return ApiError(409, "path_changed", "The path changed while relay opened it. Try again.")


def _root(env: EnvConfig) -> tuple[list[str], str]:
    root_parts = [p for p in env.projects_root.split("/") if p]
    host = env.host_path(env.projects_root)
    if host is None:
        raise ApiError(500, "files_unavailable", "The projects folder has no host mapping.")
    return root_parts, str(host)


@dataclass
class _Walk:
    """The result of a container-path resolution.

    ``fd`` is the directory that holds ``name``. When ``name`` is None the
    target is the directory ``fd`` itself. The caller owns ``fd``.
    """

    fd: int
    name: str | None
    st: os.stat_result
    parts: list[str]       # resolved components under the root

    @property
    def rel(self) -> str:
        return "/".join(self.parts)

    def close(self) -> None:
        os.close(self.fd)


def _resolve(env: EnvConfig, parts: list[str]) -> _Walk:
    root_parts, root_host = _root(env)
    try:
        root_fd = os.open(root_host, _DIR_FLAGS)
    except OSError:
        raise ApiError(503, "files_unavailable", "The projects folder of this workspace is not available.") from None
    root_st = os.fstat(root_fd)
    stack = [root_fd]
    names: list[str] = []
    pending = list(parts)
    links = 0
    final: tuple[str, os.stat_result] | None = None
    try:
        while pending:
            comp = pending.pop(0)
            if comp in ("", "."):
                continue
            if comp == "..":
                if not names:
                    raise _outside()
                os.close(stack.pop())
                names.pop()
                continue
            cur = stack[-1]
            try:
                st = os.stat(comp, dir_fd=cur, follow_symlinks=False)
            except FileNotFoundError:
                raise _not_found() from None
            except NotADirectoryError:
                raise _not_found() from None
            except PermissionError:
                raise ApiError(403, "permission_denied", "relay cannot read this path.") from None
            if stat.S_ISLNK(st.st_mode):
                links += 1
                if links > MAX_LINKS:
                    raise ApiError(400, "link_loop", "The path has too many symbolic links.")
                target = os.readlink(comp, dir_fd=cur)
                tparts = [p for p in target.split("/") if p not in ("", ".")]
                if target.startswith("/"):
                    # A container path. Only a path under projects_root maps here.
                    if tparts[: len(root_parts)] != root_parts:
                        raise _outside()
                    while len(stack) > 1:
                        os.close(stack.pop())
                    names.clear()
                    tparts = tparts[len(root_parts):]
                pending = tparts + pending
                continue
            if st.st_dev != root_st.st_dev:
                raise _outside()
            if not pending:
                final = (comp, st)
                break
            if not stat.S_ISDIR(st.st_mode):
                raise _not_found()
            try:
                fd = os.open(comp, _DIR_FLAGS, dir_fd=cur)
            except FileNotFoundError:
                raise _not_found() from None
            except OSError:
                raise _changed() from None
            fst = os.fstat(fd)
            if (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
                os.close(fd)
                raise _changed()
            stack.append(fd)
            names.append(comp)
        top_st = os.fstat(stack[-1])
        top = stack.pop()
        while stack:
            os.close(stack.pop())
        if final is None:
            return _Walk(top, None, top_st, names)
        return _Walk(top, final[0], final[1], names + [final[0]])
    except BaseException:
        for fd in stack:
            os.close(fd)
        raise


def open_dir(env: EnvConfig, rel: str) -> tuple[int, str]:
    """Open a folder under the root. Return ``(fd, resolved relative path)``."""
    walk = _resolve(env, split_path(rel))
    if walk.name is None:
        return walk.fd, walk.rel
    try:
        if not stat.S_ISDIR(walk.st.st_mode):
            raise ApiError(400, "not_a_folder", "The path is not a folder.")
        try:
            fd = os.open(walk.name, _DIR_FLAGS, dir_fd=walk.fd)
        except OSError:
            raise _changed() from None
        fst = os.fstat(fd)
        if (fst.st_dev, fst.st_ino) != (walk.st.st_dev, walk.st.st_ino):
            os.close(fd)
            raise _changed()
        return fd, walk.rel
    finally:
        walk.close()


@dataclass
class OpenFile:
    parent_fd: int
    name: str
    fd: int
    st: os.stat_result
    rel: str

    def close(self) -> None:
        for fd in (self.fd, self.parent_fd):
            try:
                os.close(fd)
            except OSError:
                pass


def open_file(env: EnvConfig, rel: str) -> OpenFile:
    """Open a regular file under the root for reading."""
    walk = _resolve(env, split_path(rel))
    if walk.name is None:
        walk.close()
        raise ApiError(400, "not_a_file", "The path is a folder.")
    if stat.S_ISDIR(walk.st.st_mode):
        walk.close()
        raise ApiError(400, "not_a_file", "The path is a folder.")
    if not stat.S_ISREG(walk.st.st_mode):
        walk.close()
        raise ApiError(400, "not_regular", "relay opens regular files only. This is a FIFO, a socket, or a device.")
    try:
        fd = os.open(walk.name, _FILE_FLAGS, dir_fd=walk.fd)
    except FileNotFoundError:
        walk.close()
        raise _not_found() from None
    except PermissionError:
        walk.close()
        raise ApiError(403, "permission_denied", "relay cannot read this file.") from None
    except OSError:
        walk.close()
        raise _changed() from None
    fst = os.fstat(fd)
    if not stat.S_ISREG(fst.st_mode) or (fst.st_dev, fst.st_ino) != (walk.st.st_dev, walk.st.st_ino):
        os.close(fd)
        walk.close()
        raise _changed()
    return OpenFile(walk.fd, walk.name, fd, fst, walk.rel)


# -- classification ----------------------------------------------------------

def revision(st: os.stat_result) -> str:
    """A short token that changes when the file content or identity changes."""
    raw = f"{st.st_dev}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}:{st.st_ctime_ns}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _ext(name: str) -> str:
    base = name.rsplit("/", 1)[-1]
    if "." not in base.lstrip("."):
        return base.lstrip(".").lower() if base.startswith(".") else ""
    return base.rsplit(".", 1)[-1].lower()


def kind_by_name(name: str) -> dict[str, Any]:
    """Kind from the name only (listings and search)."""
    ext = _ext(name)
    lower = name.lower()
    if ext in IMAGE_MIME:
        return {"kind": "image", "lang": None}
    if ext in VIDEO_MIME:
        return {"kind": "video", "lang": None}
    if ext in MARKDOWN_EXT:
        return {"kind": "markdown", "lang": None}
    if lower in CODE_NAMES:
        return {"kind": "code", "lang": CODE_NAMES[lower]}
    if ext in CODE_LANG:
        return {"kind": "code", "lang": CODE_LANG[ext]}
    if ext in TEXT_EXT:
        return {"kind": "text", "lang": None}
    return {"kind": "file", "lang": None}


def _looks_text(head: bytes) -> bool:
    if b"\x00" in head:
        return False
    try:
        codecs.getincrementaldecoder("utf-8")().decode(head, final=False)
    except UnicodeDecodeError:
        return False
    return True


def classify(name: str, size: int, head: bytes, settings: Settings) -> dict[str, Any]:
    """Decide how the Files page shows a file."""
    by_name = kind_by_name(name)
    ext = _ext(name)
    info: dict[str, Any] = {"kind": by_name["kind"], "lang": by_name["lang"], "active": ext in ACTIVE_EXT, "mime": None}
    if ext in IMAGE_MIME:
        info.update(mode="image", mime=IMAGE_MIME[ext])
        return info
    if ext in VIDEO_MIME:
        info.update(mode="video", mime=VIDEO_MIME[ext])
        return info
    textual = _looks_text(head) or (b"\x00" not in head and by_name["kind"] in ("markdown", "code", "text"))
    if not textual:
        info.update(kind="binary", mode="download", reason="binary")
        return info
    if info["kind"] == "file":
        info["kind"] = "text"
    if size <= settings.files_edit_max_bytes:
        info["mode"] = "edit"
    elif size <= settings.files_paged_max_bytes:
        info["mode"] = "paged"
    else:
        info.update(mode="download", reason="too_large")
    return info


def _read_head(fd: int, size: int) -> bytes:
    return os.pread(fd, min(SNIFF_BYTES, max(size, 0)), 0) if size else b""


def file_meta(settings: Settings, f: OpenFile) -> dict[str, Any]:
    info = classify(f.name, f.st.st_size, _read_head(f.fd, f.st.st_size), settings)
    return {
        "path": f.rel,
        "name": f.name,
        "size": f.st.st_size,
        "mtime": f.st.st_mtime,
        "revision": revision(f.st),
        "writable": _writable(f),
        "hard_links": f.st.st_nlink,
        "limits": {"edit": settings.files_edit_max_bytes, "paged": settings.files_paged_max_bytes},
        **info,
    }


def _writable(f: OpenFile) -> bool:
    try:
        return os.access(f.name, os.W_OK, dir_fd=f.parent_fd, follow_symlinks=False) and os.access(
            ".", os.W_OK, dir_fd=f.parent_fd
        )
    except (OSError, NotImplementedError):
        return False


def meta(env: EnvConfig, settings: Settings, rel: str) -> dict[str, Any]:
    f = open_file(env, rel)
    try:
        return file_meta(settings, f)
    finally:
        f.close()


# -- listing and search ------------------------------------------------------

def _count_items(dir_fd: int, name: str, hidden: bool) -> int | None:
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        n = 0
        with os.scandir(fd) as it:
            for e in it:
                if not hidden and e.name.startswith("."):
                    continue
                n += 1
                if n >= COUNT_MAX:
                    break
        return n
    except OSError:
        return None
    finally:
        os.close(fd)


def list_dir(env: EnvConfig, rel: str, *, hidden: bool) -> dict[str, Any]:
    fd, resolved = open_dir(env, rel)
    try:
        root_dev = os.fstat(fd).st_dev
        entries: list[dict[str, Any]] = []
        truncated = False
        with os.scandir(fd) as it:
            for e in it:
                if not hidden and e.name.startswith("."):
                    continue
                if len(entries) >= LIST_MAX:
                    truncated = True
                    break
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                entries.append({"name": e.name, "st": st})
        out: list[dict[str, Any]] = []
        counted = 0
        links = 0
        prefix = f"{resolved}/" if resolved else ""
        for item in entries:
            name, st = item["name"], item["st"]
            row: dict[str, Any] = {"name": name, "path": prefix + name, "link": False}
            mode = st.st_mode
            if stat.S_ISLNK(mode):
                row["link"] = True
                links += 1
                if links > LINK_RESOLVE_MAX:
                    row["type"] = "link"
                else:
                    try:
                        walk = _resolve(env, split_path(prefix + name))
                    except ApiError as exc:
                        row["type"] = "link_outside" if exc.code == "outside_root" else "link_broken"
                    else:
                        try:
                            tst = walk.st
                            if stat.S_ISDIR(tst.st_mode):
                                row["type"] = "dir"
                            elif stat.S_ISREG(tst.st_mode):
                                row.update(type="file", size=tst.st_size, mtime=tst.st_mtime, **kind_by_name(name))
                            else:
                                row["type"] = "other"
                        finally:
                            walk.close()
            elif stat.S_ISDIR(mode):
                row["type"] = "dir"
                if st.st_dev == root_dev and counted < COUNT_DIRS_MAX:
                    counted += 1
                    row["items"] = _count_items(fd, name, hidden)
            elif stat.S_ISREG(mode):
                row.update(type="file", size=st.st_size, mtime=st.st_mtime, **kind_by_name(name))
            else:
                row["type"] = "other"
            out.append(row)
        out.sort(key=lambda r: (0 if r["type"] == "dir" else 1, r["name"].casefold(), r["name"]))
        return {"path": resolved, "root": env.projects_root, "entries": out, "truncated": truncated}
    finally:
        os.close(fd)


def search(env: EnvConfig, query: str, *, hidden: bool) -> dict[str, Any]:
    """Find file names that contain ``query`` (case-insensitive). Names only."""
    needle = query.casefold()
    _, root_host = _root(env)
    try:
        root_dev = os.stat(root_host, follow_symlinks=False).st_dev
    except OSError:
        raise ApiError(503, "files_unavailable", "The projects folder of this workspace is not available.") from None
    started = time.monotonic()
    visited = 0
    partial: str | None = None
    matches: list[tuple[str, str]] = []
    walker = os.fwalk(root_host, follow_symlinks=False)
    try:
        for dirpath, dirnames, filenames, dirfd in walker:
            if os.fstat(dirfd).st_dev != root_dev:
                dirnames[:] = []
                continue
            dirnames[:] = [
                d for d in dirnames if d not in SEARCH_SKIP_DIRS and (hidden or not d.startswith("."))
            ]
            visited += len(dirnames) + len(filenames)
            rel_dir = os.path.relpath(dirpath, root_host)
            rel_dir = "" if rel_dir == "." else rel_dir
            for name in filenames:
                if not hidden and name.startswith("."):
                    continue
                if needle in name.casefold():
                    matches.append((name, f"{rel_dir}/{name}" if rel_dir else name))
                    if len(matches) >= SEARCH_COLLECT:
                        partial = "result_limit"
                        break
            if partial:
                break
            if visited >= SEARCH_WALK_MAX:
                partial = "walk_limit"
                break
            if time.monotonic() - started > SEARCH_SECONDS:
                partial = "time_limit"
                break
    finally:
        walker.close()
    matches.sort(key=lambda m: (m[0].casefold(), m[1]))
    results = []
    for name, path in matches[:SEARCH_RESULTS]:
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        results.append({"name": name, "path": path, "parent": parent, **kind_by_name(name)})
    return {
        "query": query,
        "results": results,
        "total": len(matches),
        "limit": SEARCH_RESULTS,
        "partial": partial is not None,
        "reason": partial,
    }


# -- text --------------------------------------------------------------------

def _read_exact(fd: int, size: int, cap: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    offset = 0
    while total <= cap:
        chunk = os.pread(fd, min(1 << 20, cap + 1 - total), offset)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        offset += len(chunk)
    return b"".join(chunks)


def line_endings(data: bytes) -> str:
    crlf = data.count(b"\r\n")
    lf = data.count(b"\n") - crlf
    cr = data.count(b"\r") - crlf
    if crlf and not lf and not cr:
        return "crlf"
    if cr or (crlf and lf):
        return "mixed"
    return "lf"


def read_text(env: EnvConfig, settings: Settings, rel: str) -> dict[str, Any]:
    limit = settings.files_edit_max_bytes
    for _ in range(3):
        f = open_file(env, rel)
        try:
            meta_info = file_meta(settings, f)
            if meta_info["mode"] != "edit":
                if meta_info["mode"] == "paged" or meta_info.get("reason") == "too_large":
                    raise ApiError(413, "too_large", f"Full text opens up to {limit:,} bytes.", size=f.st.st_size)
                raise ApiError(415, "not_text", "This file is not text.")
            data = _read_exact(f.fd, f.st.st_size, limit)
            if len(data) > limit:
                raise ApiError(413, "too_large", f"Full text opens up to {limit:,} bytes.", size=len(data))
            if revision(os.fstat(f.fd)) != meta_info["revision"]:
                continue  # the file changed during the read
            invalid = False
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = data.decode("utf-8", "replace")
                invalid = True
            endings = line_endings(data)
            editable = meta_info["writable"] and not invalid and endings != "mixed" and b"\x00" not in data
            reason = None
            if invalid:
                reason = "invalid_utf8"
            elif endings == "mixed":
                reason = "mixed_line_endings"
            elif not meta_info["writable"]:
                reason = "read_only"
            return {
                **meta_info,
                "text": text,
                "invalid_utf8": invalid,
                "line_ending": endings,
                "editable": editable,
                "edit_blocked": reason,
            }
        finally:
            f.close()
    raise ApiError(409, "revision_changed", "The file keeps changing. Try again.")


# -- paged view --------------------------------------------------------------

@dataclass
class PageIndex:
    revision: str
    size: int
    pages: list[tuple[int, int, bool]]   # (offset, first line number, starts mid-line)
    total_lines: int


_index_cache: OrderedDict[tuple[str, str, str], PageIndex] = OrderedDict()
_index_lock = threading.Lock()


def _char_boundary(chunk: bytes) -> int:
    """Length of ``chunk`` without a UTF-8 sequence that the cut would split."""
    n = len(chunk)
    for back in range(1, min(4, n) + 1):
        b = chunk[n - back]
        if (b & 0xC0) == 0x80:
            continue  # a continuation byte: look further back for its lead byte
        if b < 0x80:
            need = 1
        elif (b & 0xE0) == 0xC0:
            need = 2
        elif (b & 0xF0) == 0xE0:
            need = 3
        elif (b & 0xF8) == 0xF0:
            need = 4
        else:
            need = 1
        return n if back >= need else n - back
    return n  # only continuation bytes: not UTF-8 here, keep the byte limit


def build_index(fd: int, size: int, rev: str) -> PageIndex:
    pages: list[tuple[int, int, bool]] = []
    offset = 0
    line = 1
    cont = False
    while offset < size:
        pages.append((offset, line, cont))
        chunk = os.pread(fd, PAGE_BYTES, offset)
        if not chunk:
            break
        pos = 0
        n = 0
        while n < PAGE_LINES:
            i = chunk.find(b"\n", pos)
            if i < 0:
                break
            pos = i + 1
            n += 1
        at_eof = offset + len(chunk) >= size
        if n == PAGE_LINES:
            length, cont = pos, False
        elif at_eof:
            length, cont = len(chunk), False
        elif n > 0:
            length, cont = pos, False
        else:
            length, cont = _char_boundary(chunk), True
        line += n
        offset += length
    total = line - 1
    if size and not _ends_with_newline(fd, size):
        total += 1
    return PageIndex(rev, size, pages, total)


def _ends_with_newline(fd: int, size: int) -> bool:
    return os.pread(fd, 1, size - 1) == b"\n"


def read_page(env: EnvConfig, settings: Settings, rel: str, page: int, want_revision: str | None) -> dict[str, Any]:
    f = open_file(env, rel)
    try:
        info = file_meta(settings, f)
        rev = info["revision"]
        if want_revision and want_revision != rev:
            raise ApiError(
                409,
                "revision_changed",
                "The file changed while you read it.",
                revision=rev,
                size=f.st.st_size,
                mtime=f.st.st_mtime,
            )
        if info["mode"] not in ("edit", "paged"):
            if info.get("reason") == "too_large":
                raise ApiError(413, "too_large", f"The paged view opens text up to {settings.files_paged_max_bytes:,} bytes.")
            raise ApiError(415, "not_text", "This file is not text.")
        key = (env.id, f.rel, rev)
        with _index_lock:
            index = _index_cache.get(key)
            if index is not None:
                _index_cache.move_to_end(key)
        if index is None:
            index = build_index(f.fd, f.st.st_size, rev)
            if revision(os.fstat(f.fd)) != rev:
                raise ApiError(409, "revision_changed", "The file changed while relay read it.", revision=revision(os.fstat(f.fd)))
            with _index_lock:
                _index_cache[key] = index
                while len(_index_cache) > PAGE_INDEX_CACHE:
                    _index_cache.popitem(last=False)
        count = max(1, len(index.pages))
        if page < 0 or page >= count:
            raise ApiError(400, "invalid_page", "The page does not exist.", pages=count)
        if index.pages:
            start, first_line, cont = index.pages[page]
            end = index.pages[page + 1][0] if page + 1 < len(index.pages) else index.size
            continues = index.pages[page + 1][2] if page + 1 < len(index.pages) else False
        else:
            start, first_line, cont, end, continues = 0, 1, False, 0, False
        data = os.pread(f.fd, end - start, start)
        if revision(os.fstat(f.fd)) != rev:
            raise ApiError(409, "revision_changed", "The file changed while relay read it.", revision=revision(os.fstat(f.fd)))
        try:
            text = data.decode("utf-8")
            invalid = False
        except UnicodeDecodeError:
            text = data.decode("utf-8", "replace")
            invalid = True
        body = text[:-1] if text.endswith("\n") else text
        lines_on_page = body.count("\n") + 1 if (body or cont or continues) else 0
        return {
            **info,
            "page": page,
            "pages": count,
            "page_lines": PAGE_LINES,
            "page_bytes": PAGE_BYTES,
            "start_line": first_line,
            "end_line": first_line + max(lines_on_page - 1, 0),
            "total_lines": index.total_lines,
            "continued": cont,
            "continues": continues,
            "text": text,
            "invalid_utf8": invalid,
        }
    finally:
        f.close()


# -- save --------------------------------------------------------------------

def save_text(env: EnvConfig, settings: Settings, rel: str, data: bytes, base_revision: str) -> dict[str, Any]:
    """Replace a text file atomically when its revision is still ``base_revision``."""
    if len(data) > settings.files_edit_max_bytes:
        raise ApiError(413, "too_large", f"relay saves text up to {settings.files_edit_max_bytes:,} bytes.")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        raise ApiError(400, "invalid_text", "The text is not valid UTF-8.") from None
    f = open_file(env, rel)
    try:
        current = revision(f.st)
        if current != base_revision:
            raise ApiError(
                409,
                "revision_changed",
                "The file changed after you opened it. relay did not save your changes.",
                revision=current,
                size=f.st.st_size,
                mtime=f.st.st_mtime,
            )
        info = classify(f.name, f.st.st_size, _read_head(f.fd, f.st.st_size), settings)
        if info["mode"] != "edit":
            raise ApiError(409, "not_editable", "This file is not editable in relay.")
        if not _writable(f):
            raise ApiError(403, "read_only", "The file or its folder is read-only.")
        st = f.st
        tmp = f".{f.name}.relay-{secrets.token_hex(6)}.tmp"
        if len(tmp.encode("utf-8", "surrogateescape")) > 255:
            tmp = f".relay-{secrets.token_hex(8)}.tmp"
        tfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=f.parent_fd)
        placed = False
        try:
            view = memoryview(data)
            while view:
                written = os.write(tfd, view)
                view = view[written:]
            if (st.st_uid, st.st_gid) != (os.geteuid(), os.getegid()):
                try:
                    os.fchown(tfd, st.st_uid, st.st_gid)
                except PermissionError:
                    raise ApiError(409, "owner_not_preserved", "relay cannot keep the file owner, so it did not save.") from None
            os.fchmod(tfd, stat.S_IMODE(st.st_mode))
            os.fsync(tfd)
            os.close(tfd)
            tfd = -1
            # Narrow the race with another writer: compare once more just before the rename.
            now_st = os.stat(f.name, dir_fd=f.parent_fd, follow_symlinks=False)
            if not stat.S_ISREG(now_st.st_mode) or revision(now_st) != current:
                raise ApiError(409, "revision_changed", "The file changed while relay saved it. relay did not save your changes.", revision=revision(now_st))
            os.replace(tmp, f.name, src_dir_fd=f.parent_fd, dst_dir_fd=f.parent_fd)
            placed = True
            os.fsync(f.parent_fd)
        finally:
            if tfd >= 0:
                os.close(tfd)
            if not placed:
                try:
                    os.unlink(tmp, dir_fd=f.parent_fd)
                except FileNotFoundError:
                    pass
        new_st = os.stat(f.name, dir_fd=f.parent_fd, follow_symlinks=False)
        return {
            "path": f.rel,
            "revision": revision(new_st),
            "size": new_st.st_size,
            "mtime": new_st.st_mtime,
            "hard_links_detached": st.st_nlink > 1,
        }
    finally:
        f.close()


# -- raw bytes ---------------------------------------------------------------

def parse_range(header: str | None, size: int) -> tuple[int, int] | None | str:
    """Parse one ``bytes=`` range. Return (start, end), None (no range), or 'invalid'."""
    if not header:
        return None
    unit, _, spec = header.partition("=")
    if unit.strip().lower() != "bytes" or "," in spec:
        return None  # several ranges: answer with the full body
    first, _, last = spec.strip().partition("-")
    try:
        if first == "":
            length = int(last)
            if length <= 0:
                return "invalid"
            start = max(size - length, 0)
            end = size - 1
        else:
            start = int(first)
            end = int(last) if last else size - 1
    except ValueError:
        return "invalid"
    if start < 0 or start >= size or end < start:
        return "invalid"
    return start, min(end, size - 1)


def iter_bytes(fd: int, start: int, end: int) -> Iterator[bytes]:
    """Yield ``fd`` bytes from ``start`` to ``end`` (inclusive)."""
    offset = start
    while offset <= end:
        chunk = os.pread(fd, min(STREAM_CHUNK, end - offset + 1), offset)
        if not chunk:
            break
        offset += len(chunk)
        yield chunk
