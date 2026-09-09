"""Behavioral coverage for the awk port-filter embedded in scripts/healthcheck.sh.

The previous coverage in tests/test_terminal_instances.py only asserted that
certain SOURCE SUBSTRINGS (e.g. `'if (!have_group) { print port; return }'`)
appeared in healthcheck.sh. That kind of assertion passes even when the awk
program is present but wrong, and breaks on a harmless whitespace reformat —
it proves the text exists, not that the filter behaves correctly. These tests
extract the actual awk program and RUN it against fixture config.yaml files,
asserting on the emitted port list.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

HEALTHCHECK_PATH = Path(__file__).resolve().parents[1] / "scripts" / "healthcheck.sh"

# These exact strings bound the awk program inside healthcheck.sh (see the
# module docstring there). If either marker goes missing the script's shape
# changed enough that this whole suite needs a look, hence the assert below
# instead of a silent empty-program run.
_AWK_START_MARKER = 'PORTS=$(awk -v groupfile="$VM_GROUP" \''
_AWK_END_MARKER = '\' "$CONFIG")'

pytestmark = pytest.mark.skipif(
    shutil.which("awk") is None,
    reason="awk not available in this environment (Dockerfile.test is debian-based and ships it)",
)


def _extract_awk_program():
    src = HEALTHCHECK_PATH.read_text(encoding="utf-8")
    _before, sep, rest = src.partition(_AWK_START_MARKER)
    assert sep, "awk program start marker not found in healthcheck.sh"
    program, sep2, _after = rest.partition(_AWK_END_MARKER)
    assert sep2, "awk program end marker not found in healthcheck.sh"
    return program


@pytest.fixture
def awk_prog(tmp_path):
    """Write the awk program extracted from healthcheck.sh to a real file so it
    can be run standalone via `awk -f`, decoupled from the rest of the script.
    """
    prog_path = tmp_path / "healthcheck_ports.awk"
    prog_path.write_text(_extract_awk_program(), encoding="utf-8")
    return prog_path


def _write_config(tmp_path, terminals, name="config.yaml"):
    """Render a minimal config.yaml terminals: list matching the real file's
    indentation (two spaces then `- broker:`, four spaces for the rest) —
    the awk program's regexes anchor on exactly that shape.
    """
    lines = ["terminals:"]
    for t in terminals:
        lines.append(f"  - broker: {t['broker']}")
        lines.append(f"    account: {t['account']}")
        if t.get("instance") is not None:
            lines.append(f"    instance: {t['instance']}")
        lines.append(f"    port: {t['port']}")
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _run_awk(prog_path, groupfile_arg, config_path):
    result = subprocess.run(
        ["awk", "-v", f"groupfile={groupfile_arg}", "-f", str(prog_path), str(config_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


SIX_TERMINALS = [
    {"broker": "darwinex", "account": "live", "port": 6001},
    {"broker": "darwinex", "account": "demo", "port": 6002},
    {"broker": "ictrading", "account": "live", "port": 6003},
    {"broker": "ictrading", "account": "demo", "port": 6004},
    {"broker": "fxcm", "account": "live", "port": 6005},
    {"broker": "fxcm", "account": "demo", "port": 6006},
]
ALL_SIX_PORTS = [str(t["port"]) for t in SIX_TERMINALS]


def test_empty_groupfile_arg_emits_every_port(awk_prog, tmp_path):
    """groupfile="" is what a single-VM install passes (VM_GROUP unset/blank
    resolves the same way as a literal empty string). No group means no filter.
    """
    config = _write_config(tmp_path, SIX_TERMINALS)

    ports = _run_awk(awk_prog, "", config)

    assert ports == ALL_SIX_PORTS


def test_group_file_selects_only_matching_broker_account(awk_prog, tmp_path):
    """A group file listing `broker account` picks exactly those terminals,
    in the order they appear in config.yaml — not group-file order.
    """
    config = _write_config(tmp_path, SIX_TERMINALS)
    groupfile = tmp_path / "vm-group.txt"
    groupfile.write_text("darwinex demo\nfxcm live\n", encoding="utf-8")

    ports = _run_awk(awk_prog, str(groupfile), config)

    assert ports == ["6002", "6005"]


def test_group_file_that_exists_but_is_empty_falls_back_to_every_port(awk_prog, tmp_path):
    """An existing-but-empty group file must NOT mean "select nothing" — that
    is the exact trap the PR comment documents: an empty group file resulted
    in have_group staying unset (no valid `broker account` line was ever
    parsed), so the filter degrades to no-filter, same as no group file.
    """
    config = _write_config(tmp_path, SIX_TERMINALS)
    groupfile = tmp_path / "vm-group.txt"
    groupfile.write_text("", encoding="utf-8")

    ports = _run_awk(awk_prog, str(groupfile), config)

    assert ports == ALL_SIX_PORTS


def test_missing_groupfile_path_falls_back_to_every_port(awk_prog, tmp_path):
    """A groupfile path that does not exist on disk (e.g. single-VM install
    where the bind mount was never created) must behave like no filter, not
    zero ports.
    """
    config = _write_config(tmp_path, SIX_TERMINALS)
    groupfile = tmp_path / "does-not-exist.txt"

    ports = _run_awk(awk_prog, str(groupfile), config)

    assert ports == ALL_SIX_PORTS


def test_group_file_with_instance_selects_only_matching_instance(awk_prog, tmp_path):
    """Two terminals share broker+account and are distinguished only by
    instance. A `broker account instance` group line must select just the
    one whose instance matches.
    """
    terminals = [
        {"broker": "darwinex", "account": "live", "instance": "a", "port": 7001},
        {"broker": "darwinex", "account": "live", "instance": "b", "port": 7002},
    ]
    config = _write_config(tmp_path, terminals)
    groupfile = tmp_path / "vm-group.txt"
    groupfile.write_text("darwinex live a\n", encoding="utf-8")

    ports = _run_awk(awk_prog, str(groupfile), config)

    assert ports == ["7001"]


def test_group_line_without_instance_selects_only_default_instance_terminal(awk_prog, tmp_path):
    """A group line with no instance field (`broker account`) must map to the
    literal key "default" and select only the terminal that also has no
    instance set — not a sibling terminal that has an explicit instance.
    """
    terminals = [
        {"broker": "darwinex", "account": "live", "port": 8001},
        {"broker": "darwinex", "account": "live", "instance": "a", "port": 8002},
    ]
    config = _write_config(tmp_path, terminals)
    groupfile = tmp_path / "vm-group.txt"
    groupfile.write_text("darwinex live\n", encoding="utf-8")

    ports = _run_awk(awk_prog, str(groupfile), config)

    assert ports == ["8001"]


def test_group_file_ignores_comments_and_blank_lines(awk_prog, tmp_path):
    """Comment lines and blank lines in the group file must not be
    misinterpreted as broker/account entries or otherwise disturb the
    surrounding real entries.
    """
    config = _write_config(tmp_path, SIX_TERMINALS)
    groupfile = tmp_path / "vm-group.txt"
    groupfile.write_text(
        "# comment\n\ndarwinex demo\n# another comment\n\nfxcm live\n",
        encoding="utf-8",
    )

    ports = _run_awk(awk_prog, str(groupfile), config)

    assert ports == ["6002", "6005"]


# ── Verdict: is the VM dead, or just busy? ───────────────────────────────────
#
# These run the whole script with a stub `curl` on PATH, so they exercise the
# real verdict logic rather than the awk filter alone.

def _run_healthcheck(tmp_path, config, curl_body, grace=None):
    """Run healthcheck.sh with a fake curl that emits `curl_body` on stdout.

    The stub mimics curl closely enough for the script: it writes what a real
    `-w '%{http_code} %{time_connect}'` would print, and exits non-zero when
    the status is 000, exactly as curl does on a failed request.

    The slow-port counters go under tmp_path, so consecutive calls within one
    test see each other's state (as consecutive healthchecks in one container
    do) while tests never see each other's.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    curl = bindir / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        f"printf '%s' '{curl_body}'\n"
        f"case '{curl_body}' in 000*) exit 7 ;; esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)

    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HEALTHCHECK_CONFIG": str(config),
        "HEALTHCHECK_VM_GROUP": str(tmp_path / "no-such-group.txt"),
        "HEALTHCHECK_LEASES": str(tmp_path / "no-such-leases"),
        "HEALTHCHECK_STATE_DIR": str(tmp_path / "slow-state"),
    }
    if grace is not None:
        env["HEALTHCHECK_SLOW_GRACE"] = str(grace)
    return subprocess.run(
        ["sh", str(HEALTHCHECK_PATH)],
        capture_output=True, text=True, env=env, timeout=60,
    )


ONE_TERMINAL = [{"broker": "darwinex", "account": "live", "port": "6001"}]


def test_a_port_that_answers_is_healthy(tmp_path):
    config = _write_config(tmp_path, ONE_TERMINAL)
    result = _run_healthcheck(tmp_path, config, "200 0.000181")
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_port_with_nothing_listening_is_unhealthy(tmp_path):
    """Connection refused leaves time_connect at zero. This is the outage the
    healthcheck exists to catch, and it must still fail."""
    config = _write_config(tmp_path, ONE_TERMINAL)
    result = _run_healthcheck(tmp_path, config, "000 0.000000")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "DOWN" in result.stdout


def test_a_listening_but_slow_port_is_healthy(tmp_path):
    """The regression this pair exists for.

    A completed TCP handshake with no HTTP response in the probe window means
    the process is alive and the guest is merely CPU-saturated - a compile or a
    Strategy Tester run will do it. Reporting DOWN here makes a supervisor
    restart a VM that was working, turning a slow batch into an outage.
    """
    config = _write_config(tmp_path, ONE_TERMINAL)
    result = _run_healthcheck(tmp_path, config, "000 0.001204")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "slow but listening" in result.stdout


def test_a_missing_curl_fails_closed(tmp_path):
    """An empty probe result must read as DOWN, not as 'not zero, so busy'."""
    config = _write_config(tmp_path, ONE_TERMINAL)
    result = _run_healthcheck(tmp_path, config, "")
    assert result.returncode == 1, result.stdout + result.stderr


# ── Hung is not busy: the slow tolerance is bounded ──────────────────────────
#
# psyb0t (2026-09-04): "curl result 000 with a completed TCP connection" was
# healthy unconditionally, so an API that accepts TCP but never answers HTTP
# stayed healthy forever and the watchdog never recovered it. A busy spell ends
# and the port answers; a hung port does not - so tolerate the former for a
# bounded number of CONSECUTIVE checks and then call the latter what it is.

SLOW = "000 0.001204"       # handshake completed, no HTTP inside the window
ANSWERS = "200 0.000181"
REFUSED = "000 0.000000"


def _slow_runs(tmp_path, config, count, grace=None):
    return [_run_healthcheck(tmp_path, config, SLOW, grace=grace) for _ in range(count)]


def test_a_slow_port_is_tolerated_below_the_grace(tmp_path):
    config = _write_config(tmp_path, ONE_TERMINAL)
    for result in _slow_runs(tmp_path, config, 2, grace=3):
        assert result.returncode == 0, result.stdout + result.stderr
        assert "slow but listening" in result.stdout


def test_a_port_still_silent_at_the_grace_is_hung_and_down(tmp_path):
    """The regression. Third consecutive silent check with grace=3 -> DOWN,
    and the verdict says hung, not merely down, so an operator can tell it
    from a refused connection."""
    config = _write_config(tmp_path, ONE_TERMINAL)
    first, second, third = _slow_runs(tmp_path, config, 3, grace=3)
    assert first.returncode == 0 and second.returncode == 0
    assert third.returncode == 1, third.stdout + third.stderr
    assert "DOWN" in third.stdout and "hung" in third.stdout and "6001" in third.stdout
    # And it stays down while it stays silent.
    assert _run_healthcheck(tmp_path, config, SLOW, grace=3).returncode == 1


def test_an_answer_resets_the_slow_count(tmp_path):
    """slow, slow, ANSWER, slow, slow with grace=3 is healthy throughout: the
    answer proves the process serves, so the count starts over."""
    config = _write_config(tmp_path, ONE_TERMINAL)
    sequence = [SLOW, SLOW, ANSWERS, SLOW, SLOW]
    for body in sequence:
        result = _run_healthcheck(tmp_path, config, body, grace=3)
        assert result.returncode == 0, (body, result.stdout + result.stderr)
    # ...and the third silent check after the answer is the one that trips.
    assert _run_healthcheck(tmp_path, config, SLOW, grace=3).returncode == 1


def test_a_refused_connection_resets_the_slow_count(tmp_path):
    """Refused is DOWN on its own terms (nothing listening), and it means the
    hung process is gone - the next listener starts with a clean count."""
    config = _write_config(tmp_path, ONE_TERMINAL)
    _slow_runs(tmp_path, config, 2, grace=3)
    refused = _run_healthcheck(tmp_path, config, REFUSED, grace=3)
    assert refused.returncode == 1 and "hung" not in refused.stdout
    first, second = _slow_runs(tmp_path, config, 2, grace=3)
    assert first.returncode == 0 and second.returncode == 0


def test_the_default_grace_is_ten_checks(tmp_path):
    """Ten consecutive silent checks is ~5 minutes at the 30s interval - longer
    than any compile burst seen on the farm, far shorter than forever."""
    config = _write_config(tmp_path, ONE_TERMINAL)
    results = _slow_runs(tmp_path, config, 10)
    assert all(r.returncode == 0 for r in results[:9])
    assert results[9].returncode == 1 and "hung" in results[9].stdout


@pytest.mark.parametrize("bad", ["0", "00", "010", "banana", "", "-3", "2.5", " 5"])
def test_a_bad_grace_falls_back_to_the_default_not_to_forever(tmp_path, bad):
    """A typo must not silently restore the unbounded tolerance (or zero it).

    `00` slipped past a literal-`0` pattern and made the FIRST silent probe
    hung (`[ 1 -ge 00 ]` is true); `010` would read as octal. Both now strip
    to their decimal value first - `010` is simply 10, the default."""
    config = _write_config(tmp_path, ONE_TERMINAL)
    results = _slow_runs(tmp_path, config, 10, grace=bad)
    assert all(r.returncode == 0 for r in results[:9]), bad
    assert results[9].returncode == 1, bad


def test_an_unwritable_state_dir_degrades_to_tolerance_not_to_restarts(tmp_path):
    """If the counters cannot be kept, the script cannot know a port is hung -
    so it falls back to the busy verdict rather than inventing a streak. The
    wrong failure mode here would be restarting busy VMs whenever /tmp fills
    up. Documented degradation, pinned so nobody 'fixes' it into fail-closed."""
    config = _write_config(tmp_path, ONE_TERMINAL)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "curl").write_text(
        "#!/bin/sh\nprintf '%s' '000 0.001204'\nexit 7\n", encoding="utf-8"
    )
    (bindir / "curl").chmod(0o755)
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HEALTHCHECK_CONFIG": str(config),
        "HEALTHCHECK_VM_GROUP": str(tmp_path / "no-such-group.txt"),
        "HEALTHCHECK_LEASES": str(tmp_path / "no-such-leases"),
        # A file where the directory should be: mkdir -p and every write fail.
        "HEALTHCHECK_STATE_DIR": str(tmp_path / "not-a-dir"),
        "HEALTHCHECK_SLOW_GRACE": "2",
    }
    (tmp_path / "not-a-dir").write_text("", encoding="utf-8")
    for _ in range(4):
        result = subprocess.run(["sh", str(HEALTHCHECK_PATH)], capture_output=True, text=True, env=env, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "slow but listening" in result.stdout
        # ...but never silently: the verdict says the bound is off.
        assert "slow-state unwritable" in result.stdout


def test_a_leading_zero_grace_does_not_trip_on_the_first_probe(tmp_path):
    """The exact hole the counter-review found: with grace `00` the first
    silent probe used to be reported hung."""
    config = _write_config(tmp_path, ONE_TERMINAL)
    first = _run_healthcheck(tmp_path, config, SLOW, grace="00")
    assert first.returncode == 0, first.stdout + first.stderr
    assert "hung" not in first.stdout
