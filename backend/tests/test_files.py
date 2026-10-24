"""Files: confinement, symlinks in the container path space, limits, saves, pages, media.

Each test builds real files under the temporary host homes of the ``settings``
fixture (``<tmp>/work1`` and ``<tmp>/work2`` play ``/home/node``).
"""

from __future__ import annotations

import os
import socket
import stat

import pytest
from fastapi.testclient import TestClient

from relay import files
from relay.app import create_app
from relay.auth import Auth
from relay.errors import ApiError

BASE = "http://127.0.0.1:8765"


@pytest.fixture
def small(rt):
    """Small limits keep the files tiny: edit up to 1000 bytes, paged up to 600 KiB."""
    object.__setattr__(rt.settings, "files_edit_max_bytes", 1000)
    object.__setattr__(rt.settings, "files_paged_max_bytes", 600 * 1024)
    return rt


def root(rt, env="work1"):
    path = rt.settings.env(env).host_home / "projects"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def client(small):
    app = create_app(runtime=small, features=True, background=False)
    with TestClient(app, base_url=BASE) as c:
        token = Auth(small.settings, small.db).ensure_token()
        assert c.post("/api/auth/login", json={"token": token}).status_code == 204
        yield c


# -- confinement --------------------------------------------------------------

@pytest.mark.parametrize("bad", ["../work2/projects", "a/../../x", "/etc/passwd", "a\x00b"])
def test_traversal_absolute_and_nul_are_refused(small, bad):
    root(small)
    with pytest.raises(ApiError) as exc:
        files.meta(small.settings.env("work1"), small.settings, bad)
    assert exc.value.code == "invalid_path"


def test_fifo_socket_are_refused(small, tmp_path):
    r = root(small)
    os.mkfifo(r / "pipe")
    sock = socket.socket(socket.AF_UNIX)
    sock_path = r / "s.sock"
    sock.bind(str(sock_path))
    try:
        for name in ("pipe", "s.sock"):
            with pytest.raises(ApiError) as exc:
                files.read_text(small.settings.env("work1"), small.settings, name)
            assert exc.value.code == "not_regular"
        listing = files.list_dir(small.settings.env("work1"), "", hidden=False)
        kinds = {e["name"]: e["type"] for e in listing["entries"]}
        assert kinds["pipe"] == "other" and kinds["s.sock"] == "other"
    finally:
        sock.close()


def test_symlinks_resolve_in_the_container_path_space(small):
    env = small.settings.env("work1")
    r = root(small)
    (r / "app" / "docs").mkdir(parents=True)
    (r / "app" / "docs" / "guide.md").write_text("# Guide\n")
    # Relative link inside the root.
    os.symlink("docs/guide.md", r / "app" / "rel.md")
    # Absolute link written as a container path.
    os.symlink("/home/node/projects/app/docs", r / "app" / "abs-docs")
    assert files.read_text(env, small.settings, "app/rel.md")["text"] == "# Guide\n"
    assert files.read_text(env, small.settings, "app/abs-docs/guide.md")["text"] == "# Guide\n"
    listing = files.list_dir(env, "app", hidden=False)
    types = {e["name"]: (e["type"], e["link"]) for e in listing["entries"]}
    assert types["abs-docs"] == ("dir", True)
    assert types["rel.md"] == ("file", True)


def test_symlinks_that_leave_the_root_are_refused(small):
    env = small.settings.env("work1")
    r = root(small)
    w2 = root(small, "work2")
    (w2 / "secret.txt").write_text("work2 only\n")
    (small.settings.env("work1").host_home / "home.txt").write_text("outside projects\n")
    # A host path: never read as a host path. In the container it does not exist under the root.
    os.symlink(str(w2 / "secret.txt"), r / "host-abs")
    # A relative escape to the other workspace's host folder.
    os.symlink("../../work2/projects/secret.txt", r / "rel-escape")
    # A container path outside projects_root.
    os.symlink("/home/node/home.txt", r / "container-outside")
    os.symlink("/home/node/projects/../home.txt", r / "container-dotdot")
    for name in ("host-abs", "rel-escape", "container-outside", "container-dotdot"):
        with pytest.raises(ApiError) as exc:
            files.read_text(env, small.settings, name)
        assert exc.value.code == "outside_root", name
    listing = files.list_dir(env, "", hidden=False)
    types = {e["name"]: e["type"] for e in listing["entries"]}
    assert types["rel-escape"] == "link_outside"


def test_work1_never_reads_work2(small):
    root(small)
    w2 = root(small, "work2")
    (w2 / "only-in-work2.txt").write_text("w2\n")
    with pytest.raises(ApiError) as exc:
        files.read_text(small.settings.env("work1"), small.settings, "only-in-work2.txt")
    assert exc.value.code == "not_found"
    assert files.read_text(small.settings.env("work2"), small.settings, "only-in-work2.txt")["text"] == "w2\n"
    names = [r["name"] for r in files.search(small.settings.env("work1"), "only", hidden=False)["results"]]
    assert names == []


def test_link_loop_is_refused(small):
    r = root(small)
    os.symlink("b", r / "a")
    os.symlink("a", r / "b")
    with pytest.raises(ApiError) as exc:
        files.read_text(small.settings.env("work1"), small.settings, "a")
    assert exc.value.code == "link_loop"


# -- limits, read, save --------------------------------------------------------

def test_full_text_only_up_to_the_edit_limit(small):
    env = small.settings.env("work1")
    r = root(small)
    (r / "ok.txt").write_bytes(b"a" * 1000)
    (r / "big.txt").write_bytes(b"a" * 1001)
    assert len(files.read_text(env, small.settings, "ok.txt")["text"]) == 1000
    assert files.meta(env, small.settings, "big.txt")["mode"] == "paged"
    with pytest.raises(ApiError) as exc:
        files.read_text(env, small.settings, "big.txt")
    assert exc.value.status == 413


def test_stale_save_is_409_and_leaves_the_file_unchanged(small):
    env = small.settings.env("work1")
    r = root(small)
    target = r / "notes.md"
    target.write_text("one\n")
    base = files.meta(env, small.settings, "notes.md")["revision"]
    target.write_text("changed by an agent\n")
    with pytest.raises(ApiError) as exc:
        files.save_text(env, small.settings, "notes.md", b"mine\n", base)
    assert exc.value.status == 409 and exc.value.code == "revision_changed"
    assert exc.value.extra["revision"] != base
    assert target.read_text() == "changed by an agent\n"
    # Overwrite after the compare: the new base revision saves.
    result = files.save_text(env, small.settings, "notes.md", b"mine\n", exc.value.extra["revision"])
    assert target.read_text() == "mine\n"
    assert result["revision"] != exc.value.extra["revision"]


def test_save_is_atomic_and_keeps_mode_owner_and_symlink(small, monkeypatch):
    env = small.settings.env("work1")
    r = root(small)
    (r / "src").mkdir()
    real = r / "src" / "app.py"
    real.write_text("print(1)\n")
    os.chmod(real, 0o640)
    os.symlink("src/app.py", r / "link.py")
    calls = []
    real_replace, real_fsync = os.replace, os.fsync
    monkeypatch.setattr(files.os, "fsync", lambda fd: (calls.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(files.os, "replace", lambda *a, **k: (calls.append("replace"), real_replace(*a, **k))[1])
    base = files.meta(env, small.settings, "link.py")["revision"]
    files.save_text(env, small.settings, "link.py", b"print(2)\n", base)
    assert calls[:2] == ["fsync", "replace"]          # temp file flushed before the rename
    assert os.path.islink(r / "link.py")               # the link stays a link
    assert real.read_text() == "print(2)\n"            # its target got the text
    st = os.stat(real)
    assert stat.S_IMODE(st.st_mode) == 0o640
    assert st.st_uid == os.getuid()
    assert not [p for p in (r / "src").iterdir() if p.name.endswith(".tmp")]


def test_save_reports_detached_hard_links(small):
    env = small.settings.env("work1")
    r = root(small)
    (r / "a.txt").write_text("x\n")
    os.link(r / "a.txt", r / "b.txt")
    base = files.meta(env, small.settings, "a.txt")["revision"]
    result = files.save_text(env, small.settings, "a.txt", b"y\n", base)
    assert result["hard_links_detached"] is True
    assert (r / "b.txt").read_text() == "x\n"  # os.replace breaks the hard link


def test_save_over_the_limit_is_413(client, small):
    r = root(small)
    (r / "n.txt").write_text("x")
    rev = client.get("/api/files/work1/meta", params={"path": "n.txt"}).json()["revision"]
    resp = client.put(
        "/api/files/work1/text",
        params={"path": "n.txt", "base_revision": rev},
        content=b"a" * 1001,
        headers={"Origin": BASE, "Content-Type": "text/plain; charset=utf-8"},
    )
    assert resp.status_code == 413
    assert (r / "n.txt").read_text() == "x"


def test_crlf_and_mixed_endings(small):
    env = small.settings.env("work1")
    r = root(small)
    (r / "w.txt").write_bytes(b"a\r\nb\r\n")
    (r / "m.txt").write_bytes(b"a\r\nb\n")
    assert files.read_text(env, small.settings, "w.txt")["line_ending"] == "crlf"
    mixed = files.read_text(env, small.settings, "m.txt")
    assert mixed["editable"] is False and mixed["edit_blocked"] == "mixed_line_endings"


# -- paged view ----------------------------------------------------------------

def test_pages_hold_200_lines_and_report_totals(small):
    env = small.settings.env("work1")
    r = root(small)
    (r / "log.txt").write_bytes(b"".join(b"line %d\n" % i for i in range(1, 451)))
    first = files.read_page(env, small.settings, "log.txt", 0, None)
    assert (first["start_line"], first["end_line"], first["total_lines"], first["pages"]) == (1, 200, 450, 3)
    last = files.read_page(env, small.settings, "log.txt", 2, first["revision"])
    assert (last["start_line"], last["end_line"]) == (401, 450)
    assert last["text"].startswith("line 401\n")


def test_long_line_continues_and_utf8_is_never_split(small):
    env = small.settings.env("work1")
    r = root(small)
    # 300 KiB of 3-byte characters on one line, then a short line.
    data = ("€" * (300 * 1024 // 3)).encode() + b"\nend\n"
    (r / "wide.txt").write_bytes(data)
    p0 = files.read_page(env, small.settings, "wide.txt", 0, None)
    assert p0["continues"] is True and p0["invalid_utf8"] is False
    assert len(p0["text"].encode()) <= files.PAGE_BYTES
    assert set(p0["text"]) == {"€"}
    p1 = files.read_page(env, small.settings, "wide.txt", 1, p0["revision"])
    assert p1["continued"] is True and p1["start_line"] == p0["start_line"] == 1
    assert p1["text"].endswith("\nend\n")
    assert p0["text"] + p1["text"] == data.decode()


def test_invalid_utf8_is_marked(small):
    env = small.settings.env("work1")
    r = root(small)
    (r / "bad.log").write_bytes(b"ok\n" * 400 + b"\xff\xfe broken\n" + b"a" * 1000 + b"\n")
    page = files.read_page(env, small.settings, "bad.log", 2, None)
    assert page["invalid_utf8"] is True
    assert "�" in page["text"]


def test_revision_change_during_paging_is_409(small):
    env = small.settings.env("work1")
    r = root(small)
    path = r / "grow.log"
    path.write_bytes(b"x\n" * 1200)
    rev = files.read_page(env, small.settings, "grow.log", 0, None)["revision"]
    with open(path, "ab") as fh:
        fh.write(b"y\n" * 10)
    with pytest.raises(ApiError) as exc:
        files.read_page(env, small.settings, "grow.log", 1, rev)
    assert exc.value.status == 409 and exc.value.code == "revision_changed"


def test_over_paged_limit_is_download_only(small):
    env = small.settings.env("work1")
    r = root(small)
    (r / "huge.log").write_bytes(b"z" * (600 * 1024 + 1))
    assert files.meta(env, small.settings, "huge.log")["mode"] == "download"


# -- search --------------------------------------------------------------------

def test_search_names_only_hidden_toggle_and_skips(small):
    env = small.settings.env("work1")
    r = root(small)
    (r / "app" / "node_modules" / "x").mkdir(parents=True)
    (r / "app" / "node_modules" / "x" / "README.md").write_text("x")
    (r / ".git").mkdir()
    (r / ".git" / "README").write_text("x")
    (r / "app" / "README.md").write_text("no match in content")
    (r / "app" / ".readme-hidden").write_text("x")
    (r / "app" / "notes.txt").write_text("README inside the content only")
    found = files.search(env, "read", hidden=False)
    assert [x["path"] for x in found["results"]] == ["app/README.md"]
    with_hidden = files.search(env, "read", hidden=True)
    assert sorted(x["path"] for x in with_hidden["results"]) == ["app/.readme-hidden", "app/README.md"]


def test_search_walk_limit_reports_partial(small, monkeypatch):
    env = small.settings.env("work1")
    r = root(small)
    for i in range(30):
        (r / f"d{i}").mkdir()
        (r / f"d{i}" / f"file{i}.txt").write_text("x")
    monkeypatch.setattr(files, "SEARCH_WALK_MAX", 10)
    found = files.search(env, "file", hidden=False)
    assert found["partial"] is True and found["reason"] == "walk_limit"


# -- routes: auth, origin, media ----------------------------------------------------

def test_every_file_route_needs_a_session(small):
    root(small)
    app = create_app(runtime=small, features=True, background=False)
    with TestClient(app, base_url=BASE) as c:
        for method, url in [
            ("GET", "/api/files/work1/list"),
            ("GET", "/api/files/work1/search?q=a"),
            ("GET", "/api/files/work1/meta?path=a"),
            ("GET", "/api/files/work1/text?path=a"),
            ("GET", "/api/files/work1/page?path=a"),
            ("GET", "/api/files/work1/raw?path=a"),
            ("PUT", "/api/files/work1/text?path=a&base_revision=x"),
        ]:
            assert c.request(method, url).status_code == 401, url


def test_cookie_save_needs_an_allowed_origin(client, small):
    r = root(small)
    (r / "n.txt").write_text("x")
    rev = client.get("/api/files/work1/meta", params={"path": "n.txt"}).json()["revision"]
    params = {"path": "n.txt", "base_revision": rev}
    assert client.put("/api/files/work1/text", params=params, content=b"y").status_code == 403
    evil = client.put("/api/files/work1/text", params=params, content=b"y", headers={"Origin": "http://127.0.0.1:18501"})
    assert evil.status_code == 403
    ok = client.put("/api/files/work1/text", params=params, content=b"y", headers={"Origin": BASE})
    assert ok.status_code == 200 and (r / "n.txt").read_text() == "y"


def test_video_range_and_headers(client, small):
    r = root(small)
    (r / "clip.webm").write_bytes(bytes(range(256)) * 40)
    resp = client.get("/api/files/work1/raw", params={"path": "clip.webm"}, headers={"Range": "bytes=100-199"})
    assert resp.status_code == 206
    assert resp.headers["content-range"] == "bytes 100-199/10240"
    assert resp.content == (bytes(range(256)) * 40)[100:200]
    assert resp.headers["content-type"] == "video/webm"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["cache-control"] == "no-store"
    bad = client.get("/api/files/work1/raw", params={"path": "clip.webm"}, headers={"Range": "bytes=99999-"})
    assert bad.status_code == 416


@pytest.mark.parametrize("name", ["logo.svg", "index.html", "feed.xml", "doc.pdf", "unknown.bin"])
def test_active_and_unknown_types_are_attachments(client, small, name):
    r = root(small)
    (r / name).write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>")
    resp = client.get("/api/files/work1/raw", params={"path": name})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/octet-stream"
    assert resp.headers["content-disposition"].startswith("attachment;")
    assert "sandbox" in resp.headers["content-security-policy"]


def test_same_site_requests_from_a_preview_origin_are_refused(client, small):
    root(small)
    resp = client.get("/api/files/work1/list", headers={"Sec-Fetch-Site": "same-site"})
    assert resp.status_code == 403 and resp.json()["error"] == "bad_origin"
    assert client.get("/api/files/work1/list", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200


def test_media_head_full_and_ranged(client, small):
    r = root(small)
    (r / "clip.mp4").write_bytes(bytes(range(256)) * 8)
    full = client.head("/api/files/work1/raw", params={"path": "clip.mp4"})
    assert full.status_code == 200 and full.content == b""
    assert full.headers["content-length"] == "2048" and full.headers["accept-ranges"] == "bytes"
    assert full.headers["content-type"] == "video/mp4" and full.headers["x-content-type-options"] == "nosniff"
    part = client.head("/api/files/work1/raw", params={"path": "clip.mp4"}, headers={"Range": "bytes=10-19"})
    assert part.status_code == 206 and part.content == b""
    assert part.headers["content-range"] == "bytes 10-19/2048" and part.headers["content-length"] == "10"


def test_media_head_needs_a_session(small):
    r = root(small)
    (r / "clip.mp4").write_bytes(b"x" * 10)
    app = create_app(runtime=small, features=True, background=False)
    with TestClient(app, base_url=BASE) as c:
        assert c.head("/api/files/work1/raw", params={"path": "clip.mp4"}).status_code == 401


def test_media_url_is_pinned_to_one_revision(client, small):
    r = root(small)
    (r / "clip.webm").write_bytes(b"a" * 100)
    rev = client.get("/api/files/work1/meta", params={"path": "clip.webm"}).json()["revision"]
    assert client.get("/api/files/work1/raw", params={"path": "clip.webm", "v": rev}, headers={"Range": "bytes=0-9"}).status_code == 206
    (r / "clip.webm").write_bytes(b"b" * 120)  # an agent replaces the video during playback
    stale = client.get("/api/files/work1/raw", params={"path": "clip.webm", "v": rev}, headers={"Range": "bytes=10-19"})
    assert stale.status_code == 412 and stale.json()["error"] == "revision_changed"

