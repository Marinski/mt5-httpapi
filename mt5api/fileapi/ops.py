"""File operations on a Tree: list, read, write, extract a zip, delete.

Every write lands atomically: the bytes go to a staging file beside the
target and are renamed over it, so a reader (the terminal, MetaEditor) never
sees half a file. An archive is unpacked into a staging directory first and
moved into place only after every entry passed the checks and the size caps.
"""
from __future__ import annotations

import hashlib
import io
import os
import secrets
import shutil
import stat
import zipfile
import zlib

from mt5api.fileapi.errors import (
    ArchiveTooLarge,
    BadArchive,
    Conflict,
    Locked,
    NotFound,
)
from mt5api.fileapi.tree import STAGING_PREFIX, Resolved, Tree
from mt5api.logger import log

ENTRY_FILE = "file"
ENTRY_DIR = "dir"

_CHUNK_BYTES = 1024 * 1024
_ZIP_FLAG_ENCRYPTED = 0x1
_ZIP_UNIX_MODE_SHIFT = 16


def _hasher():
    return hashlib.sha256()


def _sha256_file(path: str) -> str:
    digest = _hasher()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _staging_name() -> str:
    return f"{STAGING_PREFIX}{secrets.token_hex(6)}"


def _join(parent: str, child: str) -> str:
    return f"{parent}/{child}" if parent else child


def _entry(tree: Tree, path: Resolved) -> dict:
    info = os.stat(path.full)
    is_dir = stat.S_ISDIR(info.st_mode)
    return {
        "name": os.path.basename(path.full),
        "path": path.rel,
        "type": ENTRY_DIR if is_dir else ENTRY_FILE,
        "size": 0 if is_dir else info.st_size,
        "modified_at": int(info.st_mtime),
        "readable": not tree.is_hidden(path),
        "writable": not tree.is_hidden(path) and not tree.is_readonly(path),
    }


def kind(tree: Tree, raw: str) -> tuple[Resolved, str]:
    """Resolve `raw` and report whether it is a file or a directory.

    Hidden paths are refused before their existence is checked, so the
    answer never reveals whether a credentials file is there.
    """
    path = tree.resolve(raw)
    tree.check_readable(path)
    if os.path.isdir(path.full):
        return path, ENTRY_DIR
    if os.path.isfile(path.full):
        return path, ENTRY_FILE
    raise NotFound(f"{path.rel or '/'} does not exist")


def list_dir(tree: Tree, path: Resolved) -> dict:
    """One directory's entries, directories first, then by name."""
    entries = []
    for name in os.listdir(path.full):
        if name.startswith(STAGING_PREFIX):
            continue
        child = tree.resolve(_join(path.rel, name))
        entries.append(_entry(tree, child))
    entries.sort(key=lambda item: (item["type"] != ENTRY_DIR, item["name"].lower()))
    return {"tree": tree.name, "path": path.rel, "entries": entries}


def readable_file(tree: Tree, path: Resolved) -> str:
    """The on-disk path of a file the caller may download."""
    tree.check_readable(path)
    return path.full


def write_file(tree: Tree, raw: str, data: bytes) -> dict:
    """Create or replace one file, creating its parent directories."""
    path = tree.resolve(raw)
    tree.check_writable(path)
    if os.path.isdir(path.full):
        raise Conflict(f"{path.rel} is a directory")
    created = not os.path.exists(path.full)
    parent = os.path.dirname(path.full)
    _make_dirs(tree, parent)
    staged = os.path.join(parent, _staging_name())
    try:
        with open(staged, "wb") as handle:
            handle.write(data)
        _replace(staged, path)
    finally:
        _discard(staged)
    log.info("files: wrote %s:%s (%d bytes)", tree.name, path.rel, len(data))
    return {
        "tree": tree.name,
        "path": path.rel,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "created": created,
    }


def extract_zip(
    tree: Tree,
    raw: str,
    data: bytes,
    max_bytes: int,
    max_files: int,
) -> dict:
    """Unpack a zip into the directory `raw`, merging with what is there.

    Existing files the archive names are replaced; others are left alone.
    Nothing reaches the target until every entry has been checked and
    unpacked within the caps.
    """
    target = tree.resolve(raw)
    tree.check_writable(target)
    if os.path.isfile(target.full):
        raise Conflict(f"{target.rel} is a file; ?extract needs a directory")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as err:
        raise BadArchive("the body is not a zip archive") from err
    with archive:
        plan, dirs = _plan_extract(tree, target, archive.infolist(), max_files)
        declared = sum(info.file_size for info, _ in plan)
        if declared > max_bytes:
            raise ArchiveTooLarge(
                f"archive unpacks to {declared} bytes; "
                f"the cap is {max_bytes} (files.max_extract_bytes)"
            )
        _make_dirs(tree, target.full)
        staging = os.path.join(target.full, _staging_name())
        os.mkdir(staging)
        try:
            written = _unpack(archive, plan, staging, max_bytes)
            for directory in dirs:
                _make_dirs(tree, directory.full)
            for index, (_, path) in enumerate(plan):
                _make_dirs(tree, os.path.dirname(path.full))
                if os.path.isdir(path.full):
                    raise Conflict(f"{path.rel} is a directory in the target")
                _replace(os.path.join(staging, str(index)), path)
        finally:
            _discard_dir(staging)
    log.info(
        "files: extracted %d file(s) into %s:%s (%d bytes)",
        len(plan), tree.name, target.rel, sum(item["size"] for item in written),
    )
    return {
        "tree": tree.name,
        "path": target.rel,
        "count": len(written),
        "files": written,
    }


def delete(tree: Tree, raw: str, recursive: bool) -> dict:
    """Delete a file, or a directory (non-empty ones only with `recursive`)."""
    tree.check_writable(tree.resolve(raw))
    path, entry_kind = kind(tree, raw)
    if entry_kind == ENTRY_FILE:
        _remove(path, os.remove)
        log.info("files: deleted %s:%s", tree.name, path.rel)
        return {"tree": tree.name, "deleted": path.rel, "type": ENTRY_FILE}
    tree.check_deletable_dir(path)
    if os.listdir(path.full) and not recursive:
        raise Conflict(f"{path.rel} is not empty; pass recursive to delete it with its contents")
    _remove(path, shutil.rmtree)
    log.info("files: deleted directory %s:%s", tree.name, path.rel)
    return {"tree": tree.name, "deleted": path.rel, "type": ENTRY_DIR}


def _plan_extract(
    tree: Tree,
    target: Resolved,
    infos: list[zipfile.ZipInfo],
    max_files: int,
) -> tuple[list[tuple[zipfile.ZipInfo, Resolved]], list[Resolved]]:
    plan: list[tuple[zipfile.ZipInfo, Resolved]] = []
    dirs: list[Resolved] = []
    seen: set[str] = set()
    for info in infos:
        if info.flag_bits & _ZIP_FLAG_ENCRYPTED:
            raise BadArchive(f"{info.filename} is encrypted")
        if stat.S_ISLNK(info.external_attr >> _ZIP_UNIX_MODE_SHIFT):
            raise BadArchive(f"{info.filename} is a symlink")
        path = tree.resolve(_join(target.rel, info.filename.replace("\\", "/")))
        tree.check_writable(path)
        if info.is_dir():
            dirs.append(path)
            continue
        if path.key in seen:
            raise BadArchive(f"{info.filename} appears twice in the archive")
        seen.add(path.key)
        plan.append((info, path))
        if len(plan) > max_files:
            raise ArchiveTooLarge(
                f"archive holds more than {max_files} files (files.max_extract_files)"
            )
    return plan, dirs


def _unpack(
    archive: zipfile.ZipFile,
    plan: list[tuple[zipfile.ZipInfo, Resolved]],
    staging: str,
    max_bytes: int,
) -> list[dict]:
    """Stream every planned entry into the staging directory.

    Counts the bytes actually inflated rather than trusting the sizes the
    archive declares, which a crafted zip can understate.
    """
    total = 0
    written = []
    for index, (info, path) in enumerate(plan):
        digest = _hasher()
        size = 0
        staged = os.path.join(staging, str(index))
        try:
            with archive.open(info) as source, open(staged, "wb") as sink:
                for chunk in iter(lambda: source.read(_CHUNK_BYTES), b""):
                    size += len(chunk)
                    total += len(chunk)
                    if total > max_bytes:
                        raise ArchiveTooLarge(
                            f"archive unpacks to more than {max_bytes} bytes "
                            "(files.max_extract_bytes)"
                        )
                    digest.update(chunk)
                    sink.write(chunk)
        except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError) as err:
            raise BadArchive(f"{info.filename} cannot be unpacked: {err}") from err
        written.append({"path": path.rel, "size": size, "sha256": digest.hexdigest()})
    return written


def _make_dirs(tree: Tree, directory: str) -> None:
    try:
        os.makedirs(directory, exist_ok=True)
    except (FileExistsError, NotADirectoryError) as err:
        rel = os.path.relpath(directory, tree.root).replace(os.sep, "/")
        raise Conflict(f"a file sits where {rel} needs a directory") from err


def _replace(staged: str, path: Resolved) -> None:
    try:
        os.replace(staged, path.full)
    except PermissionError as err:
        raise Locked(f"{path.rel} is in use and cannot be replaced: {err}") from err


def _remove(path: Resolved, remover) -> None:
    try:
        remover(path.full)
    except PermissionError as err:
        raise Locked(f"{path.rel} is in use and cannot be deleted: {err}") from err


def _discard(staged: str) -> None:
    if not os.path.exists(staged):
        return
    try:
        os.remove(staged)
    except OSError as err:
        log.warning("files: could not remove staging file %s: %s", staged, err)


def _discard_dir(staging: str) -> None:
    try:
        shutil.rmtree(staging)
    except OSError as err:
        log.warning("files: could not remove staging directory %s: %s", staging, err)
