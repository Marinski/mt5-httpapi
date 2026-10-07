"""Read-only: list MT5 tester processes and whether mt5api's directory match
(_in_terminal_dir, backtest/handler.py) recognises them. Writes
logs\proc-snapshot-<ts>.txt. Touches nothing."""
import datetime, os, psutil

SHARED = r"C:\Users\Docker\Desktop\Shared"
out = os.path.join(SHARED, "logs", "proc-snapshot-%s.txt" % datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))

def norm(p):
    return (p or "").replace("\\", "/").lower().rstrip("/")

lines = []
for proc in psutil.process_iter(["pid", "name", "exe", "cmdline", "cwd", "create_time"]):
    name = (proc.info.get("name") or "").lower()
    if name not in ("terminal64.exe", "metatester64.exe"):
        continue
    exe = proc.info.get("exe") or ""
    try:
        cwd = proc.cwd()
    except Exception as e:
        cwd = "?" + type(e).__name__
    cmd = " ".join(proc.info.get("cmdline") or [])
    # The directory mt5api would compare against, derived from the command line / exe.
    tdir = os.path.dirname(exe)
    expected = os.path.join(SHARED, os.path.relpath(tdir, SHARED)) if tdir.lower().startswith(SHARED.lower()) else "(exe not under SHARED)"
    lines.append("pid=%s name=%s started=%s\n  exe=%r\n  cwd=%r\n  cmd=%r\n  exe_under_SHARED=%s"
                 % (proc.info["pid"], name,
                    datetime.datetime.fromtimestamp(proc.info.get("create_time") or 0).strftime("%H:%M:%S"),
                    exe, cwd, cmd[:300], norm(exe).startswith(norm(SHARED) + "/")))
with open(out, "w", encoding="utf-8") as f:
    f.write("SHARED=%r realpath=%r\n" % (SHARED, os.path.realpath(SHARED)))
    f.write("\n".join(lines) or "(no tester processes)")
    f.write("\n")
