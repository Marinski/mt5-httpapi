"""File API REST handlers: /files (the terminal's install directory) and
/compile/files (the MQL5 tree MetaEditor compiles against).

GET a directory lists it, GET a file downloads it, PUT uploads one file (raw
body or a multipart `file` field), PUT with `?extract` unpacks a zip into the
named directory, DELETE removes a file or, with `?recursive`, a directory.

Registered only when FILES_ENABLED (see server.py). Nothing here touches the
MT5 SDK, so these routes never queue behind the terminal lock.
"""
import os

from flask import Flask, jsonify, request, send_file

from mt5api import config
from mt5api.fileapi import compile_tree, ops, terminal_tree
from mt5api.fileapi.errors import (
    ArchiveTooLarge,
    BadArchive,
    Conflict,
    FileApiError,
    InvalidPath,
    Locked,
    NotFound,
    Protected,
)
from mt5api.logger import log

_UPLOAD_FIELD = "file"
_MULTIPART_PREFIX = "multipart/"
_OCTET_STREAM = "application/octet-stream"
_FALSE_FLAGS = frozenset({"0", "false", "no", "off"})

_ERROR_STATUS = {
    InvalidPath: (400, "BAD_PATH"),
    BadArchive: (400, "BAD_ARCHIVE"),
    Protected: (403, "PROTECTED"),
    NotFound: (404, "NOT_FOUND"),
    Conflict: (409, "CONFLICT"),
    Locked: (409, "FILE_LOCKED"),
    ArchiveTooLarge: (413, "ARCHIVE_TOO_LARGE"),
}


class _BodyTooLarge(Exception):
    pass


class _BadUpload(Exception):
    pass


def _flag(name: str) -> bool:
    """A query flag: present means on (`?extract`), unless set to a false
    value such as `?extract=0`."""
    if name not in request.args:
        return False
    return request.args.get(name, "").strip().lower() not in _FALSE_FLAGS


def _error(err: FileApiError):
    status, code = _ERROR_STATUS.get(type(err), (400, "BAD_REQUEST"))
    return jsonify({"error": str(err), "code": code}), status


def _read_body() -> bytes:
    """The upload: a multipart `file` field, or else the raw body.

    The form is parsed only for multipart. `curl --data-binary` labels a raw
    body as a urlencoded form, and parsing that would consume the stream.
    """
    cap = config.MAX_UPLOAD_BODY_BYTES
    stream = request.stream
    if (request.mimetype or "").startswith(_MULTIPART_PREFIX):
        upload = request.files.get(_UPLOAD_FIELD)
        if upload is None:
            raise _BadUpload(f"multipart upload has no {_UPLOAD_FIELD!r} field")
        stream = upload.stream
    data = stream.read(cap + 1)
    if len(data) > cap:
        raise _BodyTooLarge(f"upload exceeds {cap} bytes (MAX_UPLOAD_BODY_BYTES)")
    return data


def _terminal_tree():
    return terminal_tree(config.TERMINAL_DIR)


def _compile_tree():
    return compile_tree(config.COMPILE_INCLUDE_DIR)


def _includes_changed() -> None:
    from mt5api.handlers import compile as compile_handler

    compile_handler.mark_includes_changed()


def _get(tree_factory, rel: str = ""):
    tree = tree_factory()
    try:
        path, entry_kind = ops.kind(tree, rel)
        if entry_kind == ops.ENTRY_DIR:
            return jsonify(ops.list_dir(tree, path))
        full = ops.readable_file(tree, path)
    except FileApiError as err:
        return _error(err)
    return send_file(full, mimetype=_OCTET_STREAM, download_name=os.path.basename(full))


def _put(tree_factory, after_change, rel: str):
    tree = tree_factory()
    try:
        data = _read_body()
    except _BodyTooLarge as exc:
        return jsonify({"error": str(exc), "code": "TOO_LARGE"}), 413
    except _BadUpload as exc:
        return jsonify({"error": str(exc), "code": "BAD_REQUEST"}), 400
    try:
        if _flag("extract"):
            result = ops.extract_zip(
                tree, rel, data,
                max_bytes=config.FILES_MAX_EXTRACT_BYTES,
                max_files=config.FILES_MAX_EXTRACT_FILES,
            )
            status = 201
        else:
            result = ops.write_file(tree, rel, data)
            status = 201 if result["created"] else 200
    except FileApiError as err:
        log.warning("files: PUT %s:%s refused: %s", tree.name, rel, err)
        return _error(err)
    after_change()
    return jsonify(result), status


def _delete(tree_factory, after_change, rel: str):
    tree = tree_factory()
    try:
        result = ops.delete(tree, rel, recursive=_flag("recursive"))
    except FileApiError as err:
        log.warning("files: DELETE %s:%s refused: %s", tree.name, rel, err)
        return _error(err)
    after_change()
    return jsonify(result)


def _no_change() -> None:
    return None


def _routes(app: Flask, prefix: str, endpoint: str, tree_factory, after_change) -> None:
    def get_root():
        return _get(tree_factory)

    def get_path(rel):
        return _get(tree_factory, rel)

    def put_path(rel):
        return _put(tree_factory, after_change, rel)

    def delete_path(rel):
        return _delete(tree_factory, after_change, rel)

    app.add_url_rule(prefix, f"{endpoint}_root", get_root, methods=["GET"])
    app.add_url_rule(f"{prefix}/<path:rel>", f"{endpoint}_get", get_path, methods=["GET"])
    app.add_url_rule(f"{prefix}/<path:rel>", f"{endpoint}_put", put_path, methods=["PUT"])
    app.add_url_rule(
        f"{prefix}/<path:rel>", f"{endpoint}_delete", delete_path, methods=["DELETE"],
    )


def register_files_routes(app: Flask) -> None:
    """Register /files and /compile/files on `app`. One function so the
    server, the endpoint tests and the MCP route-catalog test agree."""
    _routes(app, "/files", "files", _terminal_tree, _no_change)
    _routes(app, "/compile/files", "compile_files", _compile_tree, _includes_changed)
