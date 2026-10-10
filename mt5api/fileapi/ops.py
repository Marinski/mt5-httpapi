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
from datetime import datetime, timezone

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

_SIZE_STEP = 1024
_SIZE_UNITS = ("", "K", "M", "G", "T")

ATTRIBUTE_READONLY = "readonly"
ATTRIBUTE_HIDDEN = "hidden"
# Windows FILE_ATTRIBUTE_* bits (os.stat_result.st_file_attributes).
_WINDOWS_ATTRIBUTES = (
    (0x1, ATTRIBUTE_READONLY),
    (0x2, ATTRIBUTE_HIDDEN),
    (0x4, "system"),
    (0x20, "archive"),
    (0x100, "temporary"),
    (0x200, "sparse"),
    (0x400, "reparse_point"),
    (0x800, "compressed"),
    (0x1000, "offline"),
    (0x2000, "not_content_indexed"),
    (0x4000, "encrypted"),
)


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


def _iso(timestamp: float) -> str:
    moment = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _human_size(size: int) -> str:
    """`ls -h` style: 812, 4.2K, 1.3M, 2.0G."""
    value = float(size)
    for unit in _SIZE_UNITS:
        if value < _SIZE_STEP or unit == _SIZE_UNITS[-1]:
            return f"{int(value)}" if unit == "" else f"{value:.1f}{unit}"
        value /= _SIZE_STEP
    return f"{size}"


def _attributes(name: str, info: os.stat_result) -> list[str]:
    """Windows file attributes by name (readonly, hidden, system, ...).

    Off Windows there are none to read, so a dot-name counts as hidden and a
    file without a write bit as readonly, the closest equivalents.
    """
    flags = getattr(info, "st_file_attributes", None)
    if flags is None:
        derived = []
        if not info.st_mode & stat.S_IWUSR:
            derived.append(ATTRIBUTE_READONLY)
        if name.startswith("."):
            derived.append(ATTRIBUTE_HIDDEN)
        return derived
    return [label for flag, label in _WINDOWS_ATTRIBUTES if flags & flag]


def _created(info: os.stat_result) -> float:
    """Creation time: st_birthtime where the platform has it, else st_ctime,
    which is the creation time on Windows."""
    return getattr(info, "st_birthtime", info.st_ctime)


def _entry(tree: Tree, path: Resolved) -> dict:
    """One `ls -al` style entry. Describes a symlink itself (lstat) and
    reports where it points; one leading out of the tree is listed but
    neither readable nor writable here."""
    info = os.lstat(path.full)
    is_link = stat.S_ISLNK(info.st_mode) or _is_junction(path.full)
    target_info = info
    escapes = False
    if is_link:
        escapes = not tree.contains(path.full)
        try:
            target_info = os.stat(path.full)
        except OSError:
            target_info = info
    is_dir = stat.S_ISDIR(target_info.st_mode)
    size = 0 if is_dir else target_info.st_size
    accessible = not escapes and not tree.is_hidden(path)
    created = _created(info)
    entry = {
        "name": os.path.basename(path.full),
        "path": path.rel,
        "type": ENTRY_DIR if is_dir else ENTRY_FILE,
        "size": size,
        "size_human": _human_size(size),
        "mode": stat.filemode(info.st_mode),
        "attributes": _attributes(os.path.basename(path.full), info),
        "nlink": info.st_nlink,
        "modified_at": int(info.st_mtime),
        "modified": _iso(info.st_mtime),
        "created_at": int(created),
        "created": _iso(created),
        "accessed_at": int(info.st_atime),
        "accessed": _iso(info.st_atime),
        "is_symlink": is_link,
        "readable": accessible,
        "writable": accessible and not tree.is_readonly(path),
    }
    if is_link:
        entry["link_target"] = _link_target(path.full)
        entry["link_outside_tree"] = escapes
    return entry


def _is_junction(full: str) -> bool:
    is_junction = getattr(os.path, "isjunction", None)
    return bool(is_junction and is_junction(full))


def _link_target(full: str) -> str | None:
    try:
        return os.readlink(full)
    except OSError:
        return None


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
    """One directory's entries, directories first, then by name.

    Entries are described as they are on disk, without Tree.resolve, so a
    symlink leading out of the tree shows up (flagged) instead of failing
    the whole listing.
    """
    entries = []
    for name in os.listdir(path.full):
        if name.startswith(STAGING_PREFIX):
            continue
        child = Resolved(rel=_join(path.rel, name), full=os.path.join(path.full, name))
        try:
            entries.append(_entry(tree, child))
        except OSError as err:
            log.warning("files: could not stat %s:%s: %s", tree.name, child.rel, err)
    entries.sort(key=lambda item: (item["type"] != ENTRY_DIR, item["name"].lower()))
    return {
        "tree": tree.name,
        "path": path.rel,
        "count": len(entries),
        "total_size": sum(entry["size"] for entry in entries),
        "entries": entries,
    }


def open_readable(tree: Tree, path: Resolved):
    """Open a file the caller may download, in binary mode.

    Opened here rather than by the response, so a file another process
    holds open without sharing (an expert's FileOpen, the terminal's own
    files) is reported as Locked instead of failing mid-response.
    """
    tree.check_readable(path)
    try:
        return open(path.full, "rb")  # noqa: SIM115 - the response streams and closes it
    except FileNotFoundError as err:
        # Deleted between the existence check and the open: Windows keeps a
        # just-deleted entry visible for a moment while the delete is pending.
        raise NotFound(f"{path.rel} does not exist") from err
    except PermissionError as err:
        raise Locked(f"{path.rel} is in use and cannot be read: {err}") from err


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
