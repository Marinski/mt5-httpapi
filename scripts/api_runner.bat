@echo off
:: api_runner.bat  broker  account  instance  port  token  utc_offset  mode
:: Wraps the Python API process so start/exit/exitcode are logged.
setlocal enabledelayedexpansion
set "AR_BROKER=%~1"
set "AR_ACCOUNT=%~2"
set "AR_INSTANCE=%~3"
set "AR_PORT=%~4"
set "AR_TOKEN=%~5"
set "AR_OFFSET=%~6"
set "AR_MODE=%~7"
if "!AR_INSTANCE!"=="" set "AR_INSTANCE=default"
if "!AR_OFFSET!"=="" set "AR_OFFSET=0"
if "!AR_MODE!"=="" set "AR_MODE=live"
set "SHARED=C:\Users\Docker\Desktop\Shared"
set "LOGDIR=%SHARED%\logs"
set "AR_LOG=%LOGDIR%\api-!AR_BROKER!-!AR_ACCOUNT!-!AR_INSTANCE!.log"
set "FULL_LOG=%LOGDIR%\full.log"
set "PYDIR=C:\Program Files\Python312"
:: One thread for numpy's BLAS. Unset, OpenBLAS starts a worker and sets
:: aside a buffer for every CPU of the VM at `import numpy`, in every API
:: process: ~780 MB committed per idle process on a 40-vCPU VM, 41 GB for 53
:: terminals, against a commit limit that tester agents also need. The API
:: never does heavy linear algebra, so one thread costs nothing.
set "OPENBLAS_NUM_THREADS=1"
set "OMP_NUM_THREADS=1"
set "MKL_NUM_THREADS=1"

mkdir "%LOGDIR%" 2>nul

echo [%DATE% %TIME%] [api:!AR_BROKER!/!AR_ACCOUNT!/!AR_INSTANCE!] === PROCESS STARTED on port !AR_PORT! (utc_offset=!AR_OFFSET! mode=!AR_MODE!) === >> "!AR_LOG!"
echo [%DATE% %TIME%] [start] [api:!AR_BROKER!/!AR_ACCOUNT!/!AR_INSTANCE!] PROCESS STARTED on port !AR_PORT! utc_offset=!AR_OFFSET! mode=!AR_MODE! >> "%FULL_LOG%"

cd /d "%SHARED%"
"%PYDIR%\python.exe" -m mt5api --broker !AR_BROKER! --account !AR_ACCOUNT! --instance !AR_INSTANCE! --port !AR_PORT! --token "!AR_TOKEN!" --utc-offset "!AR_OFFSET!" --mode "!AR_MODE!" >> "!AR_LOG!" 2>&1
set "AR_EC=!ERRORLEVEL!"

echo [%DATE% %TIME%] [api:!AR_BROKER!/!AR_ACCOUNT!/!AR_INSTANCE!] === PROCESS EXITED exit_code=!AR_EC! === >> "!AR_LOG!"
echo [%DATE% %TIME%] [start] [api:!AR_BROKER!/!AR_ACCOUNT!/!AR_INSTANCE!] PROCESS EXITED exit_code=!AR_EC! >> "%FULL_LOG%"
endlocal
