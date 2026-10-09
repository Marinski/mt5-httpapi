"""Path resolution and access rules for one file tree.

A Tree is a root directory plus the paths under it that the API never shows
(credentials) or never changes (the running terminal's binaries, Chart
Deployments' own state). Every path a caller sends goes through
Tree.resolve, which refuses anything that could leave the root on either
Linux or Windows, so the operations never see an unchecked path.

Paths are relative to the root, use `/` (a `\\` is accepted and converted),
and are compared case-insensitively because the terminals run on Windows.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from mt5api.fileapi.errors import InvalidPath, Protected

# Characters Windows refuses in a file name, plus ':' which would otherwise
# name a drive or an alternate data stream.
_FORBIDDEN_CHARS = frozenset('<>:"|?*')
_RESERVED_STEM = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])$", re.IGNORECASE)
_MAX_SEGMENT_LENGTH = 255
_FIRST_PRINTABLE = 32
_DOT_SEGMENTS = frozenset({".", ".."})
_SEPARATOR = "/"

# Name prefixes of the API's own in-flight files. Never listed, never
# addressable, so a caller cannot race the write that created them.
STAGING_PREFIX = ".mt5api-"


@dataclass(frozen=True)
class Resolved:
    """A checked path: `rel` as the caller should see it, `full` on disk."""

    rel: str
    full: str

    @property
    def key(self) -> str:
        return self.rel.lower()


@dataclass(frozen=True)
class Tree:
    """One root the file API serves.

    `hidden` paths can be neither read nor changed. `readonly` paths, and
    everything under a readonly directory, can be read but not written or
    deleted. Both are relative to the root, lower case, `/`-separated.
    """

    name: str
    root: str
    hidden: frozenset[str] = field(default_factory=frozenset)
    readonly: tuple[str, ...] = ()

    def resolve(self, raw: str) -> Resolved:
        """Check a caller's path and map it under the root.

        The empty path is the root itself. Raises InvalidPath for anything
        that is malformed or that resolves, through a symlink or junction,
        outside the root.
        """
        segments = _segments(raw)
        rel = _SEPARATOR.join(segments)
        full = os.path.join(self.root, *segments)
        if not self._inside_root(full):
            raise InvalidPath(f"{raw!r} resolves outside the {self.name} tree")
        return Resolved(rel=rel, full=full)

    def is_hidden(self, path: Resolved) -> bool:
        return path.key in self.hidden

    def is_readonly(self, path: Resolved) -> bool:
        return path.key == "" or any(
            path.key == entry or path.key.startswith(entry + _SEPARATOR)
            for entry in self.readonly
        )

    def check_readable(self, path: Resolved) -> None:
        if self.is_hidden(path):
            raise Protected(f"{path.rel} holds credentials and is never exposed")

    def check_writable(self, path: Resolved) -> None:
        self.check_readable(path)
        if path.key == "":
            raise Protected(f"the root of the {self.name} tree cannot be replaced")
        if self.is_readonly(path):
            raise Protected(f"{path.rel} is read-only through the file API")

    def check_deletable_dir(self, path: Resolved) -> None:
        """A directory may go only if nothing protected lives under it."""
        self.check_writable(path)
        prefix = path.key + _SEPARATOR
        guarded = [entry for entry in (*self.hidden, *self.readonly) if entry.startswith(prefix)]
        if guarded:
            raise Protected(f"{path.rel} contains protected paths: {', '.join(sorted(guarded))}")

    def contains(self, full: str) -> bool:
        """Whether `full`, after following symlinks and junctions, is inside
        the root."""
        return self._inside_root(full)

    def _inside_root(self, full: str) -> bool:
        root = os.path.realpath(self.root)
        target = os.path.realpath(full)
        return target == root or target.startswith(root.rstrip(os.sep) + os.sep)


def _segments(raw: str) -> list[str]:
    text = (raw or "").replace("\\", _SEPARATOR)
    if text.startswith(_SEPARATOR):
        raise InvalidPath(f"{raw!r} is absolute; paths are relative to the tree root")
    if text.endswith(_SEPARATOR):
        text = text[: -len(_SEPARATOR)]
    if not text:
        return []
    segments = text.split(_SEPARATOR)
    for segment in segments:
        _check_segment(raw, segment)
    return segments


def _check_segment(raw: str, segment: str) -> None:
    if not segment:
        raise InvalidPath(f"{raw!r} has an empty path segment")
    if segment in _DOT_SEGMENTS:
        raise InvalidPath(f"{raw!r} contains a {segment!r} segment")
    if segment.startswith(STAGING_PREFIX):
        raise InvalidPath(f"{raw!r} names one of the API's own staging files")
    if len(segment) > _MAX_SEGMENT_LENGTH:
        raise InvalidPath(f"{raw!r} has a segment longer than {_MAX_SEGMENT_LENGTH} characters")
    if any(ord(char) < _FIRST_PRINTABLE or char in _FORBIDDEN_CHARS for char in segment):
        raise InvalidPath(f"{raw!r} contains a character Windows does not allow in a path")
    if segment[-1] in ". ":
        raise InvalidPath(f"{raw!r} has a segment ending in a dot or space")
    if _RESERVED_STEM.match(segment.split(".", 1)[0]):
        raise InvalidPath(f"{raw!r} names a Windows device ({segment})")
