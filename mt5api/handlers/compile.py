"""Compile MQL5 source to .ex5 via MetaEditor.

Why this exists: building an EA otherwise requires a human on a Windows machine
with MetaEditor installed. This lets an automated caller compile source it has
generated or assembled, on the host that already has the toolchain.

Threat model, because this endpoint takes arbitrary text from a caller and hands
it to a compiler:

  * SOURCE TEXT ONLY. There is no caller-supplied path anywhere. `filename` is
    reduced to a bare stem and re-suffixed, so "../../terminal64" or
    "C:\\Windows\\x" cannot escape the temp directory.
  * The caller controls neither /log: nor /inc:. Both are computed here.
  * Nothing is read from disk on the caller's behalf. The only files touched are
    the ones written into a per-request temp directory, which is removed on
    every exit path.
  * The handler cannot trade, cannot restart a terminal, and never touches the
    MT5 SDK. It shells out to MetaEditor64.exe and reads back two files.

MetaEditor specifics worth knowing before editing this:

  * It exits NON-ZERO on warnings as well as errors, so the exit code cannot
    decide success. The log is the authority, and the produced .ex5 is the
    tiebreaker.
  * It writes its log as UTF-16LE with a BOM. Decoding it as UTF-8 yields
    mojibake and every regex below silently stops matching.
  * It emits the .ex5 beside the source file, not into a configurable output
    path — which is exactly why compiling inside the temp directory is enough
    to keep concurrent requests from colliding over output names.
"""

import base64
import fnmatch
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

from flask import jsonify, request

from mt5api.config import (
    COMPILE_INCLUDE_DIGESTS,
    COMPILE_INCLUDE_DIR,
    COMPILE_LOCAL_CACHE,
    COMPILE_MAX_EX5_BYTES,
    COMPILE_MAX_SOURCE_BYTES,
    COMPILE_METAEDITOR,
    COMPILE_TIMEOUT_SECONDS,
    COMPILE_WORK_DIR,
)
from mt5api.logger import log

#: The client treats a non-JSON body as a broken host, and `log` as always a
#: string. Both are enforced at every return in this module.
MAX_LOG_BYTES = 8192

#: MetaEditor is single-instance per installation directory and compiles are
#: short. One lock, no job queue — a queue would add failure modes (lost jobs,
#: status polling, restart recovery) to buy nothing at this duration.
_COMPILE_LOCK = threading.Lock()

#: How long a caller may wait for the lock on top of its own compile budget.
#: Without a bound, a stuck compile turns every later request into a hung
#: connection, which is the one failure the client cannot distinguish from a
#: dead host.
_LOCK_WAIT_MARGIN_SECONDS = 30

#: _COMPILE_LOCK above only serializes calls inside ONE process. Every mt5api
#: process on a VM exposes /compile and, by default, every one of them
#: resolves COMPILE_METAEDITOR (or the local mirror) to the SAME installation
#: directory — so in production the real contention for the "single-instance
#: per installation directory" MetaEditor is across N processes, not within
#: one. Verified live: two separate OS processes each holding their own
#: (empty) _COMPILE_LOCK ran MetaEditor fully concurrently against a shared
#: install with no serialization at all. This is the same O_CREAT|O_EXCL
#: claim idiom _claim_warmup() already uses for exactly this kind of
#: cross-process sharing (see below), extended to cover every real compile
#: and the warm-up compile alike — not just the "who gets to warm up" claim.
_CROSS_PROCESS_LOCK_BASENAME = ".compile-inflight.lock"

#: No named-mutex-with-timeout primitive is available identically on both the
#: Windows host this runs on and Linux (where it's tested) without an extra
#: dependency, so this polls instead. Cheap relative to a compile.
_CROSS_PROCESS_LOCK_POLL_SECONDS = 0.2

#: How often a live holder rewrites its lock file's mtime. The holder spends
#: its whole critical section inside a blocking subprocess call, so this runs
#: on its own thread.
_CROSS_PROCESS_LOCK_HEARTBEAT_SECONDS = 5

#: A lock whose mtime has not moved for this long is abandoned — its holder
#: died without releasing it (crash, kill -9, container stop).
#:
#: Deliberately NOT derived from COMPILE_TIMEOUT_SECONDS. When it was
#: (COMPILE_TIMEOUT_SECONDS + 120, the same value as the warm-up budget), a
#: slow but still-live holder could be declared stale while it was inside its
#: critical section: a second process would then delete that lock and create
#: its own, and the first holder's release would delete the SECOND holder's
#: lock, letting a third compiler in while the second was still running.
#:
#: A live holder now refreshes every _CROSS_PROCESS_LOCK_HEARTBEAT_SECONDS, so
#: this is a count of missed beats (12 of them) rather than a guess about how
#: long a compile may legitimately take. Ownership tokens below close the same
#: race from the other side.
_CROSS_PROCESS_LOCK_STALE_SECONDS = 60


def _cross_process_lock_path(metaeditor_path):
    """Lock file for the actual toolchain directory THIS call will invoke —
    the raw configured path, or the local mirror, whichever _local_toolchain()
    resolved to. Both are shared VM-wide by default, which is exactly what
    needs the lock."""
    return os.path.join(os.path.dirname(metaeditor_path), _CROSS_PROCESS_LOCK_BASENAME)


def _read_lock_token(path):
    """The token currently in the lock file, or None if it is gone/unreadable."""
    try:
        with open(path, "r", encoding="ascii") as handle:
            return handle.read(64).strip()
    except OSError:
        return None


class _CrossProcessLock:
    """A held cross-process compile lock.

    Carries a unique owner token so release can tell "my lock" from "a lock
    that replaced mine after I was wrongly reaped". Without that check a
    late-finishing holder deletes whoever owns the lock now, which admits a
    second concurrent MetaEditor — the whole thing this lock exists to prevent.
    """

    def __init__(self, path, token):
        self.path = path
        self.token = token
        self._stop = threading.Event()
        self._heartbeat = threading.Thread(
            target=self._beat, name="compile-lock-heartbeat", daemon=True
        )
        self._heartbeat.start()

    def _beat(self):
        while not self._stop.wait(_CROSS_PROCESS_LOCK_HEARTBEAT_SECONDS):
            # Re-check ownership every beat: if we were reaped and replaced,
            # refreshing the file would keep ANOTHER holder's lock alive and
            # hide its own staleness. Stop touching it instead.
            if _read_lock_token(self.path) != self.token:
                return
            try:
                os.utime(self.path, None)
            except OSError as exc:
                # Keep beating. A transient failure here (a Windows sharing
                # violation, an AV scan, a momentary EACCES) must not end
                # liveness for the rest of the hold — that would let the next
                # stale sweep reap a lock whose compile is still running.
                log.warning("compile: lock heartbeat at %s failed (%s)", self.path, exc)

    def release(self):
        self._stop.set()
        self._heartbeat.join(timeout=_CROSS_PROCESS_LOCK_HEARTBEAT_SECONDS)
        holder = _read_lock_token(self.path)
        if holder is None and os.path.exists(self.path):
            # Present but unreadable. We cannot prove it is still ours, and
            # deleting someone else's lock is the failure this class exists to
            # prevent — so leave it for the stale sweep and say so.
            log.warning(
                "compile: could not read the lock at %s to release it; "
                "leaving it for the stale sweep", self.path,
            )
            return
        if holder is None:
            return  # already gone
        if holder != self.token:
            # We were reaped as stale and someone else legitimately owns the
            # lock now. Removing it would let a third process in alongside
            # them. Leave it; their own release (or the stale reaper) handles it.
            log.warning(
                "compile: cross-process lock at %s is held by another owner; not removing",
                self.path,
            )
            return
        try:
            os.remove(self.path)
        except OSError:
            pass


def _reap_if_stale(path, token):
    """Remove an abandoned lock. A live holder's heartbeat keeps its mtime far
    inside the window, so this only fires for a holder that stopped running.

    Claim-by-rename, not a bare os.remove. Every waiter polls this, so the
    moment a lock does expire they all judge it stale together — and a loser's
    remove can land AFTER a winner has already created its replacement,
    deleting the NEW owner's lock and admitting a second MetaEditor. Ownership
    tokens do not help there: the damage is done before anyone releases.
    Renaming is atomic on POSIX and Windows alike, so exactly one racer moves
    a given file and the rest fail outright.
    """
    observed = _read_lock_token(path)
    if observed is None and not os.path.exists(path):
        return  # absent — nothing to reap
    if observed is None:
        # Present but unreadable. Do NOT bail out here: stat and unlink need no
        # read permission, so refusing to touch it would wedge every future
        # compile forever over one bad file — the exact outcome the stale
        # window exists to prevent. Fall through to the age check; an
        # unreadable lock older than the window is abandoned by definition.
        log.warning("compile: lock at %s exists but cannot be read", path)
    try:
        # Wall clock against the file's mtime, because mtime is the only
        # liveness signal that crosses processes. A forward clock step larger
        # than the stale window would therefore make every live lock look
        # abandoned at once; ntpd slews rather than steps after initial sync,
        # so this is accepted rather than defended against.
        if time.time() - os.path.getmtime(path) <= _CROSS_PROCESS_LOCK_STALE_SECONDS:
            return
    except OSError:
        return  # vanished under us

    claimed = f"{path}.{token}.dead"
    try:
        os.rename(path, claimed)
    except OSError:
        return  # another reaper claimed it first, or the holder released it

    if _read_lock_token(claimed) != observed:
        # What we moved is NOT the dead lock we judged: a winner reaped and
        # recreated it between our staleness check and our rename. Deleting it
        # would strand a live holder and let a second compiler in, so put it
        # straight back. `path` is empty here precisely because we just moved
        # it, so this all but always succeeds.
        try:
            if not os.path.exists(path):
                os.rename(claimed, path)
                return
        except OSError:
            pass
        log.warning("compile: could not restore a live lock at %s after a reap race", path)
        return

    try:
        os.remove(claimed)
    except OSError:
        pass


def _acquire_cross_process_lock(metaeditor_path, deadline):
    """Block (polling), until this process is the only one invoking
    `metaeditor_path`, or `deadline` (a time.monotonic() value) passes.

    Returns a _CrossProcessLock to pass to _release_cross_process_lock, or
    None on timeout.
    """
    path = _cross_process_lock_path(metaeditor_path)
    token = uuid.uuid4().hex
    while True:
        _reap_if_stale(path, token)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            try:
                os.write(fd, token.encode("ascii"))
            finally:
                os.close(fd)
            try:
                return _CrossProcessLock(path, token)
            except Exception:
                # The file is on disk but nothing owns it — e.g. the heartbeat
                # thread could not start. Leaving it would wedge the toolchain
                # for a whole stale window over a lock no one holds.
                try:
                    os.remove(path)
                except OSError:
                    pass
                raise
        except FileExistsError:
            pass  # another process holds it -- the expected, silent case
        except OSError as exc:
            # Anything else (permission denied, ENOSPC, a bad path) is NOT
            # ordinary contention: without this, a broken lock directory
            # would poll silently until `deadline` and come back as a plain
            # "compile busy", indistinguishable from real load.
            log.warning("compile: cross-process lock at %s unusable (%s)", path, exc)
        if time.monotonic() >= deadline:
            return None
        time.sleep(_CROSS_PROCESS_LOCK_POLL_SECONDS)


def _release_cross_process_lock(lock):
    if lock is None:
        return
    lock.release()

#: "Result: 0 errors, 2 warnings, 143 msec elapsed" — the summary line. Builds
#: differ on singular/plural and on the "Result:" prefix, so match the counts
#: rather than the whole line.
_ERRORS_RE = re.compile(r"(\d+)\s+error", re.IGNORECASE)
_WARNINGS_RE = re.compile(r"(\d+)\s+warning", re.IGNORECASE)

#: Fallback when no summary line is present: per-diagnostic lines look like
#: "ea.mq5(12,5) : error 123: ';' - unexpected token".
_ERROR_LINE_RE = re.compile(r":\s*error\s+\d+", re.IGNORECASE)
_WARNING_LINE_RE = re.compile(r":\s*warning\s+\d+", re.IGNORECASE)


#: Resolved once per process by _local_toolchain(): (metaeditor, include_dir),
#: or None when no mirror is configured or the mirror could not be built.
_LOCAL_TOOLCHAIN = None
_LOCAL_TOOLCHAIN_RESOLVED = False

#: When the mirrored include tree was last re-checked against the source.
_INCLUDES_CHECKED_AT = 0.0

#: Cached include-tree digest, and the root it was computed for.
_INCLUDE_HASH = None
_INCLUDE_HASH_KEY = None

#: Cached per-header digests, keyed by the same root.
_INCLUDE_FILES = None

#: Globs (relative to the include root) whose per-file digests are reported.
_INCLUDE_DIGEST_PATTERNS = tuple(
    pattern.strip() for pattern in (COMPILE_INCLUDE_DIGESTS or "").split(",") if pattern.strip()
)

#: How stale the mirrored include tree may get before it is re-validated.
#:
#: MetaEditor and its Config are effectively immutable between deployments, but
#: the include tree is NOT: a shared library gets edited and every build after
#: that is supposed to pick it up. Resolving the mirror once per process meant
#: an edited .mqh was invisible until the next restart, and the compile that
#: used the old copy still returned ok:true - a silently stale binary, which is
#: the worst failure shape this endpoint has. Bound that window instead.
INCLUDE_REFRESH_SECONDS = 60

#: What MetaEditor actually needs to compile. Deliberately NOT the whole
#: terminal directory - that also holds terminal64.exe, metatester64.exe and
#: Bases, roughly 350MB of things a compile never reads.
_TOOLCHAIN_ITEMS = ("MetaEditor64.exe", "Config", "MQL5")

#: Compile a throwaway EA in the background shortly after start, so the first
#: REAL caller does not pay MetaEditor's cold load - measured at 30-55s on a
#: busy host against ~2-3s warm. Only meaningful with a local mirror, which is
#: also the only configuration where the cold cost is worth eliminating.
WARMUP_ENABLED = bool(COMPILE_LOCAL_CACHE)

#: Wait this long before warming. The VM launches every terminal at boot, and a
#: MetaEditor run added to that contention makes the guest slower precisely when
#: its health probe is most marginal. Warm once things have settled instead.
WARMUP_DELAY_SECONDS = 180

#: Every API process on a VM exposes /compile and they share COMPILE_LOCAL_CACHE,
#: so an ungated warm-up means one MetaEditor per terminal - 20 of them on this
#: host, all at once. A claim file in the shared cache keeps it to one per VM.
_WARMUP_CLAIM = ".warmup-claim"

#: A claim older than this is treated as abandoned, so a process that died
#: holding it cannot disable warm-up for every future boot. The cache directory
#: survives reboots; the claim inside it must not be permanent.
WARMUP_CLAIM_TTL_SECONDS = 3600


def _is_current(src, dst):
    """True when dst already matches src closely enough to skip re-copying.

    Size plus whole-second mtime. Whole seconds because the mirror and the
    source can sit on filesystems with different timestamp resolution, and a
    sub-second difference there would make every file look stale forever.
    """
    try:
        s_stat = os.stat(src)
        d_stat = os.stat(dst)
    except OSError:
        return False
    return s_stat.st_size == d_stat.st_size and int(s_stat.st_mtime) == int(d_stat.st_mtime)


def _mirror_tree(src, dst):
    """Copy a directory into the mirror, skipping files already current.

    Returns (files copied, files pruned).

    This is deliberately not shutil.copytree(dirs_exist_ok=True): that re-copies
    every file on every call, and this runs at the first compile after each
    process start. The stock MQL5 Include tree is ~260 files, so on a slow
    host-shared mount an unconditional re-copy puts tens of seconds in front of
    the first compile - which is exactly the window where a caller is already
    waiting on a reverse-proxy timeout.
    """
    copied = 0
    expected = set()
    for root, _dirs, files in os.walk(src):
        relative = os.path.relpath(root, src)
        target = dst if relative == "." else os.path.join(dst, relative)
        os.makedirs(target, exist_ok=True)
        for name in files:
            src_file = os.path.join(root, name)
            dst_file = os.path.join(target, name)
            expected.add(os.path.relpath(dst_file, dst))
            if _is_current(src_file, dst_file):
                continue
            # copy2 preserves mtime, which is what makes the next run a no-op.
            shutil.copy2(src_file, dst_file)
            copied += 1

    # Copying alone leaves a one-way mirror: a header deleted from the source
    # stays here and keeps resolving, so `#include <Gone.mqh>` still compiles
    # and the tree the compiler reads drifts from the tree anyone is
    # maintaining. Drop what the source no longer has.
    #
    # Guarded on a non-empty source: if the mount is unreachable the walk above
    # yields nothing, and pruning against that would delete the entire mirror
    # over a transient failure.
    removed = 0
    if expected:
        for root, _dirs, files in os.walk(dst):
            for name in files:
                stale = os.path.join(root, name)
                if os.path.relpath(stale, dst) not in expected:
                    try:
                        os.remove(stale)
                        removed += 1
                    except OSError:
                        pass  # in use or already gone; the next pass retries
    return copied, removed


def _include_hash(include_root):
    """sha256 over the include tree the compiler was actually given.

    Returns "sha256:<hex>", or None if the tree cannot be read.

    Deliberately hashed from the tree passed as /inc:, NOT from the source it
    was mirrored from. The point of this value is to answer "which library is
    this binary built against" after the fact, so it has to describe what the
    compiler read - if the mirror were stale, a hash of the source would assert
    the opposite and be worse than no hash at all.

    Covers relative paths as well as contents so that adding, renaming or
    removing a header changes the digest, not just editing one.

    Cached against _INCLUDE_HASH_KEY because the tree only changes when this
    module copies into it, and re-reading ~16MB per compile buys nothing.
    """
    global _INCLUDE_HASH, _INCLUDE_HASH_KEY

    root = os.path.join(include_root, "Include")
    if not os.path.isdir(root):
        root = include_root
    if not os.path.isdir(root):
        return None
    if _INCLUDE_HASH_KEY == root and _INCLUDE_HASH:
        return _INCLUDE_HASH

    digest = hashlib.sha256()
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            # Sorted so the digest is stable across filesystems that hand back
            # directory entries in different orders.
            dirnames.sort()
            for name in sorted(filenames):
                path = os.path.join(dirpath, name)
                relative = os.path.relpath(path, root).replace(os.sep, "/")
                digest.update(relative.encode("utf-8", "replace") + b"\0")
                with open(path, "rb") as handle:
                    for chunk in iter(lambda h=handle: h.read(1024 * 1024), b""):
                        digest.update(chunk)
                digest.update(b"\0")
    except OSError as exc:
        log.warning("compile: could not hash include tree (%s)", exc)
        return None

    _INCLUDE_HASH_KEY = root
    _INCLUDE_HASH = "sha256:" + digest.hexdigest()
    return _INCLUDE_HASH


def _include_file_digests(include_root):
    """Per-header digests for the globs in COMPILE_INCLUDE_DIGESTS.

    Returns {relative path: "sha256:<hex>"}, or {} when nothing is configured
    or nothing matches.

    Exists because the tree hash cannot say WHAT moved. An upgrade of the stock
    MQL5 library and an edit of a caller's own shared header both change it,
    and the correct responses are opposites: the first needs no rebuild, the
    second needs every dependent artifact rebuilt. Without this a caller has to
    assume the expensive one.

    A configured header that is absent from the tree is simply absent here, not
    null - "not in the tree the compiler read" is the thing worth knowing
    before a rebuild, and a null would blur it with "present but unreadable".

    Same root and cache lifetime as _include_hash.
    """
    global _INCLUDE_FILES

    if not _INCLUDE_DIGEST_PATTERNS:
        return {}
    root = os.path.join(include_root, "Include")
    if not os.path.isdir(root):
        root = include_root
    if not os.path.isdir(root):
        return {}
    if _INCLUDE_HASH_KEY == root and _INCLUDE_FILES is not None:
        return _INCLUDE_FILES

    digests = {}
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for name in sorted(filenames):
                path = os.path.join(dirpath, name)
                relative = os.path.relpath(path, root).replace(os.sep, "/")
                if not any(
                    fnmatch.fnmatch(relative, pattern) for pattern in _INCLUDE_DIGEST_PATTERNS
                ):
                    continue
                digest = hashlib.sha256()
                with open(path, "rb") as handle:
                    for chunk in iter(lambda h=handle: h.read(1024 * 1024), b""):
                        digest.update(chunk)
                digests[relative] = "sha256:" + digest.hexdigest()
    except OSError as exc:
        log.warning("compile: could not hash individual includes (%s)", exc)
        return {}

    _INCLUDE_FILES = digests
    return digests


def _invalidate_include_hash():
    """Drop the cached digests. Called whenever this module writes into the tree."""
    global _INCLUDE_HASH, _INCLUDE_HASH_KEY, _INCLUDE_FILES
    _INCLUDE_HASH = None
    _INCLUDE_HASH_KEY = None
    _INCLUDE_FILES = None


def _refresh_mirrored_includes():
    """Re-validate the mirrored include tree against the source.

    Runs at most once every INCLUDE_REFRESH_SECONDS, and only walks MQL5 - not
    MetaEditor64.exe, which is 105MB and does not change under a running
    process.

    This exists because an edited shared header must reach subsequent builds.
    Without it the mirror is resolved once per process and an updated .mqh is
    invisible until the next restart, while the compile that used the old copy
    still returns ok:true. A silently stale binary is worse than a failed
    compile: nothing downstream can tell it apart from a correct one.

    Caller must hold _COMPILE_LOCK.
    """
    global _INCLUDES_CHECKED_AT

    if not _LOCAL_TOOLCHAIN or not COMPILE_LOCAL_CACHE:
        return
    if time.monotonic() - _INCLUDES_CHECKED_AT < INCLUDE_REFRESH_SECONDS:
        return

    _INCLUDES_CHECKED_AT = time.monotonic()
    src = os.path.join(os.path.dirname(COMPILE_METAEDITOR), "MQL5")
    dst = os.path.join(COMPILE_LOCAL_CACHE, "MQL5")
    if not os.path.isdir(src):
        return
    try:
        copied, removed = _mirror_tree(src, dst)
        if copied or removed:
            _invalidate_include_hash()
            log.info(
                "compile: include tree refreshed (%d changed, %d pruned)", copied, removed
            )
    except Exception as exc:  # noqa: BLE001 - keep compiling with what we have
        log.warning("compile: could not refresh includes (%s), using mirrored copy", exc)


def _local_toolchain():
    """Mirror the compile toolchain onto local disk, once per process.

    Returns (metaeditor_path, include_dir) to compile with, or None to use the
    configured paths as-is.

    Failure here is never fatal: if the mirror cannot be built we log it and
    fall back to the shared copy, which is slower but correct. A compile that
    works slowly beats a compile that stops working because a cache directory
    was not writable.

    Callers must hold _COMPILE_LOCK - this writes ~100MB and must not run twice
    concurrently.
    """
    global _LOCAL_TOOLCHAIN, _LOCAL_TOOLCHAIN_RESOLVED, _INCLUDES_CHECKED_AT

    if _LOCAL_TOOLCHAIN_RESOLVED:
        _refresh_mirrored_includes()
        return _LOCAL_TOOLCHAIN

    _LOCAL_TOOLCHAIN_RESOLVED = True
    _INCLUDES_CHECKED_AT = time.monotonic()
    if not COMPILE_LOCAL_CACHE:
        return None

    source_dir = os.path.dirname(COMPILE_METAEDITOR)
    try:
        os.makedirs(COMPILE_LOCAL_CACHE, exist_ok=True)
        for item in _TOOLCHAIN_ITEMS:
            src = os.path.join(source_dir, item)
            dst = os.path.join(COMPILE_LOCAL_CACHE, item)
            if not os.path.exists(src):
                continue
            if os.path.isdir(src):
                copied, removed = _mirror_tree(src, dst)
                if copied or removed:
                    _invalidate_include_hash()
                    log.info(
                        "compile: mirrored %s (%d refreshed, %d pruned)", item, copied, removed
                    )
            elif not _is_current(src, dst):
                shutil.copy2(src, dst)

        editor = os.path.join(COMPILE_LOCAL_CACHE, "MetaEditor64.exe")
        if not os.path.exists(editor):
            log.warning("compile: local cache built without MetaEditor, using shared copy")
            return None

        # Includes come from the mirrored tree so a compile never reaches back
        # across the slow mount for a .mqh either.
        include_dir = os.path.join(COMPILE_LOCAL_CACHE, "MQL5")
        if not os.path.isdir(include_dir):
            include_dir = COMPILE_INCLUDE_DIR

        _LOCAL_TOOLCHAIN = (editor, include_dir)
        log.info("compile: using local toolchain at %s", COMPILE_LOCAL_CACHE)
    except Exception as exc:  # noqa: BLE001 - fall back, never fail the request
        log.warning("compile: could not build local toolchain (%s), using shared copy", exc)
        _LOCAL_TOOLCHAIN = None

    return _LOCAL_TOOLCHAIN


def _tail(text, limit=MAX_LOG_BYTES):
    """Last `limit` bytes of a log, as a string. Never None."""
    if not text:
        return ""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return text
    # Cut on a character boundary so the tail is still valid UTF-8.
    return encoded[-limit:].decode("utf-8", errors="replace")


def _read_metaeditor_log(path):
    """Decode MetaEditor's log file. Missing or unreadable reads as empty."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError:
        return ""
    return _read_metaeditor_log_bytes(raw)


def _read_metaeditor_log_bytes(raw):
    """Decode MetaEditor's UTF-16LE log bytes.

    Split out from the file read so the decoding — the part that actually goes
    wrong — is testable against real bytes without a filesystem.

    Falls back through UTF-8 and latin-1 rather than raising: a log we cannot
    decode must not turn a real compile result into a 500.
    """
    if not raw:
        return ""

    for encoding in ("utf-16", "utf-8-sig", "utf-8", "latin-1"):
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        # A UTF-16 log decoded as UTF-8 comes back riddled with NULs; treat that
        # as a failed decode rather than returning shredded text.
        if "\x00" in text:
            continue
        return text.replace("\r\n", "\n")

    return raw.decode("utf-8", errors="replace").replace("\x00", "")


def _parse_counts(log_text):
    """(errors, warnings) from a MetaEditor log.

    Prefers the trailing summary line; falls back to counting diagnostics. When
    neither is present the counts are 0 and the caller decides on the .ex5.
    """
    errors = warnings = None

    # Walk backwards: the summary is the last line that carries both counts.
    for line in reversed(log_text.splitlines()):
        if _ERRORS_RE.search(line) and _WARNINGS_RE.search(line):
            errors = int(_ERRORS_RE.search(line).group(1))
            warnings = int(_WARNINGS_RE.search(line).group(1))
            break

    if errors is None:
        errors = len(_ERROR_LINE_RE.findall(log_text))
    if warnings is None:
        warnings = len(_WARNING_LINE_RE.findall(log_text))

    return errors, warnings


def _safe_stem(filename):
    """Reduce a caller-supplied filename to a harmless stem.

    Everything structural is discarded: directories, drive letters, traversal.
    What survives is a conservative character class, because this string becomes
    a filename inside the temp directory AND the .ex5 name we read back.
    """
    if not isinstance(filename, str):
        filename = ""
    # ntpath-style and posix separators, plus drive colons.
    stem = re.split(r"[\\/]", filename)[-1]
    stem = stem.split(":")[-1]
    if stem.lower().endswith(".mq5"):
        stem = stem[:-4]
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem).strip("._-")
    return stem or "ea"


def _json(payload, status):
    """Every exit from this handler goes through here, so the client never sees
    a non-JSON body — which it is documented to treat as a broken host."""
    payload.setdefault("log", "")
    if payload.get("log") is None:
        payload["log"] = ""
    return jsonify(payload), status


def _claim_warmup():
    """Claim the once-per-VM warm-up. True only for the process that wins it.

    O_CREAT|O_EXCL against a file in the shared cache, because the processes
    racing for this are separate PIDs - a threading.Lock would gate one process
    while the other nineteen went ahead and launched MetaEditor anyway.
    """
    if not COMPILE_LOCAL_CACHE:
        return False
    path = os.path.join(COMPILE_LOCAL_CACHE, _WARMUP_CLAIM)
    try:
        os.makedirs(COMPILE_LOCAL_CACHE, exist_ok=True)
        try:
            if time.time() - os.path.getmtime(path) > WARMUP_CLAIM_TTL_SECONDS:
                os.remove(path)
        except OSError:
            pass  # absent, or someone else just removed it - both fine
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        try:
            os.write(fd, time.strftime("%Y-%m-%dT%H:%M:%S").encode())
        finally:
            os.close(fd)
        return True
    except OSError:
        return False  # another process holds the claim


def _warmup():
    """Run one throwaway compile so the first real caller finds MetaEditor warm."""
    time.sleep(WARMUP_DELAY_SECONDS)
    if not _claim_warmup():
        return

    # Non-blocking: if a real compile is already running the toolchain is being
    # warmed by it, and making a caller queue behind a warm-up would defeat the
    # point of having one.
    if not _COMPILE_LOCK.acquire(blocking=False):
        log.info("compile warm-up: skipped, a compile is already running")
        return

    work_dir = None
    try:
        local = _local_toolchain()
        metaeditor, include_dir = local if local else (COMPILE_METAEDITOR, COMPILE_INCLUDE_DIR)
        if not os.path.exists(metaeditor):
            log.warning("compile warm-up: MetaEditor missing at %s", metaeditor)
            return

        os.makedirs(COMPILE_WORK_DIR, exist_ok=True)
        work_dir = tempfile.mkdtemp(prefix="warmup-", dir=COMPILE_WORK_DIR)
        source_path = os.path.join(work_dir, "warmup.mq5")
        with open(source_path, "w", encoding="utf-8-sig", newline="\r\n") as handle:
            handle.write("int OnInit(){return(INIT_SUCCEEDED);}\nvoid OnTick(){}\n")

        started = time.monotonic()
        warmup_budget = COMPILE_TIMEOUT_SECONDS + 120
        # Winning the warm-up claim only means no OTHER PROCESS is also trying
        # to warm up — it says nothing about a real compile landing on a
        # different process at the same moment and sharing this same
        # installation directory. Same cross-process serialization real
        # compiles get, so a warm-up cannot collide with one either.
        cross_lock = _acquire_cross_process_lock(metaeditor, time.monotonic() + warmup_budget)
        if cross_lock is None:
            log.warning("compile warm-up: could not get the cross-process compile lock in time")
            return
        try:
            subprocess.run(
                [
                    metaeditor,
                    f"/compile:{source_path}",
                    f"/log:{os.path.join(work_dir, 'warmup.log')}",
                    f"/inc:{include_dir}",
                ],
                capture_output=True,
                # Generous: the whole point is absorbing the cold load, which is
                # slower than any warm compile this deadline normally covers.
                timeout=warmup_budget,
            )
        finally:
            _release_cross_process_lock(cross_lock)
        log.info("compile warm-up done in %.1fs", time.monotonic() - started)
    except Exception as exc:  # noqa: BLE001 - a failed warm-up must not matter
        log.warning(
            "compile warm-up failed (%s); the first real compile pays the cold load", exc
        )
    finally:
        _COMPILE_LOCK.release()
        if work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)


def start_warmup():
    """Kick the warm-up off in the background. Never blocks or fails startup."""
    if not WARMUP_ENABLED:
        return
    threading.Thread(target=_warmup, daemon=True, name="compile-warmup").start()


def compile_source():
    """POST /compile — synchronous compile of one .mq5 to one .ex5."""
    started = time.monotonic()
    try:
        return _compile_source_inner(started)
    except Exception:  # noqa: BLE001 - the handler must never raise
        # The full traceback goes to the server log and ONLY there. Echoing
        # the exception class and message gave any caller who could provoke a
        # compiler or filesystem error a readout of internal paths and
        # implementation detail.
        log.exception("compile: unhandled error")
        return _json({"ok": False, "log": "internal error"}, 500)


def _body_byte_cap():
    """Upper bound on the raw request body, derived from the source cap.

    JSON escaping inflates the encoded string (worst case 6x for \\uXXXX
    escapes); 4x plus envelope slack admits every realistic encoding of a
    source that is itself within the cap. The authoritative check is on the
    DECODED source below - this one exists so an oversized body is refused
    from its declared Content-Length, before any of it is parsed.
    """
    return 4 * COMPILE_MAX_SOURCE_BYTES + 4096


def _compile_source_inner(started):
    # request.content_length is None for a chunked (Transfer-Encoding:
    # chunked, no Content-Length) request, which would silently skip the
    # size check below and let get_json() buffer an unbounded body into
    # memory before the decoded-source check downstream ever runs. A JSON
    # body this small has no legitimate reason to arrive chunked, so it is
    # refused before anything is read rather than merely falling through to
    # a slower path.
    declared = request.content_length
    if declared is None:
        log.warning("compile: rejected request with no declared Content-Length")
        return _json(
            {"ok": False, "log": "Content-Length header is required"},
            411,
        )
    if declared > _body_byte_cap():
        log.warning("compile: rejected %d-byte body (cap %d)", declared, _body_byte_cap())
        return _json(
            {"ok": False, "log": f"request body exceeds {_body_byte_cap()} bytes"},
            413,
        )

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _json({"ok": False, "log": "body must be a JSON object"}, 400)

    source = body.get("source")
    if not isinstance(source, str) or not source.strip():
        return _json({"ok": False, "log": "'source' must be a non-empty string"}, 400)

    # Enforced BEFORE anything touches disk: past this line the source is
    # written into the work dir, so this check is what bounds that write.
    source_bytes = len(source.encode("utf-8"))
    if source_bytes > COMPILE_MAX_SOURCE_BYTES:
        log.warning(
            "compile: rejected %d-byte source (cap %d)",
            source_bytes, COMPILE_MAX_SOURCE_BYTES,
        )
        return _json(
            {
                "ok": False,
                "log": (
                    f"source is {source_bytes} bytes; this server accepts at "
                    f"most {COMPILE_MAX_SOURCE_BYTES} (COMPILE_MAX_SOURCE_BYTES)"
                ),
            },
            413,
        )

    stem = _safe_stem(body.get("filename") or "ea.mq5")
    ea_version = body.get("ea_version")

    if not os.path.exists(COMPILE_METAEDITOR):
        log.error("compile: MetaEditor missing at %s", COMPILE_METAEDITOR)
        # The path is for the operator's log, not the caller's response.
        return _json(
            {"ok": False, "log": "MetaEditor is not available on this host"},
            500,
        )

    # Bound the wait so a stuck compile fails fast for everyone behind it
    # instead of holding connections open. One total budget for BOTH the
    # in-process lock below and the cross-process one inside _run_compile —
    # an absolute deadline off `started`, not two stacked relative timeouts.
    total_lock_budget = COMPILE_TIMEOUT_SECONDS + _LOCK_WAIT_MARGIN_SECONDS
    deadline = started + total_lock_budget
    if not _COMPILE_LOCK.acquire(timeout=max(0, deadline - time.monotonic())):
        log.warning("compile: lock wait exceeded %ss", total_lock_budget)
        return _json(
            {
                "ok": False,
                "log": f"compile busy: waited {total_lock_budget}s for the compiler lock",
            },
            504,
        )

    try:
        return _run_compile(stem, source, ea_version, started, deadline)
    finally:
        _COMPILE_LOCK.release()


def _run_compile(stem, source, ea_version, started, deadline):
    # Under the lock, so the one-time ~100MB mirror cannot race itself. Only
    # the FIRST call in a process's lifetime actually copies anything -- but
    # when it does, on a slow mount that can be tens of seconds (see
    # docs/compiling.md), and `deadline` was set before this ran. Extend it
    # by whatever this cost, the same way _warmup() computes its own lock
    # deadline AFTER _local_toolchain() rather than before -- otherwise an
    # uncontended request could get a spurious "busy" 504 that was actually
    # entirely mirror-build time, not lock contention.
    toolchain_started = time.monotonic()
    local = _local_toolchain()
    deadline += time.monotonic() - toolchain_started
    metaeditor, include_dir = local if local else (COMPILE_METAEDITOR, COMPILE_INCLUDE_DIR)

    os.makedirs(COMPILE_WORK_DIR, exist_ok=True)
    work_dir = tempfile.mkdtemp(prefix="compile-", dir=COMPILE_WORK_DIR)
    src_path = os.path.join(work_dir, f"{stem}.mq5")
    ex5_path = os.path.join(work_dir, f"{stem}.ex5")
    log_path = os.path.join(work_dir, "compile.log")

    try:
        # utf-8-sig: MetaEditor honours a BOM and will otherwise guess the
        # codepage, which mangles non-ASCII string literals in the source.
        with open(src_path, "w", encoding="utf-8-sig", newline="\r\n") as handle:
            handle.write(source)

        cmd = [
            metaeditor,
            f"/compile:{src_path}",
            f"/log:{log_path}",
            f"/inc:{include_dir}",
        ]

        log.info(
            "compile start stem=%s ea_version=%s bytes=%d dir=%s",
            stem, ea_version, len(source), work_dir,
        )

        # Serialize against every OTHER PROCESS sharing this same MetaEditor
        # install (see _acquire_cross_process_lock's docstring) — _COMPILE_LOCK
        # only serializes calls inside this one process.
        cross_lock = _acquire_cross_process_lock(metaeditor, deadline)
        if cross_lock is None:
            total_budget = COMPILE_TIMEOUT_SECONDS + _LOCK_WAIT_MARGIN_SECONDS
            log.warning(
                "compile: cross-process lock wait exceeded %ss stem=%s", total_budget, stem
            )
            return _json(
                {
                    "ok": False,
                    "log": f"compile busy: waited {total_budget}s for the compiler lock",
                },
                504,
            )

        timed_out = False
        returncode = None
        try:
            completed = subprocess.run(
                cmd,
                cwd=work_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=COMPILE_TIMEOUT_SECONDS,
                check=False,
            )
            returncode = completed.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
        except OSError as exc:
            log.error("compile: could not launch MetaEditor: %s", exc)
            # exc carries the executable's path; that stays in the server log.
            return _json({"ok": False, "log": "could not launch MetaEditor"}, 500)
        finally:
            _release_cross_process_lock(cross_lock)

        if timed_out:
            log.warning("compile timeout after %ss stem=%s", COMPILE_TIMEOUT_SECONDS, stem)
            return _json(
                {"ok": False, "log": f"compile timeout after {COMPILE_TIMEOUT_SECONDS}s"},
                504,
            )

        log_text = _read_metaeditor_log(log_path)
        errors, warnings = _parse_counts(log_text)

        ex5_bytes = b""
        if os.path.exists(ex5_path):
            # Size-checked on disk BEFORE the read: an artifact over the cap
            # must not transit memory or get base64-inflated into the
            # response at all. This is a server policy refusal, not a compile
            # failure - the caller's source built fine - so it does not take
            # the 422 path, and the log names the knob to raise.
            try:
                ex5_size = os.path.getsize(ex5_path)
            except OSError:
                ex5_size = 0
            if ex5_size > COMPILE_MAX_EX5_BYTES:
                log.error(
                    "compile: refusing %d-byte .ex5 (cap %d) stem=%s",
                    ex5_size, COMPILE_MAX_EX5_BYTES, stem,
                )
                return _json(
                    {
                        "ok": False,
                        "log": (
                            f"compiled binary is {ex5_size} bytes; this server "
                            f"returns at most {COMPILE_MAX_EX5_BYTES} "
                            f"(COMPILE_MAX_EX5_BYTES)"
                        ),
                    },
                    500,
                )
            try:
                with open(ex5_path, "rb") as handle:
                    ex5_bytes = handle.read()
            except OSError as exc:
                log.error("compile: could not read .ex5: %s", exc)

        elapsed = round(time.monotonic() - started, 3)

        # Success needs BOTH a clean log and an actual binary. Reporting ok:true
        # without a binary is the one thing the client cannot recover from, and
        # MetaEditor's exit code is not usable here because warnings make it
        # non-zero too.
        if errors == 0 and ex5_bytes:
            log.info(
                "compile ok stem=%s bytes=%d warnings=%d rc=%s dur=%.3fs",
                stem, len(ex5_bytes), warnings, returncode, elapsed,
            )
            body = {
                "ok": True,
                "ex5_base64": base64.b64encode(ex5_bytes).decode("ascii"),
                "log": _tail(log_text),
                "warnings": warnings,
            }
            # Identifies the library this binary was built against, so a caller
            # can answer "is this still current?" later without having to trust
            # that a sync had landed at the time. Omitted rather than guessed if
            # the tree cannot be read.
            digest = _include_hash(include_dir)
            if digest:
                body["include_hash"] = digest
            # Only alongside the tree hash: on its own it would say which of a
            # caller's headers changed without saying whether anything else did.
            per_file = _include_file_digests(include_dir) if digest else {}
            if per_file:
                body["include_files"] = per_file
            return _json(body, 200)

        # No binary but a clean log means MetaEditor failed in a way it did not
        # report as a diagnostic. That is still the caller's compile failing, so
        # it belongs on the 422 path with at least one error rather than a 500 —
        # but say so, because an empty log would otherwise look like success.
        if errors == 0 and not ex5_bytes:
            errors = 1
            log_text = (
                (log_text.rstrip() + "\n" if log_text.strip() else "")
                + f"compile produced no .ex5 (MetaEditor exit code {returncode})"
            )

        log.info(
            "compile failed stem=%s errors=%d warnings=%d rc=%s dur=%.3fs",
            stem, errors, warnings, returncode, elapsed,
        )
        return _json(
            {"ok": False, "log": _tail(log_text), "errors": errors},
            422,
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
