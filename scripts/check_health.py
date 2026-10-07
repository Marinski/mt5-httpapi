"""
check_health.py -- called from the start.bat status loop every 60s.
Prints UP/DOWN for each configured terminal port.
Logs any DOWN ports to full.log with timestamp.
"""
import datetime
import os
import socket
import sys

try:
    import yaml
except ImportError:
    print('  ERROR: pyyaml not installed')
    raise SystemExit(1) from None

# Reuse config_helper's per-VM filter rather than re-deriving it. It sits in
# this same directory and guards its main() behind __name__, so importing it
# is side-effect free.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from config_helper import _in_group, _vm_group_filter
except ImportError:
    def _vm_group_filter():
        return None

    def _in_group(terminal, allowed):
        return True

    print('  WARN: config_helper import failed - per-VM filter disabled, probing ALL ports')

SHARED = r'C:\Users\Docker\Desktop\Shared'
CONFIG = os.path.join(SHARED, 'config', 'config.yaml')
FULL_LOG = os.path.join(SHARED, 'logs', 'full.log')

try:
    with open(CONFIG, encoding='utf-8') as f:
        cfg = yaml.safe_load(f) or {}
    terminals = cfg.get('terminals') or []
except Exception as e:
    print(f'  ERROR reading config.yaml: {e}')
    raise SystemExit(1) from e

now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
dead = []

# Only this VM's terminals. Without the filter each VM probes every port in
# config.yaml, including the ones another VM hosts, and reports them DOWN — so
# the container sits permanently "unhealthy" and a genuine failure is
# indistinguishable from the usual noise. Observed 2026-08-04: the fast VM was
# unhealthy with a 25,272-long failing streak, listing only bulk-VM ports,
# while all 12 of its own terminals were serving normally.
allowed = _vm_group_filter()
terminals = [t for t in terminals if _in_group(t, allowed)]

for t in terminals:
    port = t['port']
    broker = t['broker']
    account = t['account']
    try:
        s = socket.create_connection(('localhost', port), timeout=2)
        s.close()
        print(f'  UP    {broker}/{account} :{port}')
    except Exception as e:
        print(f'  DOWN  {broker}/{account} :{port}  ({e})')
        dead.append(t)

if dead:
    try:
        with open(FULL_LOG, 'a') as f:
            for t in dead:
                f.write(
                    f'[{now}] [monitor] PORT {t["port"]}'
                    f' ({t["broker"]}/{t["account"]}) IS DOWN\n'
                )
    except Exception:
        pass

# One-shot guest memory snapshot. `touch <this VM's logs dir>/mem-snapshot.request`
# on the host; the next tick removes it and launches mem-snapshot.ps1 detached,
# which writes logs\mem-snapshot-<ts>.txt. Never blocks or fails the health loop.
try:
    _req = os.path.join(SHARED, 'logs', 'mem-snapshot.request')
    if os.path.exists(_req):
        os.remove(_req)
        import subprocess
        _log = open(os.path.join(SHARED, 'logs', 'mem-snapshot.launch.log'), 'a')
        subprocess.Popen(
            ['powershell', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
             '-File', os.path.join(SHARED, 'scripts', 'mem-snapshot.ps1')],
            stdin=subprocess.DEVNULL, stdout=_log, stderr=subprocess.STDOUT,
            creationflags=0x08000000 | 0x00000200,  # CREATE_NO_WINDOW | NEW_PROCESS_GROUP
        )
        print('  mem-snapshot launched')
except Exception as e:
    print(f'  mem-snapshot hook failed: {e}')

# One-shot tester-process snapshot (read-only). `touch <logs>/proc-snapshot.request`.
try:
    _req = os.path.join(SHARED, 'logs', 'proc-snapshot.request')
    if os.path.exists(_req):
        os.remove(_req)
        import subprocess, sys
        _log = open(os.path.join(SHARED, 'logs', 'proc-snapshot.launch.log'), 'a')
        subprocess.Popen(
            [r'C:\Program Files\Python312\python.exe', os.path.join(SHARED, 'scripts', 'proc-snapshot.py')],
            stdin=subprocess.DEVNULL, stdout=_log, stderr=subprocess.STDOUT,
            creationflags=0x08000000 | 0x00000200,
        )
        print('  proc-snapshot launched')
except Exception as e:
    print(f'  proc-snapshot hook failed: {e}')

# Idle-only rolling restart of this VM's backtest APIs (loads new mt5api code).
# `echo run > <logs>/api-rolling-restart.request` (or `echo dry` to only report).
try:
    _req = os.path.join(SHARED, 'logs', 'api-rolling-restart.request')
    if os.path.exists(_req):
        with open(_req) as _f:
            _mode = _f.read().strip() or 'dry'
        os.remove(_req)
        import subprocess
        _log = open(os.path.join(SHARED, 'logs', 'api-rolling-restart.launch.log'), 'a')
        subprocess.Popen(
            [r'C:\Program Files\Python312\python.exe', os.path.join(SHARED, 'scripts', 'api-rolling-restart.py'), _mode],
            stdin=subprocess.DEVNULL, stdout=_log, stderr=subprocess.STDOUT,
            creationflags=0x08000000 | 0x00000200,
        )
        print(f'  api-rolling-restart launched ({_mode})')
except Exception as e:
    print(f'  api-rolling-restart hook failed: {e}')
