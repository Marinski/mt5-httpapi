"""Typed failures of the file API.

mt5api/handlers/files.py maps each class to its HTTP status and error code,
so the operations here stay free of HTTP.
"""


class FileApiError(Exception):
    """Base class for every file API failure."""


class InvalidPath(FileApiError):
    """The path is malformed or would leave the tree: an absolute path, a
    `..` segment, a Windows device name, a character Windows refuses, or a
    symlink pointing out of the root."""


class Protected(FileApiError):
    """The path is one the API refuses to expose or change: the broker
    credentials, the running terminal's executables, or Chart Deployments'
    own files."""


class NotFound(FileApiError):
    """Nothing exists at the path."""


class Conflict(FileApiError):
    """The path exists with the wrong kind: a file where a directory is
    needed or the other way round, or a non-empty directory deleted without
    `recursive`."""


class Locked(FileApiError):
    """Windows refused the write or delete because another process, usually
    the terminal, holds the file open."""


class BadArchive(FileApiError):
    """The `?extract` body is not a usable zip: corrupt, encrypted, holding a
    symlink, or naming the same file twice."""


class ArchiveTooLarge(FileApiError):
    """The archive unpacks to more files or bytes than the configured caps."""
