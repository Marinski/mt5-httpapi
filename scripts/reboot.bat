@echo off
setlocal enabledelayedexpansion
::
:: The ONLY reboot path for the whole stack. Every caller goes through here.
::
:: WHY THIS EXISTS
:: ---------------
:: The MT5AutoReboot scheduled task used to run `shutdown /r /t 0 /f`
:: directly. That skipped both halves of the reboot protocol:
::
::   1. It never wrote rebooting.flag, which install.bat consumes on the
::      next boot to clear its own stale lock.
::   2. It gave zero grace period, so it killed start.bat mid-run while
::      start.bat still held %SHARED%\start.running.
::
:: Because %SHARED% is the host-mounted volume, that orphaned lock survived
:: the reboot and every subsequent boot bailed on it -- a permanent outage
:: that only a human running run.sh could clear.
::
:: Routing every reboot through this one script means the flag is always
:: written and the locks are always released, no matter who asks to reboot.
::
:: Usage: reboot.bat [reason]
::
set "SHARED=C:\Users\Docker\Desktop\Shared"
set "LOGDIR=%SHARED%\logs"
set "FULL_LOG=%LOGDIR%\full.log"
set "REBOOT_FLAG=%SHARED%\rebooting.flag"
set "VM_GROUP=%SHARED%\config\vm-group.txt"
:: Provisional -- both are scoped per VM further down, once the log exists to
:: record which names were chosen.
set "START_LOCK=%SHARED%\start.running"
set "INSTALL_LOCK=%SHARED%\install.running"
:: Grace period so the log write and the lock removals land on the mounted
:: volume before the OS tears the filesystem down.
set "GRACE_SECONDS=5"

set "PYDIR=C:\Program Files\Python312"
set "SCRIPTS=%SHARED%\scripts"

set "REASON=%~1"
if "!REASON!"=="" set "REASON=unspecified"

mkdir "%LOGDIR%" 2>nul
echo [%date% %time%] [reboot] reboot requested (reason=!REASON!) >> "%FULL_LOG%"

:: A scheduled reboot waits until no API is running a backtest or a request
:: that changes something (orders, stop losses, deployments, file writes).
:: reboot_guard.py logs every wait with its reason and always returns, at the
:: latest after reboot_max_postpone minutes. The boot-time reboots
:: (pip-changed, install-requested) run before any API exists, so they skip it.
if /i "!REASON!"=="scheduled" (
    if exist "%SCRIPTS%\reboot_guard.py" (
        "%PYDIR%\python.exe" "%SCRIPTS%\reboot_guard.py"
    )
)
:: -- Scope the locks to this VM -------------------------------------
:: %SHARED% is the same host directory for every VM, so start.bat and
:: install.bat each scope their lock by vm-group.txt. Releasing the UNSCOPED
:: names here would release a lock nobody holds, silently turning the belt
:: below into a no-op; releasing every scoped name would be worse still,
:: tearing up a live lock held by another VM in the middle of its install.
set "LOCK_SCOPE="
set "VG_FIRST="
set "VG_COUNT=0"
if exist "%VM_GROUP%" (
    for /f "usebackq eol=# tokens=1-3 delims= " %%A in ("%VM_GROUP%") do (
        set /a VG_COUNT+=1
        if not defined VG_FIRST set "VG_FIRST=%%A_%%B_%%C"
    )
    if defined VG_FIRST set "LOCK_SCOPE=!VG_FIRST!_!VG_COUNT!"
)
if defined LOCK_SCOPE set "START_LOCK=%SHARED%\start.running.!LOCK_SCOPE!"
if defined LOCK_SCOPE set "INSTALL_LOCK=%SHARED%\install.running.!LOCK_SCOPE!"

:: Tell the next boot that any lock it finds belongs to a dead instance.
:: install.bat consumes this flag; start.bat does not, because it reaches the
:: same conclusion from its own boot stamp -- and consuming it here would
:: delete the flag before install.bat (which start.bat calls) ever saw it.
echo rebooting > "%REBOOT_FLAG%"

:: Belt AND braces: drop the locks now rather than relying only on the flag.
:: The boot stamp is a SIBLING file, so the lock dir is empty and a bare rmdir
:: would serve; /s /q is kept as defence against anything that ever drops a
:: file inside it, which a bare rmdir would then refuse to remove.
echo [%date% %time%] [reboot] releasing !START_LOCK! and !INSTALL_LOCK! >> "%FULL_LOG%"
rmdir /s /q "%START_LOCK%" 2>nul
rmdir /s /q "%INSTALL_LOCK%" 2>nul

echo [%date% %time%] [reboot] locks released, rebooting in !GRACE_SECONDS!s >> "%FULL_LOG%"
shutdown /r /t !GRACE_SECONDS! /f
exit /b 0
