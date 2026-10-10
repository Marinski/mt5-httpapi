"""File API (/files, /compile/files) through Flask's test client.

The routes are registered on a fresh app with the terminal and compile trees
repointed at tmp dirs, so the suite does not depend on FILES_ENABLED at
import. The terminal tree is laid out like a real install: terminal64.exe,
mt5start.ini with credentials, Config/accounts.dat, and Chart Deployments'
own directories.
"""
from __future__ import annotations

import hashlib
import io
import os
import stat
import zipfile

import pytest

from mt5api import config
from mt5api.fileapi import ops, terminal_tree
from mt5api.fileapi.errors import ArchiveTooLarge, InvalidPath
from tests.conftest import FILE_TREE_SECRET as SECRET


@pytest.fixture
def trees(file_trees):
    return file_trees


@pytest.fixture
def client(files_app):
    return files_app.test_client()


def _zip(entries: dict[str, bytes], symlinks: dict[str, str] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
        for name, target in (symlinks or {}).items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, target)
    return buffer.getvalue()


def _staging_left(root) -> list[str]:
    return [
        os.path.join(dirpath, name)
        for dirpath, dirnames, filenames in os.walk(root)
        for name in dirnames + filenames
        if name.startswith(".mt5api-")
    ]


# ── listing and reading ──────────────────────────────────────────────

def test_the_root_lists_with_access_flags(client):
    r = client.get("/files")

    assert r.status_code == 200
    body = r.get_json()
    assert body["tree"] == "terminal"
    assert body["path"] == ""
    entries = {entry["name"]: entry for entry in body["entries"]}
    assert entries["MQL5"]["type"] == "dir"
    assert entries["terminal64.exe"] == {
        **entries["terminal64.exe"],
        "type": "file",
        "size": 2,
        "readable": True,
        "writable": False,
    }
    assert entries["mt5start.ini"]["readable"] is False
    assert entries["mt5start.ini"]["writable"] is False
    names = [entry["name"] for entry in body["entries"]]
    assert names.index("MQL5") < names.index("terminal64.exe"), "directories first"


def test_entries_carry_ls_style_metadata(client, trees):
    target = trees["terminal"] / "logs" / "20261009.log"
    os.utime(target, (1_700_000_000, 1_700_000_100))
    os.chmod(target, 0o444)

    body = client.get("/files/logs").get_json()

    assert body["count"] == 1
    assert body["total_size"] == target.stat().st_size
    (entry,) = body["entries"]
    assert entry["mode"] == "-r--r--r--"
    assert entry["attributes"] == ["readonly"]
    assert entry["nlink"] == 1
    assert entry["modified_at"] == 1_700_000_100
    assert entry["modified"] == "2023-11-14T22:15:00Z"
    assert entry["accessed_at"] == 1_700_000_000
    assert entry["accessed"] == "2023-11-14T22:13:20Z"
    assert isinstance(entry["created_at"], int)
    assert entry["created"].endswith("Z")
    assert entry["is_symlink"] is False
    assert "link_target" not in entry
    assert entry["size_human"] == str(entry["size"])


@pytest.mark.parametrize(
    ("size", "human"),
    [(0, "0"), (1023, "1023"), (1024, "1.0K"), (4300, "4.2K"), (1_363_149, "1.3M"),
     (3 * 1024**3, "3.0G")],
)
def test_sizes_read_like_ls_h(size, human):
    assert ops._human_size(size) == human


def test_a_directory_entry_reads_as_one(client):
    entries = {e["name"]: e for e in client.get("/files").get_json()["entries"]}

    assert entries["MQL5"]["mode"].startswith("d")
    assert entries["MQL5"]["size"] == 0
    assert entries["MQL5"]["size_human"] == "0"


def test_a_symlink_out_of_the_tree_is_listed_but_not_reachable(client, trees, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, trees["terminal"] / "MQL5" / "Files" / "link")

    r = client.get("/files/MQL5/Files")

    assert r.status_code == 200
    link = next(e for e in r.get_json()["entries"] if e["name"] == "link")
    assert link["is_symlink"] is True
    assert link["link_target"] == str(outside)
    assert link["link_outside_tree"] is True
    assert link["readable"] is False
    assert link["writable"] is False


def test_a_symlink_inside_the_tree_is_listed_as_reachable(client, trees):
    os.symlink(trees["terminal"] / "logs", trees["terminal"] / "MQL5" / "Files" / "logs-link")

    entries = client.get("/files/MQL5/Files").get_json()["entries"]

    link = next(e for e in entries if e["name"] == "logs-link")
    assert link["type"] == "dir"
    assert link["link_outside_tree"] is False
    assert link["readable"] is True


def test_a_subdirectory_lists_relative_paths(client):
    r = client.get("/files/MQL5/Files")

    assert r.status_code == 200
    (entry,) = r.get_json()["entries"]
    assert entry["path"] == "MQL5/Files/chartctl"
    assert entry["writable"] is False


def test_a_file_downloads_as_bytes(client, trees):
    r = client.get("/files/logs/20261009.log")

    assert r.status_code == 200
    assert r.mimetype == "application/octet-stream"
    assert r.data == (trees["terminal"] / "logs" / "20261009.log").read_bytes()


def _refuse(*_args, **_kwargs):
    raise PermissionError(13, "The process cannot access the file")


@pytest.mark.parametrize(
    ("method", "kwargs", "refused_call"),
    [
        ("get", {}, "open"),
        ("put", {"data": b"x"}, "os.replace"),
        ("delete", {}, "os.remove"),
    ],
)
def test_a_file_held_open_by_another_process_is_reported_locked(
    client, trees, monkeypatch, method, kwargs, refused_call
):
    """On Windows a file an expert opened without sharing refuses reads,
    replaces and deletes; the API answers 409 FILE_LOCKED for each, not a
    500 halfway through the response. Simulated with the PermissionError
    Windows raises, on exactly the call Windows refuses."""
    target = trees["terminal"] / "MQL5" / "Files" / "held.bin"
    target.write_bytes(b"held")
    if refused_call == "open":
        monkeypatch.setattr(ops, "open", _refuse, raising=False)
    else:
        monkeypatch.setattr(ops.os, refused_call.split(".")[1], _refuse)

    r = getattr(client, method)("/files/MQL5/Files/held.bin", **kwargs)

    assert r.status_code == 409, r.data
    assert r.get_json()["code"] == "FILE_LOCKED"
    assert target.read_bytes() == b"held"


def test_a_file_deleted_between_the_check_and_the_open_is_404(client, trees, monkeypatch):
    """Windows keeps a just-deleted entry visible while its delete is pending,
    so the existence check passes and the open finds nothing."""
    target = trees["terminal"] / "MQL5" / "Files" / "gone.bin"
    target.write_bytes(b"gone")

    def _vanished(*_args, **_kwargs):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(ops, "open", _vanished, raising=False)

    r = client.get("/files/MQL5/Files/gone.bin")

    assert r.status_code == 404, r.data
    assert r.get_json()["code"] == "NOT_FOUND"


def test_a_missing_path_is_404(client):
    r = client.get("/files/MQL5/nope.mqh")

    assert r.status_code == 404
    assert r.get_json()["code"] == "NOT_FOUND"


@pytest.mark.parametrize(
    "path",
    ["mt5start.ini", "MT5START.INI", "Config/accounts.dat", "config/Accounts.DAT"],
)
def test_credentials_are_never_read(client, path):
    r = client.get(f"/files/{path}")

    assert r.status_code == 403
    assert r.get_json()["code"] == "PROTECTED"
    assert b"hunter2" not in r.data


def test_other_config_files_are_readable(client):
    assert client.get("/files/Config/common.ini").data == b"[Experts]\n"


# ── writing ──────────────────────────────────────────────────────────

def test_put_creates_a_file_and_its_directories(client, trees):
    data = b"#define LIB 1\n"

    r = client.put("/files/MQL5/Include/MyLib/Core/Base.mqh", data=data)

    assert r.status_code == 201
    assert r.get_json() == {
        "tree": "terminal",
        "path": "MQL5/Include/MyLib/Core/Base.mqh",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "created": True,
    }
    target = trees["terminal"] / "MQL5" / "Include" / "MyLib" / "Core" / "Base.mqh"
    assert target.read_bytes() == data
    assert _staging_left(trees["terminal"]) == []


def test_put_replaces_an_existing_file(client, trees):
    client.put("/files/MQL5/Files/state.json", data=b"old")

    r = client.put("/files/MQL5/Files/state.json", data=b"new")

    assert r.status_code == 200
    assert r.get_json()["created"] is False
    assert (trees["terminal"] / "MQL5" / "Files" / "state.json").read_bytes() == b"new"


def test_a_raw_body_labelled_as_a_form_is_stored_verbatim(client, trees):
    """curl --data-binary labels its body application/x-www-form-urlencoded."""
    data = b"a=1&b=2\x00\xff"

    r = client.put(
        "/files/MQL5/Files/raw.bin",
        data=data,
        content_type="application/x-www-form-urlencoded",
    )

    assert r.status_code == 201
    assert (trees["terminal"] / "MQL5" / "Files" / "raw.bin").read_bytes() == data


def test_a_multipart_upload_stores_the_file_field(client, trees):
    r = client.put(
        "/files/MQL5/Libraries/helper.dll",
        data={"file": (io.BytesIO(b"MZdll"), "whatever.dll")},
        content_type="multipart/form-data",
    )

    assert r.status_code == 201
    assert (trees["terminal"] / "MQL5" / "Libraries" / "helper.dll").read_bytes() == b"MZdll"


def test_a_multipart_upload_without_the_file_field_is_refused(client):
    r = client.put(
        "/files/MQL5/Files/x.txt",
        data={"other": (io.BytesIO(b"x"), "x.txt")},
        content_type="multipart/form-data",
    )

    assert r.status_code == 400
    assert r.get_json()["code"] == "BAD_REQUEST"


def test_an_upload_over_the_cap_is_refused(client, trees, monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_BODY_BYTES", 8)

    r = client.put("/files/MQL5/Files/big.bin", data=b"x" * 9)

    assert r.status_code == 413
    assert r.get_json()["code"] == "TOO_LARGE"
    assert not (trees["terminal"] / "MQL5" / "Files" / "big.bin").exists()


def test_put_onto_a_directory_is_a_conflict(client):
    r = client.put("/files/MQL5/Include", data=b"x")

    assert r.status_code == 409
    assert r.get_json()["code"] == "CONFLICT"


def test_put_under_a_file_is_a_conflict(client):
    r = client.put("/files/logs/20261009.log/child.txt", data=b"x")

    assert r.status_code == 409


@pytest.mark.parametrize(
    "path",
    [
        "mt5start.ini",
        "Config/accounts.dat",
        "terminal64.exe",
        "TERMINAL64.EXE",
        "MetaEditor64.exe",
        "MQL5/Experts/Uploaded/EA.ex5",
        "MQL5/Files/chartctl/desired.json",
        "chartctl/registry.json",
    ],
)
def test_protected_paths_are_not_written(client, trees, path):
    before = sorted(os.listdir(trees["terminal"]))

    r = client.put(f"/files/{path}", data=b"evil")

    assert r.status_code == 403
    assert r.get_json()["code"] == "PROTECTED"
    assert sorted(os.listdir(trees["terminal"])) == before
    assert (trees["terminal"] / "mt5start.ini").read_bytes() == SECRET
    assert (trees["terminal"] / "terminal64.exe").read_bytes() == b"MZ"


@pytest.mark.parametrize(
    "path",
    [
        "../escape.txt",
        "MQL5/../../escape.txt",
        "MQL5/..\\..\\escape.txt",
        "%2e%2e/escape.txt",
        "C:/Windows/win.ini",
        "MQL5/Files/stream.txt:ads",
        "MQL5/Files/con.txt",
        "MQL5/NUL",
        "MQL5/Files/trailing.",
        "MQL5/Files/trailing ",
        "MQL5/Files/a%09b",
        "MQL5/Files/.mt5api-abc",
    ],
)
def test_paths_that_could_leave_the_tree_are_refused(client, tmp_path, path):
    r = client.put(f"/files/{path}", data=b"x")

    assert r.status_code == 400, r.data
    assert r.get_json()["code"] == "BAD_PATH"
    assert not (tmp_path / "escape.txt").exists()


def test_a_symlink_out_of_the_tree_is_refused(client, trees, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"outside")
    os.symlink(outside, trees["terminal"] / "MQL5" / "Files" / "link")

    read = client.get("/files/MQL5/Files/link/secret.txt")
    write = client.put("/files/MQL5/Files/link/new.txt", data=b"x")

    assert read.status_code == 400
    assert write.status_code == 400
    assert not (outside / "new.txt").exists()


def test_paths_are_checked_before_any_write():
    tree = terminal_tree("/nonexistent-root")
    with pytest.raises(InvalidPath):
        tree.resolve("/etc/passwd")
    with pytest.raises(InvalidPath):
        tree.resolve("a//b")


# ── deleting ─────────────────────────────────────────────────────────

def test_delete_removes_a_file(client, trees):
    client.put("/files/MQL5/Files/gone.txt", data=b"x")

    r = client.delete("/files/MQL5/Files/gone.txt")

    assert r.status_code == 200
    assert r.get_json() == {"tree": "terminal", "deleted": "MQL5/Files/gone.txt", "type": "file"}
    assert not (trees["terminal"] / "MQL5" / "Files" / "gone.txt").exists()


def test_a_non_empty_directory_needs_recursive(client, trees):
    client.put("/files/MQL5/Include/MyLib/a.mqh", data=b"x")

    refused = client.delete("/files/MQL5/Include/MyLib")
    removed = client.delete("/files/MQL5/Include/MyLib?recursive")

    assert refused.status_code == 409
    assert removed.status_code == 200
    assert removed.get_json()["type"] == "dir"
    assert not (trees["terminal"] / "MQL5" / "Include" / "MyLib").exists()


def test_recursive_false_is_not_recursive(client):
    client.put("/files/MQL5/Include/MyLib/a.mqh", data=b"x")

    assert client.delete("/files/MQL5/Include/MyLib?recursive=0").status_code == 409


@pytest.mark.parametrize(
    "path",
    ["MQL5?recursive", "Config?recursive", "MQL5/Experts?recursive", "mt5start.ini"],
)
def test_a_directory_holding_protected_paths_is_not_deleted(client, trees, path):
    r = client.delete(f"/files/{path}")

    assert r.status_code == 403
    assert (trees["terminal"] / "mt5start.ini").exists()
    assert (trees["terminal"] / "MQL5" / "Experts" / "Uploaded").is_dir()


def test_deleting_a_missing_path_is_404(client):
    assert client.delete("/files/MQL5/nothing.txt").status_code == 404


# ── ?extract ─────────────────────────────────────────────────────────

def test_extract_unpacks_a_tree_and_stores_no_zip(client, trees):
    archive = _zip({
        "Core/Base.mqh": b"base",
        "Core/Util/Math.mqh": b"math",
        "Signals.mqh": b"signals",
        "Empty/": b"",
    })

    r = client.put("/files/MQL5/Include/MyLib?extract", data=archive)

    assert r.status_code == 201
    body = r.get_json()
    assert body["path"] == "MQL5/Include/MyLib"
    assert body["count"] == 3
    assert {item["path"]: item["sha256"] for item in body["files"]} == {
        "MQL5/Include/MyLib/Core/Base.mqh": hashlib.sha256(b"base").hexdigest(),
        "MQL5/Include/MyLib/Core/Util/Math.mqh": hashlib.sha256(b"math").hexdigest(),
        "MQL5/Include/MyLib/Signals.mqh": hashlib.sha256(b"signals").hexdigest(),
    }
    lib = trees["terminal"] / "MQL5" / "Include" / "MyLib"
    assert (lib / "Core" / "Util" / "Math.mqh").read_bytes() == b"math"
    assert (lib / "Empty").is_dir()
    assert sorted(p.name for p in lib.iterdir()) == ["Core", "Empty", "Signals.mqh"]
    assert _staging_left(trees["terminal"]) == []


def test_extract_merges_into_an_existing_directory(client, trees):
    client.put("/files/MQL5/Include/MyLib/Keep.mqh", data=b"keep")
    client.put("/files/MQL5/Include/MyLib/Signals.mqh", data=b"old")

    r = client.put("/files/MQL5/Include/MyLib?extract", data=_zip({"Signals.mqh": b"new"}))

    assert r.status_code == 201
    lib = trees["terminal"] / "MQL5" / "Include" / "MyLib"
    assert (lib / "Keep.mqh").read_bytes() == b"keep"
    assert (lib / "Signals.mqh").read_bytes() == b"new"


def test_extract_with_a_false_value_stores_the_bytes_as_a_file(client, trees):
    archive = _zip({"a.mqh": b"a"})

    r = client.put("/files/MQL5/Files/lib.zip?extract=false", data=archive)

    assert r.status_code == 201
    assert (trees["terminal"] / "MQL5" / "Files" / "lib.zip").read_bytes() == archive


@pytest.mark.parametrize(
    ("entries", "symlinks", "code"),
    [
        ({"../evil.mqh": b"x"}, None, "BAD_PATH"),
        ({"ok.mqh": b"x", "a/../../evil.mqh": b"x"}, None, "BAD_PATH"),
        ({"/abs/evil.mqh": b"x"}, None, "BAD_PATH"),
        ({"C:/evil.mqh": b"x"}, None, "BAD_PATH"),
        ({"ok.mqh": b"x"}, {"link": "/etc/passwd"}, "BAD_ARCHIVE"),
        ({"a.mqh": b"x", "A.MQH": b"y"}, None, "BAD_ARCHIVE"),
    ],
)
def test_a_bad_archive_writes_nothing(client, trees, tmp_path, entries, symlinks, code):
    r = client.put("/files/MQL5/Include/MyLib?extract", data=_zip(entries, symlinks))

    assert r.status_code == 400
    assert r.get_json()["code"] == code
    assert not (trees["terminal"] / "MQL5" / "Include" / "MyLib").exists()
    assert not (tmp_path / "evil.mqh").exists()


def test_an_archive_reaching_a_protected_path_writes_nothing(client, trees):
    archive = _zip({"notes.txt": b"x", "chartctl/desired.json": b"evil"})

    r = client.put("/files/MQL5/Files?extract", data=archive)

    assert r.status_code == 403
    assert not (trees["terminal"] / "MQL5" / "Files" / "notes.txt").exists()
    desired = trees["terminal"] / "MQL5" / "Files" / "chartctl" / "desired.json"
    assert desired.read_bytes() == b"{}"


def test_a_body_that_is_not_a_zip_is_refused(client):
    r = client.put("/files/MQL5/Include/MyLib?extract", data=b"not a zip")

    assert r.status_code == 400
    assert r.get_json()["code"] == "BAD_ARCHIVE"


def test_too_many_files_is_refused(client, trees, monkeypatch):
    monkeypatch.setattr(config, "FILES_MAX_EXTRACT_FILES", 2)

    r = client.put(
        "/files/MQL5/Include/MyLib?extract",
        data=_zip({"a": b"1", "b": b"2", "c": b"3"}),
    )

    assert r.status_code == 413
    assert r.get_json()["code"] == "ARCHIVE_TOO_LARGE"
    assert not (trees["terminal"] / "MQL5" / "Include" / "MyLib").exists()


def test_too_many_bytes_is_refused(client, trees, monkeypatch):
    monkeypatch.setattr(config, "FILES_MAX_EXTRACT_BYTES", 10)

    r = client.put("/files/MQL5/Include/MyLib?extract", data=_zip({"a": b"x" * 11}))

    assert r.status_code == 413
    assert not (trees["terminal"] / "MQL5" / "Include" / "MyLib").exists()


def test_the_inflated_size_is_counted_not_the_declared_one(trees, tmp_path):
    """A crafted zip can declare small sizes; the unpack loop counts what it
    actually inflates."""
    archive = zipfile.ZipFile(io.BytesIO(_zip({"a": b"x" * 64})))
    tree = terminal_tree(str(trees["terminal"]))
    plan = [(archive.infolist()[0], tree.resolve("MQL5/a"))]
    staging = tmp_path / "staging"
    staging.mkdir()

    with pytest.raises(ArchiveTooLarge):
        ops._unpack(archive, plan, str(staging), max_bytes=10)


def test_extract_onto_a_file_is_a_conflict(client):
    r = client.put("/files/logs/20261009.log?extract", data=_zip({"a": b"1"}))

    assert r.status_code == 409


# ── the compile tree ─────────────────────────────────────────────────

def test_compile_files_write_the_compile_tree_and_mark_it_changed(client, trees):
    r = client.put(
        "/compile/files/Include/MyLib?extract",
        data=_zip({"Base.mqh": b"base"}),
    )

    assert r.status_code == 201
    assert r.get_json()["tree"] == "compile"
    assert (trees["compile"] / "Include" / "MyLib" / "Base.mqh").read_bytes() == b"base"
    assert not (trees["terminal"] / "Include").exists()
    assert trees["changed"] == [True]

    client.delete("/compile/files/Include/MyLib?recursive")
    assert trees["changed"] == [True, True]


def test_a_refused_compile_write_does_not_mark_it_changed(client, trees):
    r = client.put("/compile/files/Experts/Uploaded/EA.ex5", data=b"x")

    assert r.status_code == 403
    assert trees["changed"] == []


def test_the_compile_tree_lists(client):
    r = client.get("/compile/files")

    assert r.status_code == 200
    assert {entry["name"] for entry in r.get_json()["entries"]} == {"Include", "Experts"}


def test_terminal_writes_do_not_mark_the_compile_tree(client, trees):
    client.put("/files/MQL5/Include/a.mqh", data=b"a")

    assert trees["changed"] == []


# ── wiring in the real app ──────────────────────────────────────────

def test_the_routes_are_absent_unless_enabled(api_client):
    """mt5api.server registers the file API only when FILES_ENABLED, which
    the test config leaves off."""
    assert config.FILES_ENABLED is False
    assert api_client.get("/files").status_code == 404
    assert api_client.put("/files/MQL5/x.txt", data=b"x").status_code == 404


def test_a_file_upload_gets_the_upload_body_cap(api_client):
    """A raw PUT to the file API carries a file, not JSON, so the generic
    4 MiB JSON cap must not refuse it before the handler sees it."""
    body = b"x" * (config.MAX_REQUEST_BODY_BYTES + 1)

    upload = api_client.put("/files/MQL5/Files/big.bin", data=body)
    json_route = api_client.put("/orders/1", data=body, content_type="application/json")

    assert upload.status_code != 413
    assert json_route.status_code == 413
