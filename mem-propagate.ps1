# mem-propagate.ps1 - distribute mem.py from THIS repo to consumer copies.
# Source of truth: this clone (git: dosermen/mem). Consumers keep a plain
# copy at their own path and never see git.
#   default     copy + sha256 verify, per-line report, append to log
#   -Check      report only, touch nothing (exit 1 on drift)
# targets: propagate.txt, one destination FILE per line, # = comment
param([switch]$Check)

$ErrorActionPreference = 'Stop'
$src     = Join-Path $PSScriptRoot "mem.py"
$targets = Join-Path $PSScriptRoot "propagate.txt"
$log     = join-path $PSScriptRoot "mem-propagate.log"

function Log($m) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m"
    if (-not $Check) { Add-Content -Path $log -Value $line -Encoding UTF8 }
    Write-Output $line
}

$srcHash = (Get-FileHash $src -Algorithm SHA256).Hash
$drift = 0
$done  = 0
Get-Content $targets | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith("#")) { return }
    if (-not (Test-Path $line)) {
        if ($Check) { Log "MISSING  $line"; $script:drift++ }
        else {
            $dir = Split-Path $line -Parent
            if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
            Copy-Item $src $line -Force
            $h = (Get-FileHash $line -Algorithm SHA256).Hash
            if ($h -eq $srcHash) { Log "BOOTSTRAP $line (sha256 OK)"; $script:done++ }
            else { Log "FAIL     $line (hash mismatch after copy)"; $script:drift++ }
        }
        return
    }
    $dstHash = (Get-FileHash $line -Algorithm SHA256).Hash
    if ($dstHash -eq $srcHash) { Log "OK       $line"; return }
    if ($Check) { Log "DRIFT    $line"; $script:drift++; return }
    Copy-Item $src $line -Force
    $h = (Get-FileHash $line -Algorithm SHA256).Hash
    if ($h -eq $srcHash) { Log "UPDATED  $line (sha256 OK)"; $script:done++ }
    else { Log "FAIL     $line (hash mismatch after copy)"; $script:drift++ }
}

if ($Check) {
    if ($drift) { Log "check: $drift target(s) out of sync"; exit 1 }
    Log "check: all targets in sync"
    exit 0
}
Log "propagate: $done updated, $drift failed"
if ($drift) { exit 1 }
exit 0
