"""Every lock on %SHARED% must be scoped per VM.

%SHARED% is the same host directory for every VM, so any lock named without a
scope is shared ACROSS VMs rather than private to one. Booting two together
races them for it and the loser bails: start.bat with all of its terminals
unlaunched, install.bat having skipped its entire setup. The scope used to come
from a PowerShell MD5 of vm-group.txt, which returns nothing when PowerShell is
not yet available during early boot -- and the empty result fell through to the
shared lock silently.

Three scripts take part: start.bat and install.bat each hold a lock, and
reboot.bat releases both. reboot.bat matters as much as the other two -- aimed
at the unscoped names it would release a lock nobody holds, quietly becoming a
no-op, and aimed at every scoped name it would tear up a live lock belonging to
another VM.

These tests pin the properties that prevent all of that: the derivation cannot
depend on an external process, must be distinct per VM, and must be the same
derivation everywhere.
"""

import re
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
START_BAT = SCRIPTS / "start.bat"
INSTALL_BAT = SCRIPTS / "install.bat"
REBOOT_BAT = SCRIPTS / "reboot.bat"
ALL_SCRIPTS = [START_BAT, INSTALL_BAT, REBOOT_BAT]

# Each lock name built on %SHARED%: (script, variable, unscoped base name).
LOCK_NAMES = [
    (START_BAT, "LOCKDIR", "start.running"),
    (INSTALL_BAT, "LOCKDIR", "install.running"),
    (REBOOT_BAT, "START_LOCK", "start.running"),
    (REBOOT_BAT, "INSTALL_LOCK", "install.running"),
]

DERIVATION = re.compile(r'set "LOCK_SCOPE="\n.*?\n\)\n', re.S)


def _scope_region(path=None):
    """The `── Scope ...` section, up to the next section header."""
    text = (path or START_BAT).read_text(encoding="utf-8", errors="replace")
    start = text.index(":: -- Scope")
    m = re.search(r"\n:: -- ", text[start + 1:])
    nxt = (start + 1 + m.start()) if m else -1
    return text[start:] if nxt == -1 else text[start:nxt]


def _lock_scope_block():
    """The derivation, from `set "LOCK_SCOPE="` to where LOCKDIR is settled.

    LOCKDIR is settled (bare, then scoped) and logged before the acquire_lock.ps1
    section that follows, so that section header is the block's end.
    """
    text = START_BAT.read_text(encoding="utf-8", errors="replace")
    start = text.index('set "LOCK_SCOPE="')
    end = text.index("Boot-scoped lock", start)
    return text[start:end]


def _derive_scope(lines):
    """Mirror `for /f "usebackq eol=# tokens=1-3 delims= "` over vm-group.txt:
    blanks and #-comments skipped, first terminal joined with the line count."""
    first, count = None, 0
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        count += 1
        if first is None:
            fields = (line.split() + ["", "", ""])[:3]
            first = "_".join(fields)
    return f"{first}_{count}" if first else None


def test_lock_scope_derivation_shells_out_to_nothing():
    """No external process, so it cannot fail silently and fall back."""
    block = _lock_scope_block()
    assert "powershell" not in block.lower(), block
    assert "Get-FileHash" not in block, block


def test_lock_scope_is_read_with_for_f_from_the_group_file():
    block = _lock_scope_block()
    assert re.search(r'for /f "usebackq[^"]*" %%A in \("%VM_GROUP%"\)', block), block


def test_the_unscoped_lock_is_only_reachable_without_a_scope():
    """LOCKDIR may only be overridden to the scoped name, never the reverse."""
    block = _lock_scope_block()
    assert 'if defined LOCK_SCOPE set "LOCKDIR=%SHARED%\\start.running.!LOCK_SCOPE!"' in block
    # The bare assignment must come first, so the scoped one wins.
    assert block.index('set "LOCKDIR=%SHARED%\\start.running"') < block.index(
        'if defined LOCK_SCOPE set "LOCKDIR='
    )


def test_which_lock_was_chosen_is_logged_either_way():
    """The old failure was invisible except by inferring it from a filename."""
    block = _lock_scope_block()
    assert "boot lock scoped to" in block
    assert "unscoped boot lock" in block


# ── The behavioural contract the batch derivation has to satisfy ─────────────


def test_two_vms_get_different_scopes():
    fast = _derive_scope(["darwinex live a", "darwinex live b"])
    bulk = _derive_scope(["darwinex live l", "darwinex live m", "darwinex live n"])
    assert fast != bulk


def test_scope_is_stable_across_calls():
    lines = ["darwinex live l", "ictrading demo"]
    assert _derive_scope(lines) == _derive_scope(lines)


def test_terminals_without_an_instance_still_produce_a_scope():
    """`<broker> <account>` lines are valid; the third token is simply empty."""
    assert _derive_scope(["ictrading demo", "fxcm live"]) == "ictrading_demo__2"


def test_comments_and_blank_lines_do_not_change_the_scope():
    plain = _derive_scope(["darwinex live l", "darwinex live m"])
    noisy = _derive_scope(["# a comment", "", "darwinex live l", "  ", "darwinex live m"])
    assert plain == noisy


@pytest.mark.parametrize("lines", [[], ["# only a comment"], ["", "   "]])
def test_an_empty_group_file_yields_no_scope(lines):
    """Which leaves the unscoped lock -- correct for a single-VM install, and
    the same reading healthcheck.sh takes when the group file is empty."""
    assert _derive_scope(lines) is None


def test_the_group_files_this_topology_mounts_do_not_collide():
    """Only the files the generated compose actually mounts count.

    data/ also accumulates group files from earlier topologies -- vm-group-a
    and vm-group-b predate the fast/bulk naming -- and vm-group-a carries the
    same terminal set as vm-group-fast, so it derives the same scope. That is
    correct rather than a collision: the same terminals ARE the same VM, and
    two files describing one VM can never be mounted at once. Globbing data/
    compares a live file against its own superseded copy.
    """
    root = START_BAT.resolve().parents[1]
    compose = root / "docker-compose.yml"
    if not compose.exists():
        pytest.skip("no generated compose to read the topology from")

    mounted = sorted(set(re.findall(r"vm-group-[A-Za-z0-9_-]+\.txt", compose.read_text(encoding="utf-8"))))
    if len(mounted) < 2:
        pytest.skip("single-VM install; nothing to race")

    scopes = {}
    for name in mounted:
        path = root / "data" / name
        if not path.exists():
            pytest.skip(f"{name} is mounted but not generated yet")
        scopes[name] = _derive_scope(path.read_text(encoding="utf-8").splitlines())

    assert len(set(scopes.values())) == len(scopes), scopes


# ── The same derivation, in every script that names one of these locks ───────


@pytest.mark.parametrize("path", ALL_SCRIPTS, ids=lambda p: p.name)
def test_every_script_derives_the_scope_the_same_way(path):
    """Three copies that drift are one copy plus a latent cross-VM race."""
    found = DERIVATION.findall(_scope_region(path))
    assert len(found) == 1, path.name
    assert found[0] == DERIVATION.findall(_scope_region(START_BAT))[0], path.name


def _code_only(block):
    """Drop batch comments -- they discuss the PowerShell this replaced."""
    return "\n".join(
        line for line in block.splitlines()
        if not line.strip().lower().startswith(("::", "rem "))
    )


@pytest.mark.parametrize("path", ALL_SCRIPTS, ids=lambda p: p.name)
def test_no_script_shells_out_to_derive_the_scope(path):
    """An external process is what failed silently and fell through before."""
    code = _code_only(_scope_region(path))
    assert "powershell" not in code.lower(), path.name
    assert "Get-FileHash" not in code, path.name


@pytest.mark.parametrize(
    "path,var,base", LOCK_NAMES, ids=lambda v: v.name if hasattr(v, "name") else str(v)
)
def test_the_unscoped_name_is_only_reachable_without_a_scope(path, var, base):
    """The bare assignment must come first, so the scoped one wins."""
    text = path.read_text(encoding="utf-8", errors="replace")
    bare = 'set "%s=%%SHARED%%\\%s"' % (var, base)
    scoped = 'if defined LOCK_SCOPE set "%s=%%SHARED%%\\%s.!LOCK_SCOPE!"' % (var, base)
    assert bare in text, (path.name, bare)
    assert scoped in text, (path.name, scoped)
    assert text.index(bare) < text.index(scoped), path.name


def test_install_never_names_its_lock_directly():
    """The reboot-flag cleanup used to rmdir a hardcoded %SHARED%\\install.running,
    which after scoping would clear a lock nobody holds while leaving this VM's
    own stale lock in place."""
    text = INSTALL_BAT.read_text(encoding="utf-8", errors="replace")
    hardcoded = [
        line
        for line in text.splitlines()
        if "%SHARED%\\install.running" in line and not line.startswith(("set ", "if defined LOCK_SCOPE set "))
    ]
    assert hardcoded == [], hardcoded


def test_reboot_releases_through_the_variables_not_literal_paths():
    text = REBOOT_BAT.read_text(encoding="utf-8", errors="replace")
    assert 'rmdir /s /q "%START_LOCK%"' in text
    assert 'rmdir /s /q "%INSTALL_LOCK%"' in text
    assert 'rmdir /s /q "%SHARED%\\start.running"' not in text
    assert 'rmdir /s /q "%SHARED%\\install.running"' not in text


def test_reboot_logs_the_names_it_released():
    """Otherwise a scope mismatch is invisible: rmdir on a wrong path is silent."""
    text = REBOOT_BAT.read_text(encoding="utf-8", errors="replace")
    assert "!START_LOCK! and !INSTALL_LOCK!" in text


def test_install_logs_which_lock_it_chose():
    region = _scope_region(INSTALL_BAT)
    assert "Install lock scoped to" in region
    assert "unscoped install lock" in region
