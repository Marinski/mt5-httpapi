"""The file API's REST routes on the running stack, through nginx and the
API's own server, against the terminal's real (Windows) file system.

Everything written lives under MQL5/Files/livetest-* and is purged before and
after the session (see conftest). Needs files enabled on the target terminal
and, for the writes, a demo account unless MT5_LIVE_ALLOW_REAL=1.
"""
from __future__ import annotations

import hashlib
import io
import os
import uuid
import zipfile

import pytest
import requests

from tests.live.conftest import ARTIFACT_PREFIX
from tests.live.live_client import RestClient

_HTTP_OK = 200
_HTTP_CREATED = 201
_HTTP_BAD_REQUEST = 400
_HTTP_FORBIDDEN = 403
_HTTP_NOT_FOUND = 404
_HTTP_CONFLICT = 409
_HTTP_TOO_LARGE = 413
_UPLOAD_TIMEOUT_SECONDS = 300
_MIB = 1024 * 1024
# Under nginx's client_max_body_size and the API's 25 MiB upload cap.
_BIG_UPLOAD_BYTES = 20 * _MIB
_OVERSIZE_UPLOAD_BYTES = 26 * _MIB
_LS_FIELDS = {
    "name", "path", "type", "size", "size_human", "mode", "attributes", "nlink",
    "modified_at", "modified", "created_at", "created", "accessed_at", "accessed",
    "is_symlink", "readable", "writable",
}


def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _url(rest: RestClient, path: str, query: str = "") -> str:
    """The root listing is /files; Flask does not serve /files/."""
    suffix = f"/{path}" if path else ""
    return f"{rest.base}/files{suffix}{query}"


def _put(rest: RestClient, path: str, data: bytes, query: str = "", **kwargs) -> requests.Response:
    return rest.session.put(
        _url(rest, path, query), data=data, timeout=_UPLOAD_TIMEOUT_SECONDS, **kwargs
    )


def _get(rest: RestClient, path: str) -> requests.Response:
    return rest.session.get(_url(rest, path), timeout=_UPLOAD_TIMEOUT_SECONDS)


def _delete(rest: RestClient, path: str, query: str = "") -> requests.Response:
    return rest.session.delete(_url(rest, path, query), timeout=_UPLOAD_TIMEOUT_SECONDS)


@pytest.fixture
def scratch(files_api) -> str:
    """A fresh MQL5/Files/livetest-* directory name for one test."""
    return f"MQL5/Files/{ARTIFACT_PREFIX}{uuid.uuid4().hex[:8]}"


def test_a_raw_binary_upload_round_trips_byte_for_byte(rest: RestClient, scratch: str):
    data = os.urandom(256 * 1024)

    created = _put(rest, f"{scratch}/blob.bin", data)
    downloaded = _get(rest, f"{scratch}/blob.bin")

    assert created.status_code == _HTTP_CREATED, created.text
    assert created.json()["sha256"] == hashlib.sha256(data).hexdigest()
    assert created.json()["created"] is True
    assert downloaded.status_code == _HTTP_OK
    assert downloaded.headers["Content-Type"].startswith("application/octet-stream")
    assert downloaded.content == data


def test_replacing_a_file_answers_200(rest: RestClient, scratch: str):
    _put(rest, f"{scratch}/state.json", b'{"v":1}')

    replaced = _put(rest, f"{scratch}/state.json", b'{"v":2}')

    assert replaced.status_code == _HTTP_OK
    assert replaced.json()["created"] is False
    assert _get(rest, f"{scratch}/state.json").content == b'{"v":2}'


def test_a_body_labelled_as_a_form_is_stored_verbatim(rest: RestClient, scratch: str):
    """What `curl --data-binary` sends: a raw body labelled urlencoded."""
    data = b"a=1&b=2&c=%20\x00\xff"

    created = _put(
        rest, f"{scratch}/form.bin", data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert created.status_code == _HTTP_CREATED
    assert _get(rest, f"{scratch}/form.bin").content == data


def test_a_multipart_upload_stores_the_file_field(rest: RestClient, scratch: str):
    data = os.urandom(4096)

    created = rest.session.put(
        _url(rest, f"{scratch}/multi.bin"),
        files={"file": ("ignored-name.bin", data)},
        timeout=_UPLOAD_TIMEOUT_SECONDS,
    )

    assert created.status_code == _HTTP_CREATED, created.text
    assert _get(rest, f"{scratch}/multi.bin").content == data


def test_a_20_mib_upload_passes_nginx_and_round_trips(rest: RestClient, scratch: str):
    data = os.urandom(_BIG_UPLOAD_BYTES)

    created = _put(rest, f"{scratch}/big.bin", data)
    downloaded = _get(rest, f"{scratch}/big.bin")

    assert created.status_code == _HTTP_CREATED, created.text[:300]
    assert created.json()["size"] == _BIG_UPLOAD_BYTES
    assert hashlib.sha256(downloaded.content).hexdigest() == hashlib.sha256(data).hexdigest()


def test_an_upload_over_the_cap_is_refused(rest: RestClient, scratch: str):
    refused = _put(rest, f"{scratch}/too-big.bin", b"\0" * _OVERSIZE_UPLOAD_BYTES)

    assert refused.status_code == _HTTP_TOO_LARGE
    assert _get(rest, f"{scratch}/too-big.bin").status_code == _HTTP_NOT_FOUND


def test_windows_matches_paths_case_insensitively(rest: RestClient, scratch: str):
    _put(rest, f"{scratch}/Case.txt", b"one file")

    assert _get(rest, f"{scratch}/CASE.TXT").content == b"one file"


def test_a_zip_unpacks_into_the_terminal_tree_and_merges(rest: RestClient, scratch: str):
    _put(rest, f"{scratch}/keep.txt", b"keep")
    archive = _zip({
        "Core/Base.mqh": b"base",
        "Core/Util/Math.mqh": b"math",
        "keep.txt": b"replaced",
    })

    unpacked = _put(rest, scratch, archive, "?extract")

    assert unpacked.status_code == _HTTP_CREATED, unpacked.text
    assert unpacked.json()["count"] == 3
    assert _get(rest, f"{scratch}/Core/Util/Math.mqh").content == b"math"
    assert _get(rest, f"{scratch}/keep.txt").content == b"replaced"
    names = {entry["name"] for entry in _get(rest, scratch).json()["entries"]}
    assert names == {"Core", "keep.txt"}, "a staging directory or the zip was left behind"


@pytest.mark.parametrize(
    ("entries", "code"),
    [
        ({"ok.mqh": b"x", "../escape.mqh": b"x"}, "BAD_PATH"),
        ({"ok.mqh": b"x", "C:/escape.mqh": b"x"}, "BAD_PATH"),
        ({"ok.mqh": b"x", "OK.MQH": b"y"}, "BAD_ARCHIVE"),
    ],
)
def test_a_bad_archive_writes_nothing(rest: RestClient, scratch: str, entries, code):
    refused = _put(rest, f"{scratch}/lib", _zip(entries), "?extract")

    assert refused.status_code == _HTTP_BAD_REQUEST
    assert refused.json()["code"] == code
    assert _get(rest, f"{scratch}/lib").status_code == _HTTP_NOT_FOUND
    assert _get(rest, f"{scratch}/escape.mqh").status_code == _HTTP_NOT_FOUND


def test_a_body_that_is_not_a_zip_is_refused(rest: RestClient, scratch: str):
    refused = _put(rest, f"{scratch}/lib", b"plainly not a zip", "?extract")

    assert refused.status_code == _HTTP_BAD_REQUEST
    assert refused.json()["code"] == "BAD_ARCHIVE"


@pytest.mark.parametrize(
    "path",
    [
        "terminal64.exe",
        "TERMINAL64.EXE",
        "mt5start.ini",
        "Config/accounts.dat",
        "MQL5/Experts/Uploaded/livetest-evil.ex5",
        "MQL5/Files/chartctl/livetest-evil.json",
    ],
)
def test_protected_paths_are_not_written(rest: RestClient, files_api, path):
    refused = _put(rest, path, b"evil")

    assert refused.status_code == _HTTP_FORBIDDEN, refused.text
    assert refused.json()["code"] == "PROTECTED"


def test_the_credentials_are_hidden_in_any_case(rest: RestClient, files_enabled_target):
    for path in ("MT5START.INI", "config/ACCOUNTS.DAT"):
        refused = _get(rest, path)
        assert refused.status_code == _HTTP_FORBIDDEN, path
        assert "Password" not in refused.text


@pytest.mark.parametrize(
    "name",
    ["CON.txt", "nul", "COM1.log", "trailing.", "a%3Ab.txt"],
)
def test_names_windows_cannot_hold_are_refused(rest: RestClient, scratch: str, name: str):
    refused = _put(rest, f"{scratch}/{name}", b"x")

    assert refused.status_code == _HTTP_BAD_REQUEST, refused.text
    assert refused.json()["code"] == "BAD_PATH"


def test_a_non_empty_directory_needs_recursive(rest: RestClient, scratch: str):
    _put(rest, f"{scratch}/a.txt", b"a")

    refused = _delete(rest, scratch)
    deleted = _delete(rest, scratch, "?recursive")

    assert refused.status_code == _HTTP_CONFLICT
    assert deleted.status_code == _HTTP_OK
    assert _get(rest, scratch).status_code == _HTTP_NOT_FOUND


def test_a_listing_carries_ls_style_metadata(rest: RestClient, scratch: str):
    data = b"x" * 5000
    _put(rest, f"{scratch}/five-k.bin", data)

    listing = _get(rest, scratch).json()

    assert listing["count"] == 1
    assert listing["total_size"] == len(data)
    (entry,) = listing["entries"]
    assert _LS_FIELDS <= set(entry), _LS_FIELDS - set(entry)
    assert entry["size"] == len(data)
    assert entry["size_human"] == "4.9K"
    assert entry["mode"].startswith("-")
    assert entry["modified"].endswith("Z")
    assert entry["is_symlink"] is False
    assert entry["writable"] is True


def test_the_root_lists_the_install_directory(rest: RestClient, files_enabled_target):
    entries = {entry["name"].lower(): entry for entry in _get(rest, "").json()["entries"]}

    assert entries["mql5"]["mode"].startswith("d")
    assert entries["terminal64.exe"]["writable"] is False
    assert entries["terminal64.exe"]["size"] > _MIB
    assert entries["mt5start.ini"]["readable"] is False


def test_the_compile_tree_lists(rest: RestClient, files_enabled_target):
    listing = rest.session.get(f"{rest.base}/compile/files/Include", timeout=60).json()

    names = {entry["name"].lower() for entry in listing["entries"]}
    assert "trade" in names, "the compile tree has no MQL5 standard library"
