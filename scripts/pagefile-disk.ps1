# Prepare the VM's pagefile disk: dockur's second disk (DISK2_SIZE, /storage2),
# an image on the host's /data drive rather than the root SSD that holds the
# system disk. Formats it as P: (label mt5-pagefile) the first time it is seen,
# then only checks it. pagefile.py puts the large pagefile there when P: exists.
#
# Touches only a disk that is RAW (no partition table), not boot and not
# system, and only when exactly one such disk is attached. Prints one line for
# start.log. Exit 0 = ready or no such disk attached, 1 = could not set it up.
$ErrorActionPreference = 'Stop'
$label = 'mt5-pagefile'
$letter = 'P'
try {
    $vol = Get-Volume -FileSystemLabel $label -ErrorAction SilentlyContinue
    if ($vol) {
        if ($vol.DriveLetter -ne $letter) {
            Get-Partition -Volume $vol | Set-Partition -NewDriveLetter $letter
        }
        "pagefile disk ok (${letter}:)"
        exit 0
    }
    $raw = @(Get-Disk | Where-Object { $_.PartitionStyle -eq 'RAW' -and -not $_.IsBoot -and -not $_.IsSystem })
    if ($raw.Count -eq 0) {
        'no pagefile disk attached'
        exit 0
    }
    if ($raw.Count -gt 1) {
        "found $($raw.Count) blank disks; not guessing which one is the pagefile disk"
        exit 1
    }
    if (Get-PSDrive -Name $letter -ErrorAction SilentlyContinue) {
        "drive ${letter}: is already in use; pagefile disk left unformatted"
        exit 1
    }
    $disk = $raw[0]
    if ($disk.IsOffline) { Set-Disk -Number $disk.Number -IsOffline $false }
    if ($disk.IsReadOnly) { Set-Disk -Number $disk.Number -IsReadOnly $false }
    Initialize-Disk -Number $disk.Number -PartitionStyle GPT
    $part = New-Partition -DiskNumber $disk.Number -UseMaximumSize -DriveLetter $letter
    Format-Volume -Partition $part -FileSystem NTFS -NewFileSystemLabel $label -Confirm:$false | Out-Null
    "pagefile disk $($disk.Number) formatted as ${letter}: ($([math]::Round($disk.Size / 1GB)) GB)"
    exit 0
} catch {
    "pagefile disk setup failed: $_"
    exit 1
}
