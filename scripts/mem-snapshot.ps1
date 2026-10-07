# mem-snapshot.ps1 -- one-shot guest memory breakdown, written to Shared\logs.
# Launched by check_health.py when Shared\logs\mem-snapshot.request exists
# (each VM has its own logs dir, so the request targets one VM).
# Read-only: it only queries counters and the process list.
$ErrorActionPreference = 'Continue'
$logs = 'C:\Users\Docker\Desktop\Shared\logs'
$out  = Join-Path $logs ('mem-snapshot-{0}.txt' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
$tmp  = "$out.partial"

function GB($b) { '{0,8:N2} GB' -f ($b / 1GB) }

& {
    "== mem-snapshot $(Get-Date -Format s)  host=$env:COMPUTERNAME"
    $os = Get-CimInstance Win32_OperatingSystem
    "uptime since $($os.LastBootUpTime)"
    ''
    '== Physical memory (guest view)'
    $total = [double]$os.TotalVisibleMemorySize * 1KB
    $free  = [double]$os.FreePhysicalMemory * 1KB
    "Total visible        $(GB $total)"
    "Available (free+sb)  $(GB $free)"
    ''
    '== Memory counters'
    $m = Get-CimInstance Win32_PerfRawData_PerfOS_Memory
    "In use (approx)      $(GB ($total - [double]$m.AvailableBytes))"
    "Available            $(GB $m.AvailableBytes)"
    "  Free & zero        $(GB ([double]$m.FreeAndZeroPageListBytes))"
    "  Standby (cache)    $(GB ([double]$m.StandbyCacheCoreBytes + [double]$m.StandbyCacheNormalPriorityBytes + [double]$m.StandbyCacheReserveBytes))"
    "    normal prio      $(GB $m.StandbyCacheNormalPriorityBytes)"
    "    reserve          $(GB $m.StandbyCacheReserveBytes)"
    "    core             $(GB $m.StandbyCacheCoreBytes)"
    "Modified (dirty)     $(GB $m.ModifiedPageListBytes)"
    "System cache (ws)    $(GB $m.CacheBytes)"
    "Pool paged           $(GB $m.PoolPagedBytes)"
    "Pool nonpaged        $(GB $m.PoolNonpagedBytes)"
    "Committed            $(GB $m.CommittedBytes)  / limit $(GB $m.CommitLimit)"
    ''
    '== Processes grouped by name (private working set = RAM the process alone holds)'
    $procs = Get-CimInstance Win32_PerfRawData_PerfProc_Process |
        Where-Object { $_.Name -notin '_Total', 'Idle' }
    $procs | ForEach-Object {
        [pscustomobject]@{ Name = ($_.Name -replace '#\d+$', ''); WSP = [double]$_.WorkingSetPrivate; WS = [double]$_.WorkingSet; PB = [double]$_.PrivateBytes }
    } | Group-Object Name | ForEach-Object {
        [pscustomobject]@{
            Name       = $_.Name
            Count      = $_.Count
            PrivWS_GB  = [math]::Round(($_.Group | Measure-Object WSP -Sum).Sum / 1GB, 2)
            WS_GB      = [math]::Round(($_.Group | Measure-Object WS  -Sum).Sum / 1GB, 2)
            Commit_GB  = [math]::Round(($_.Group | Measure-Object PB  -Sum).Sum / 1GB, 2)
        }
    } | Sort-Object PrivWS_GB -Descending | Select-Object -First 30 | Format-Table -AutoSize | Out-String -Width 200
    $sumP = ($procs | Measure-Object WorkingSetPrivate -Sum).Sum
    "Sum of all private working sets: $(GB $sumP)"
    ''
    '== Top 30 individual processes by private working set'
    $cmd = @{}
    Get-CimInstance Win32_Process | ForEach-Object { $cmd[[int]$_.ProcessId] = $_.CommandLine }
    $procs | Sort-Object { [double]$_.WorkingSetPrivate } -Descending | Select-Object -First 30 | ForEach-Object {
        $c = $cmd[[int]$_.IDProcess]; if ($c -and $c.Length -gt 160) { $c = $c.Substring(0, 160) }
        # never write the API token to the log
        if ($c) { $c = $c -replace '--token\s+"?[^"\s]+"?', '--token ***' }
        '{0,7} {1,-22} privWS {2}  commit {3}  {4}' -f $_.IDProcess, $_.Name, (GB $_.WorkingSetPrivate), (GB $_.PrivateBytes), $c
    }
} *>&1 | Out-File -FilePath $tmp -Encoding utf8 -Width 220

Move-Item -Force $tmp $out
