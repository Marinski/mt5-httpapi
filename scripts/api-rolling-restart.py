"""Restart every backtest-mode mt5api process on this VM, one at a time, and
only while its terminal is idle, so new mt5api code loads without killing a
running backtest. api_runner.bat relaunches a process ~10 s after it exits.

Idle = no terminal64.exe/metatester64.exe under the instance directory (by
either path spelling: Desktop\\Shared is a link to \\\\host.lan\\Data) on two
checks 15 s apart. Gives up after MAX_HOURS. Logs to
logs\\api-rolling-restart-<ts>.log. Never logs command lines (they carry the token).
"""
import datetime, os, sys, time
import psutil

SHARED = r"C:\Users\Docker\Desktop\Shared"
MAX_HOURS = 6
T0 = time.time()
LOG = os.path.join(SHARED, "logs", "api-rolling-restart-%s.log" % datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write("[%s] %s\n" % (datetime.datetime.now().strftime("%H:%M:%S"), msg))


def norm(p):
    return (p or "").replace("\\", "/").lower().rstrip("/")


ROOTS = {norm(SHARED), norm(os.path.realpath(SHARED))}


def arg(cmd, name):
    try:
        return cmd[cmd.index(name) + 1]
    except (ValueError, IndexError):
        return None


def backtest_apis():
    """{pid: (id, instance_dir_rel)} for mt5api processes in backtest mode started before T0."""
    found = {}
    for p in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            if (p.info["name"] or "").lower() != "python.exe":
                continue
            cmd = p.info["cmdline"] or []
            if "mt5api" not in cmd or arg(cmd, "--mode") != "backtest":
                continue
            if (p.info["create_time"] or 0) >= T0:
                continue
            b, a, i = arg(cmd, "--broker"), arg(cmd, "--account"), arg(cmd, "--instance") or "default"
            rel = "terminals/%s/%s/%s" % (b, a, i)
            if i == "default" and not os.path.isdir(os.path.join(SHARED, "terminals", b, a, "default")):
                rel = "terminals/%s/%s" % (b, a)
            found[p.info["pid"]] = ("%s/%s/%s" % (b, a, i), rel.lower())
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return found


def tester_dirs():
    """Normalized, root-relative directories of every running tester process."""
    dirs = []
    for p in psutil.process_iter(["name", "exe"]):
        try:
            if (p.info["name"] or "").lower() not in ("terminal64.exe", "metatester64.exe"):
                continue
            d = norm(os.path.dirname(p.info["exe"] or ""))
            for root in ROOTS:
                if d == root or d.startswith(root + "/"):
                    dirs.append(d[len(root) + 1:])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return dirs


def busy(rel, dirs):
    # Account-level layout (instance "default" without its own dir): only an
    # exact match, so sibling instance dirs under the account never count.
    exact = rel.count("/") == 2
    return any(d == rel or (not exact and d.startswith(rel + "/")) for d in dirs)


DRY = "dry" in sys.argv[1:]
log("start%s; roots=%s" % (" (dry run)" if DRY else "", sorted(ROOTS)))
if DRY:
    dirs = tester_dirs()
    for pid, (ident, rel) in sorted(backtest_apis().items(), key=lambda kv: kv[1][0]):
        log("  %-40s pid=%-6d %s  dir=%s" % (ident, pid, "BUSY" if busy(rel, dirs) else "idle", rel))
    log("  tester dirs: %s" % sorted(set(dirs)))
    sys.exit(0)
done = 0
while time.time() - T0 < MAX_HOURS * 3600:
    apis = backtest_apis()
    if not apis:
        break
    idle = [pid for pid, (_, rel) in apis.items() if not busy(rel, tester_dirs())]
    restarted = False
    for pid in idle:
        time.sleep(15)
        ident, rel = apis[pid]
        if busy(rel, tester_dirs()):
            continue
        try:
            psutil.Process(pid).kill()
            done += 1
            log("restarted %s (pid %d); %d left" % (ident, pid, len(apis) - 1))
            restarted = True
            time.sleep(20)
            break
        except psutil.NoSuchProcess:
            continue
    if not restarted:
        time.sleep(30)
left = backtest_apis()
log("finished: %d restarted, %d not restarted%s" % (done, len(left), (": " + ", ".join(v[0] for v in left.values())) if left else ""))
