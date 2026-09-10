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
import time
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

def _run_healthcheck(tmp_path, config, curl_body, grace=None, state_dir=None):
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
        "HEALTHCHECK_STATE_DIR": state_dir or str(tmp_path / "slow-state"),
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


# ── Probe budget: many terminals must not blow the Docker healthcheck timeout ─


MANY_TERMINALS = [
    {"broker": "darwinex", "account": "live", "instance": chr(c), "port": str(6600 + i)}
    for i, c in enumerate(range(ord("a"), ord("a") + 24))
]


def _run_with_slow_curl(tmp_path, config, delay, timeout):
    """Run the script with a curl stub that sleeps, mimicking an unanswered probe.

    Every probe here hangs for `delay` seconds and then reports a refused
    connection, which is what a terminal that has not finished starting looks
    like.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    curl = bindir / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        f"sleep {delay}\n"
        "printf '000 0.000000'\n"
        "exit 7\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HEALTHCHECK_CONFIG": str(config),
        "HEALTHCHECK_VM_GROUP": str(tmp_path / "no-such-group.txt"),
        "HEALTHCHECK_LEASES": str(tmp_path / "no-such-leases"),
        # Without this the run reads and writes the REAL /tmp/healthcheck-slow,
        # so it is neither hermetic nor safe to run in parallel with itself.
        "HEALTHCHECK_STATE_DIR": str(tmp_path / "slow-state"),
    }
    started = time.monotonic()
    result = subprocess.run(
        ["sh", str(HEALTHCHECK_PATH)],
        capture_output=True, text=True, env=env, timeout=timeout,
    )
    return result, time.monotonic() - started


def test_24_slow_terminals_still_finish_inside_the_docker_timeout(tmp_path):
    """The bug this parallelisation exists for.

    24 terminals x a 3s probe ran sequentially is 72s, well past the compose
    `timeout: 30s`. Docker killed the check and recorded "Health check exceeded
    timeout" — which a supervisor cannot tell apart from a dead VM, so it
    restarted VMs that were merely still starting.

    Fanned out, the wall clock is one probe, not twenty-four of them.
    """
    config = _write_config(tmp_path, MANY_TERMINALS)
    result, elapsed = _run_with_slow_curl(tmp_path, config, delay=2, timeout=60)

    assert elapsed < 20, f"probes did not run concurrently: {elapsed:.1f}s for 24 ports"
    # And the verdict must still be the useful one, naming the ports.
    assert result.returncode == 1
    assert "DOWN ports:" in result.stdout


def test_concurrent_probes_still_report_every_dead_port(tmp_path):
    """Fanning out must not lose results: all 24 ports belong in the verdict."""
    config = _write_config(tmp_path, MANY_TERMINALS)
    result = _run_healthcheck(tmp_path, config, "000 0.000000")

    assert result.returncode == 1
    reported = {tok for tok in result.stdout.split() if tok.isdigit()}
    # The SET, not the count: a duplicated port plus a dropped one would pass a
    # length check while losing a real result.
    assert reported == {t["port"] for t in MANY_TERMINALS}, result.stdout


def test_the_hung_bound_survives_the_fan_out(tmp_path):
    """The two changes meet here.

    The bound was written against the sequential probe loop, where the counter
    lived in the parent shell's own flow. Fanned out, each port's counter is
    incremented inside its own background job, so this pins that all 24 still
    count independently and all 24 reach the bound together - a hung API is a
    hung API whether the VM carries one terminal or two dozen.
    """
    config = _write_config(tmp_path, MANY_TERMINALS)
    first, second, third = _slow_runs(tmp_path, config, 3, grace=3)

    assert first.returncode == 0 and second.returncode == 0
    assert third.returncode == 1, third.stdout + third.stderr
    assert "hung" in third.stdout
    for terminal in MANY_TERMINALS:
        assert terminal["port"] in third.stdout


def test_a_slow_state_write_failure_is_reported_once_not_per_port(tmp_path):
    """With the counters unwritable every one of the 24 jobs fails to write.
    The parent learns it from a marker rather than from a variable it cannot
    see across the fork, and says so once."""
    config = _write_config(tmp_path, MANY_TERMINALS)
    (tmp_path / "not-a-dir").write_text("", encoding="utf-8")
    result = _run_healthcheck(
        tmp_path, config, SLOW, grace=3, state_dir=str(tmp_path / "not-a-dir")
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("slow-state unwritable") == 1


def test_a_port_configured_twice_is_probed_once(tmp_path):
    """A duplicated port is a misconfiguration, and it used to be acted on
    twice.

    Fanned out that is two background jobs writing one verdict file and one
    slow counter, which is a real race; sequentially it was worse in a quieter
    way, because the counter was incremented twice per check and the hung bound
    fired at half the configured grace. Emitting each port once removes both.
    """
    duplicated = [
        {"broker": "darwinex", "account": "live", "instance": "a", "port": "6001"},
        {"broker": "darwinex", "account": "live", "instance": "b", "port": "6001"},
    ]
    config = _write_config(tmp_path, duplicated)

    first, second = _slow_runs(tmp_path, config, 2, grace=3)
    assert first.returncode == 0 and second.returncode == 0
    # Named once in the port list...
    listed = first.stdout.split("all ports up:")[1]
    assert listed.split().count("6001") == 1, first.stdout
    # ...and counted once, so the grace still means what it says: the third
    # consecutive silent check is the one that trips it, not the second.
    assert (tmp_path / "slow-state" / "6001").read_text().strip() == "2"
    third = _run_healthcheck(tmp_path, config, SLOW, grace=3)
    assert third.returncode == 1 and "hung" in third.stdout


# ── What the fan-out must not break ──────────────────────────────────────────


def _run_with_scripted_curl(tmp_path, config, body, extra_env=None):
    """Run the script with a curl stub that can answer differently per port."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    curl = bindir / "curl"
    curl.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    curl.chmod(0o755)
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HEALTHCHECK_CONFIG": str(config),
        "HEALTHCHECK_VM_GROUP": str(tmp_path / "no-such-group.txt"),
        "HEALTHCHECK_LEASES": str(tmp_path / "no-such-leases"),
        "HEALTHCHECK_STATE_DIR": str(tmp_path / "slow-state"),
    }
    env.update(extra_env or {})
    return subprocess.run(
        ["sh", str(HEALTHCHECK_PATH)], capture_output=True, text=True, env=env, timeout=120
    )


def test_each_port_gets_its_own_verdict(tmp_path):
    """Routing. Every other test feeds one uniform reply to all 24 ports, so a
    job that reported another port's result would sail through them.

    Here the reply depends on the port: one third answer, one third accept but
    stay silent, one third refuse - and each third must land in its own bucket.
    """
    config = _write_config(tmp_path, MANY_TERMINALS)
    result = _run_with_scripted_curl(
        tmp_path, config,
        # $@ ends with the URL; take the port out of it.
        'url=$(eval echo \\$$#)\n'
        'port=${url##*:}; port=${port%%/*}\n'
        'case $((port % 3)) in\n'
        '  0) printf "%s" "200 0.000181"; exit 0 ;;\n'
        '  1) printf "%s" "000 0.001204"; exit 7 ;;\n'
        '  2) printf "%s" "000 0.000000"; exit 7 ;;\n'
        'esac\n',
    )
    ports = [int(t["port"]) for t in MANY_TERMINALS]
    reported_dead = {int(t) for t in result.stdout.split("(")[0].split() if t.isdigit()}

    assert result.returncode == 1, result.stdout + result.stderr
    assert reported_dead == {p for p in ports if p % 3 == 2}, result.stdout
    # And the slow third is counted as slow, not lost and not called dead.
    slow_counted = sorted(int(f.name) for f in (tmp_path / "slow-state").iterdir())
    assert slow_counted == sorted(p for p in ports if p % 3 == 1)


def test_a_job_that_dies_before_reporting_is_a_down_port(tmp_path):
    """Fail closed, which is the documented rule and had no test.

    The parent reads each job's exit status; a job killed outright returns
    128+signal, which is not one of the verdicts. That must read as down, and
    must not touch the ports beside it.
    """
    config = _write_config(tmp_path, MANY_TERMINALS[:3])
    doomed = MANY_TERMINALS[1]["port"]
    result = _run_with_scripted_curl(
        tmp_path, config,
        'url=$(eval echo \\$$#)\n'
        'port=${url##*:}; port=${port%%/*}\n'
        f'if [ "$port" = "{doomed}" ]; then kill -9 $PPID; sleep 5; fi\n'
        'printf "%s" "200 0.000181"\n',
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert doomed in result.stdout
    for survivor in (MANY_TERMINALS[0]["port"], MANY_TERMINALS[2]["port"]):
        assert survivor not in result.stdout.split("(")[0], result.stdout


def test_a_disk_that_cannot_be_written_never_becomes_a_port_outage(tmp_path):
    """The reason the verdicts travel in exit statuses rather than in files.

    A per-port verdict file under a `mktemp -d` looks obviously fine until the
    disk is full: the write fails, the parent finds no verdict, and its
    fail-closed rule reports every terminal on the VM as down. Ten of those and
    the watchdog recreates a perfectly healthy VM, killing two dozen running
    backtests, because /tmp filled up. The sequential loop this replaced had no
    disk dependency and neither may this.
    """
    config = _write_config(tmp_path, MANY_TERMINALS)
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("", encoding="utf-8")

    result = _run_with_scripted_curl(
        tmp_path, config, 'printf "%s" "200 0.000181"\n',
        # Every place a scratch-file implementation could put its verdicts:
        # a `mktemp -d` honours TMPDIR, a fixed root honours HEALTHCHECK_RUN_DIR,
        # and the slow counters use HEALTHCHECK_STATE_DIR. All three point at a
        # FILE, so any attempt to create a directory there fails - as root too,
        # which a permission bit would not achieve.
        extra_env={
            "HEALTHCHECK_STATE_DIR": str(blocked),
            "HEALTHCHECK_RUN_DIR": str(blocked),
            "TMPDIR": str(blocked),
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok all ports up" in result.stdout
    for terminal in MANY_TERMINALS:
        assert terminal["port"] in result.stdout


def test_the_check_leaves_nothing_behind_to_clean_up(tmp_path):
    """No scratch directory means nothing can leak when Docker SIGKILLs a check
    that overran its timeout - and no trap is needed to promise otherwise."""
    config = _write_config(tmp_path, MANY_TERMINALS)
    before = set(Path("/tmp").glob("tmp.*"))
    result = _run_healthcheck(tmp_path, config, "200 0.000181")
    assert result.returncode == 0, result.stdout + result.stderr
    assert set(Path("/tmp").glob("tmp.*")) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bin", "config.yaml", "slow-state"]


def test_curl_is_invoked_the_way_its_output_is_parsed(tmp_path):
    """The stub curl ignores its arguments, so every other test here passes
    against a script that calls curl wrongly.

    It bit for real: a `-w` format that reached curl with literal quotes around
    it produced output whose first field was not a status code, and the script
    reported every port on both live VMs as `slow but listening` while all 24
    terminals were answering 401. Nothing in this file could see it.

    So: capture the real argument vector and assert the two things the parser
    depends on - the exact `-w` format, and a bounded `--max-time`.
    """
    config = _write_config(tmp_path, ONE_TERMINAL)
    argv = tmp_path / "argv.txt"
    result = _run_with_scripted_curl(
        tmp_path, config,
        f'for a in "$@"; do printf "%s\\n" "$a" >>"{argv}"; done\n'
        'printf "%s" "200 0.000181"\n',
    )
    assert result.returncode == 0, result.stdout + result.stderr

    args = argv.read_text(encoding="utf-8").splitlines()
    assert "%{http_code} %{time_connect}" in args, args
    assert "--max-time" in args, args
    assert args[args.index("--max-time") + 1] == "3", args
    assert any(a.endswith(":6001/ping") for a in args), args


def test_a_real_http_status_from_the_stub_reads_as_up_not_slow(tmp_path):
    """The symptom the mangled format produced, pinned from the other side: an
    answering port must land in `all ports up` and NOT in `slow but listening`,
    and a 401 from the auth layer counts as answering."""
    config = _write_config(tmp_path, ONE_TERMINAL)
    for body in ("200 0.000181", "401 0.000379", "503 0.000112"):
        result = _run_healthcheck(tmp_path, config, body)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "slow but listening" not in result.stdout, (body, result.stdout)
        assert result.stdout.startswith("ok all ports up:"), (body, result.stdout)
