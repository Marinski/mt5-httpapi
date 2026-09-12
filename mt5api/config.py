import argparse
import math
import os
import re

import MetaTrader5 as mt5

HOST = "0.0.0.0"

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(PACKAGE_DIR)
CONFIG_YAML = os.path.join(BASE_DIR, "config", "config.yaml")
BROKERS_DIR = os.path.join(BASE_DIR, "terminals")
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
DEFAULT_BACKTEST_TIMEOUT = "6h"
DEFAULT_INSTANCE = "default"


def load_yaml_config():
    """Read config.yaml. Returns {} if missing or unparseable.

    yaml import is deferred so module import doesn't blow up in
    environments where pyyaml isn't installed yet (start.bat installs
    it before launching the API process).
    """
    try:
        import yaml
    except ImportError:
        return {}
    if not os.path.exists(CONFIG_YAML):
        return {}
    try:
        with open(CONFIG_YAML, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _parse_args():
    parser = argparse.ArgumentParser(description="MT5 HTTP API")
    parser.add_argument("--broker", default=None)
    parser.add_argument("--account", default=None)
    parser.add_argument("--instance", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument(
        "--utc-offset",
        default=None,
        dest="utc_offset",
        help="Broker's UTC offset as a duration string ('3h', '3h30m', "
             "'-2h', '0', '90m'). MT5 returns timestamps in broker wall-clock "
             "time disguised as unix UTC; this offset normalizes them to real "
             "UTC on the wire. Negative values are allowed for west-of-UTC "
             "brokers.",
    )
    parser.add_argument(
        "--mode",
        default=None,
        choices=["live", "backtest"],
        help="Terminal role. 'live' (default) initializes the MT5 SDK and "
             "runs the monitor; 'backtest' skips both so this process can "
             "spawn terminal64.exe /portable subprocesses against the same "
             "data dir without hitting MT5's single-instance lock.",
    )
    args, _ = parser.parse_known_args()
    return args


_DURATION_RE = re.compile(
    r"^\s*(?P<sign>[+-])?\s*"
    r"(?:(?P<d>\d+(?:\.\d+)?)\s*d)?\s*"
    r"(?:(?P<h>\d+(?:\.\d+)?)\s*h)?\s*"
    r"(?:(?P<m>\d+(?:\.\d+)?)\s*m)?\s*"
    r"(?:(?P<s>\d+(?:\.\d+)?)\s*s)?\s*$",
    re.IGNORECASE,
)


def parse_duration_to_seconds(value):
    """Parse '3d', '3h', '3h30m', '-2h', '90m', '0' into integer seconds.

    Bare numbers (e.g. '3' or '3.5' or 3) are interpreted as HOURS for
    convenience — most brokers run on whole-hour offsets.
    """
    if value is None or value == "":
        return 0
    if isinstance(value, (int, float)):
        f = float(value)
        if not math.isfinite(f):
            raise ValueError(f"Invalid duration: {value!r}. Must be finite.")
        return int(round(f * 3600))
    s = str(value).strip()
    if not s:
        return 0
    # Bare number → hours.
    try:
        f = float(s)
    except ValueError:
        f = None
    if f is not None:
        if not math.isfinite(f):
            raise ValueError(f"Invalid duration: {value!r}. Must be finite.")
        return int(round(f * 3600))
    m = _DURATION_RE.match(s)
    if not m or not (m.group("d") or m.group("h") or m.group("m") or m.group("s")):
        raise ValueError(
            f"Invalid duration: {value!r}. "
            "Use '3d', '3h', '3h30m', '-2h', '90m', or a bare number (hours)."
        )
    d = float(m.group("d") or 0)
    h = float(m.group("h") or 0)
    minutes = float(m.group("m") or 0)
    secs = float(m.group("s") or 0)
    total = d * 86400 + h * 3600 + minutes * 60 + secs
    # The unit path needs the same finiteness guard as the bare-number paths:
    # the regex accepts arbitrarily many digits, so a ~400-digit hours value
    # makes float() return inf and int(round(inf)) raise an uncaught
    # OverflowError — a 500 where the caller should get a 400.
    if not math.isfinite(total):
        raise ValueError(f"Invalid duration: {value!r}. Must be finite.")
    if m.group("sign") == "-":
        total = -total
    return int(round(total))


def normalize_instance(value):
    if value in (None, ""):
        return DEFAULT_INSTANCE
    return str(value).strip() or DEFAULT_INSTANCE


def match_terminal_config(terms, broker=None, account=None, instance=None):
    wanted_instance = normalize_instance(instance) if instance is not None else None
    for terminal in terms:
        if broker is not None and terminal.get("broker") != broker:
            continue
        if account is not None and terminal.get("account", "") != account:
            continue
        terminal_instance = normalize_instance(terminal.get("instance"))
        if wanted_instance is not None and terminal_instance != wanted_instance:
            continue
        return {
            "broker": terminal.get("broker", "default"),
            "account": terminal.get("account", ""),
            "instance": terminal_instance,
            "port": terminal.get("port"),
            "utc_offset": terminal.get("utc_offset", "0"),
            "mode": (terminal.get("mode") or "live"),
            "symbol_suffix": terminal.get("symbol_suffix"),
        }
    return None


def terminal_dir_candidates(brokers_dir, broker, account="", instance=DEFAULT_INSTANCE):
    candidates = []
    normalized_instance = normalize_instance(instance)
    if account:
        candidates.append(
            os.path.join(brokers_dir, broker, account, normalized_instance, "terminal64.exe")
        )
        candidates.append(os.path.join(brokers_dir, broker, account, "terminal64.exe"))
    candidates.append(os.path.join(brokers_dir, broker, "base", "terminal64.exe"))
    return candidates


def make_identity(broker, account="", instance=DEFAULT_INSTANCE):
    normalized_instance = normalize_instance(instance)
    if account:
        return f"{broker}/{account}/{normalized_instance}"
    return broker


def load_terminal_config():
    """Default broker/account when CLI args aren't supplied.

    start.bat always passes --broker/--account/--port/--utc-offset, so
    this is only hit when running the API directly for testing. Falls
    back to the first entry in config.yaml's terminals list.
    """
    cfg = load_yaml_config()
    terms = cfg.get("terminals") or []
    if _args.broker or _args.account or _args.instance:
        match = match_terminal_config(
            terms,
            broker=_args.broker,
            account=_args.account,
            instance=_args.instance,
        )
        if match:
            return match
    if terms:
        return match_terminal_config(terms) or {
            "broker": "default",
            "account": "",
            "instance": DEFAULT_INSTANCE,
            "mode": "live",
        }
    return {
        "broker": "default",
        "account": "",
        "instance": DEFAULT_INSTANCE,
        "mode": "live",
    }


_args = _parse_args()
_terminal_config = load_terminal_config()

BROKER = _args.broker or _terminal_config.get("broker", "default")
ACCOUNT = _args.account or _terminal_config.get("account", "")
INSTANCE = normalize_instance(_args.instance or _terminal_config.get("instance"))
PORT = _args.port or _terminal_config.get("port") or 6542
API_TOKEN = _args.token or os.environ.get("API_TOKEN", "")

# ── Compile endpoint (POST /compile) ─────────────────────────────
# A SECOND credential that opens /compile and nothing else. The existing
# API_TOKEN unlocks order placement, position management and terminal restart;
# a caller that only compiles must not hold that. server.py
# enforces the split: API_TOKEN works everywhere, COMPILE_API_TOKEN works only
# on /compile.
_compile_cfg = load_yaml_config()


def _compile_setting(env_name, yaml_name, default=""):
    """Env wins, then config.yaml, then the default -- the same precedence the
    rest of this file uses for tokens and timeouts."""
    value = os.environ.get(env_name)
    if value not in (None, ""):
        return value
    value = _compile_cfg.get(yaml_name)
    if value not in (None, ""):
        return str(value)
    return default


COMPILE_API_TOKEN = _compile_setting("COMPILE_API_TOKEN", "compile_api_token")

# Compiles run in a DEDICATED terminal directory, never a broker terminal.
# MetaEditor is independent of terminal64.exe, so this is not about the
# compiler colliding with a running terminal - it is about not putting write
# traffic and a temp tree inside a directory a live terminal or a running
# tester owns. metaquotes/base is the natural pick: it ships MetaEditor64.exe
# and no instance runs a terminal out of it.
COMPILE_TERMINAL_DIR = _compile_setting(
    "COMPILE_TERMINAL_DIR", "compile_terminal_dir"
) or os.path.join(BROKERS_DIR, "metaquotes", "base")
COMPILE_METAEDITOR = os.path.join(COMPILE_TERMINAL_DIR, "MetaEditor64.exe")

# Passed to MetaEditor as /inc:. This is the MQL5 directory (the PARENT of
# Include), because that is what /inc: expects - `#include <Foo.mqh>` resolves
# to <inc>/Include/Foo.mqh.
COMPILE_INCLUDE_DIR = _compile_setting(
    "COMPILE_INCLUDE_DIR", "compile_include_dir"
) or os.path.join(COMPILE_TERMINAL_DIR, "MQL5")

# Per-request temp directories live here, not in the Windows user temp, so a
# crashed process leaves its debris somewhere visible and prunable.
COMPILE_WORK_DIR = _compile_setting(
    "COMPILE_WORK_DIR", "compile_work_dir"
) or os.path.join(BASE_DIR, "logs", "compile-work")

# Optional local mirror of the compile toolchain.
#
# When the terminal directory sits on a network or host-shared mount, the cost
# of a compile is dominated by dragging MetaEditor across it, not by compiling:
# measured on a docker-hosted Windows VM with the terminals on a 9p share, a
# compile MetaEditor itself timed at 4.1s took 29s wall-clock, every time --
# the page cache does not save you.
#
# Point this at a path on the VM's own disk and the toolchain (MetaEditor, its
# Config, and the include tree) is mirrored there once per process, then
# compiled from local disk. Empty = disabled, which is the right default for
# any install whose terminals are already local.
COMPILE_LOCAL_CACHE = _compile_setting("COMPILE_LOCAL_CACHE", "compile_local_cache")

# Comma-separated globs naming headers whose INDIVIDUAL digests are reported
# alongside the whole-tree include_hash, matched against paths relative to the
# include root (e.g. "MyLib*.mqh, Trade/Trade.mqh").
#
# The tree hash alone tells a caller that SOMETHING under /inc: moved, which is
# all that is needed to detect drift. It cannot say what: an upgrade of the
# stock MQL5 library looks identical to an edit of the caller's own shared
# header, and those have opposite correct responses - the first needs no
# rebuild at all, the second needs every dependent artifact rebuilt. Naming the
# few headers a caller actually owns lets them tell those apart.
#
# Empty = omit the field entirely. Deliberately not defaulted to the whole
# tree: ~260 digests per response is a payload, not an answer.
COMPILE_INCLUDE_DIGESTS = _compile_setting(
    "COMPILE_INCLUDE_DIGESTS", "compile_include_digests"
)

# Default 30s, hard ceiling 60s. MetaEditor compiles are seconds; anything
# approaching the ceiling means something is wrong, and holding the connection
# longer does not help the caller.
COMPILE_TIMEOUT_CEILING_SECONDS = 60
COMPILE_TIMEOUT_SECONDS = max(
    1,
    min(
        COMPILE_TIMEOUT_CEILING_SECONDS,
        parse_duration_to_seconds(_compile_setting("COMPILE_TIMEOUT", "compile_timeout", "30s")) or 30,
    ),
)


def _compile_byte_limit(env_name, yaml_key, default):
    """A positive per-request byte cap, clamped rather than raised.

    Same call as the other numeric settings in this module: config.py is
    imported by the whole API, so a typo in an optional endpoint's tuning
    value must not stop trading and backtesting. The floor keeps a bad value
    from silently disabling /compile outright.
    """
    raw = _compile_setting(env_name, yaml_key, str(default))
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(1024, value)


# Per-request caps for /compile, so one authenticated request cannot consume
# unbounded disk (the source is written to a temp dir), memory (the .ex5 is
# read back whole), or response bandwidth (it is base64-encoded into the JSON
# body). Checked before the write and before the read respectively - see
# handlers/compile.py. Documented in docs/compiling.md.
COMPILE_MAX_SOURCE_BYTES = _compile_byte_limit(
    "COMPILE_MAX_SOURCE_BYTES", "compile_max_source_bytes", 2 * 1024 * 1024
)
COMPILE_MAX_EX5_BYTES = _compile_byte_limit(
    "COMPILE_MAX_EX5_BYTES", "compile_max_ex5_bytes", 16 * 1024 * 1024
)
UTC_OFFSET_RAW = _args.utc_offset if _args.utc_offset is not None else os.environ.get("UTC_OFFSET", "")
UTC_OFFSET_SECONDS = parse_duration_to_seconds(UTC_OFFSET_RAW)
UTC_OFFSET_HOURS = UTC_OFFSET_SECONDS / 3600.0
_BACKTEST_TIMEOUT_ENV = os.environ.get("BACKTEST_TIMEOUT")
_BACKTEST_TIMEOUT_CONFIG = load_yaml_config().get("backtest_timeout")
BACKTEST_TIMEOUT_RAW = (
    _BACKTEST_TIMEOUT_ENV
    if _BACKTEST_TIMEOUT_ENV not in (None, "")
    else (_BACKTEST_TIMEOUT_CONFIG if _BACKTEST_TIMEOUT_CONFIG not in (None, "") else DEFAULT_BACKTEST_TIMEOUT)
)
BACKTEST_TIMEOUT = BACKTEST_TIMEOUT_RAW
BACKTEST_TIMEOUT_SECONDS = parse_duration_to_seconds(BACKTEST_TIMEOUT)

# Upper bound on the per-request 'timeout' form field. Without a cap, a
# caller-supplied timeout (e.g. a typo like '999999h') holds RUN_LOCK for
# that entire duration, blocking every other backtest against this terminal.
_BACKTEST_MAX_TIMEOUT_ENV = os.environ.get("BACKTEST_MAX_TIMEOUT")
BACKTEST_MAX_TIMEOUT_SECONDS = parse_duration_to_seconds(
    _BACKTEST_MAX_TIMEOUT_ENV if _BACKTEST_MAX_TIMEOUT_ENV not in (None, "") else "48h"
)

# Startup backtest-cleanup windows. sweep_orphans() only inspects state files
# touched within the lookback: a live job rewrites its file on every state
# transition and a run is bounded by its timeout, so anything older than the
# window is dead history and needs no sweep. prune_old_jobs() retires terminal
# (completed/failed) jobs older than the retention window so the shared
# backtest-jobs dir — every backtest API on a VM points at it — cannot grow
# without bound and make every boot's directory scan slower.
_SWEEP_LOOKBACK_ENV = os.environ.get("BACKTEST_SWEEP_LOOKBACK")
BACKTEST_SWEEP_LOOKBACK_SECONDS = parse_duration_to_seconds(
    _SWEEP_LOOKBACK_ENV if _SWEEP_LOOKBACK_ENV not in (None, "") else "24h"
)
_JOB_RETENTION_ENV = os.environ.get("BACKTEST_JOB_RETENTION")
BACKTEST_JOB_RETENTION_SECONDS = parse_duration_to_seconds(
    _JOB_RETENTION_ENV if _JOB_RETENTION_ENV not in (None, "") else "30d"
)
_MODE_RAW = (_args.mode or _terminal_config.get("mode") or os.environ.get("MT5_MODE") or "live")
MODE = str(_MODE_RAW).strip().lower() or "live"
if MODE not in ("live", "backtest"):
    MODE = "live"
SYMBOL_SUFFIX_CONFIGURED = "symbol_suffix" in _terminal_config
_SYMBOL_SUFFIX_RAW = _terminal_config.get("symbol_suffix")
SYMBOL_SUFFIX = "" if _SYMBOL_SUFFIX_RAW is None else str(_SYMBOL_SUFFIX_RAW)

# Wickworks TA sidecar — reachable only from the mt5 container's net namespace
# (compose: network_mode: "service:mt5", no published ports). From inside the
# Windows VM, the dockurr/windows gateway address 20.20.20.1 routes to the
# shared netns where wickworks binds 0.0.0.0:8000.
_wickworks_cfg = (load_yaml_config().get("wickworks") or {})
WICKWORKS_URL = (
    os.environ.get("WICKWORKS_URL")
    or _wickworks_cfg.get("url")
    or "http://20.20.20.1:8000/"
)
WICKWORKS_TIMEOUT_SECONDS = parse_duration_to_seconds(
    os.environ.get("WICKWORKS_TIMEOUT") or _wickworks_cfg.get("timeout") or "30s"
) or 30

# Resolve TERMINAL_PATH: account-specific copy first, then base install
_candidates = terminal_dir_candidates(BROKERS_DIR, BROKER, ACCOUNT, INSTANCE)

TERMINAL_PATH = _candidates[0]
for _c in _candidates:
    if os.path.exists(_c):
        TERMINAL_PATH = _c
        break

TERMINAL_DIR = os.path.dirname(TERMINAL_PATH)
INI_FILE = os.path.join(TERMINAL_DIR, "mt5start.ini")
IDENTITY = make_identity(BROKER, ACCOUNT, INSTANCE)
LOG_DIR = os.path.join(BASE_DIR, "logs")
FULL_LOG = os.path.join(LOG_DIR, "full.log")
BACKTEST_JOB_DIR = os.path.join(LOG_DIR, "backtest-jobs")

TIMEFRAME_MAP = {
    "M1": mt5.TIMEFRAME_M1,
    "M2": mt5.TIMEFRAME_M2,
    "M3": mt5.TIMEFRAME_M3,
    "M4": mt5.TIMEFRAME_M4,
    "M5": mt5.TIMEFRAME_M5,
    "M6": mt5.TIMEFRAME_M6,
    "M10": mt5.TIMEFRAME_M10,
    "M12": mt5.TIMEFRAME_M12,
    "M15": mt5.TIMEFRAME_M15,
    "M20": mt5.TIMEFRAME_M20,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
    "H2": mt5.TIMEFRAME_H2,
    "H3": mt5.TIMEFRAME_H3,
    "H4": mt5.TIMEFRAME_H4,
    "H6": mt5.TIMEFRAME_H6,
    "H8": mt5.TIMEFRAME_H8,
    "H12": mt5.TIMEFRAME_H12,
    "D1": mt5.TIMEFRAME_D1,
    "W1": mt5.TIMEFRAME_W1,
    "MN1": mt5.TIMEFRAME_MN1,
}

TIMEFRAME_SECONDS = {
    "M1": 60, "M2": 120, "M3": 180, "M4": 240, "M5": 300,
    "M6": 360, "M10": 600, "M12": 720, "M15": 900, "M20": 1200,
    "M30": 1800, "H1": 3600, "H2": 7200, "H3": 10800, "H4": 14400,
    "H6": 21600, "H8": 28800, "H12": 43200, "D1": 86400,
    "W1": 604800, "MN1": 2592000,
}

ORDER_TYPE_MAP = {
    "BUY": mt5.ORDER_TYPE_BUY,
    "SELL": mt5.ORDER_TYPE_SELL,
    "BUY_LIMIT": mt5.ORDER_TYPE_BUY_LIMIT,
    "SELL_LIMIT": mt5.ORDER_TYPE_SELL_LIMIT,
    "BUY_STOP": mt5.ORDER_TYPE_BUY_STOP,
    "SELL_STOP": mt5.ORDER_TYPE_SELL_STOP,
    "BUY_STOP_LIMIT": mt5.ORDER_TYPE_BUY_STOP_LIMIT,
    "SELL_STOP_LIMIT": mt5.ORDER_TYPE_SELL_STOP_LIMIT,
}

FILLING_MAP = {
    "FOK": mt5.ORDER_FILLING_FOK,
    "IOC": mt5.ORDER_FILLING_IOC,
    "RETURN": mt5.ORDER_FILLING_RETURN,
}

TIME_MAP = {
    "GTC": mt5.ORDER_TIME_GTC,
    "DAY": mt5.ORDER_TIME_DAY,
    "SPECIFIED": mt5.ORDER_TIME_SPECIFIED,
    "SPECIFIED_DAY": mt5.ORDER_TIME_SPECIFIED_DAY,
}
