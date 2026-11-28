# mem-propagate.ps1 - distribute repo files to consumer copies.
# Source of truth: this clone (git: dosermen/mem). Consumers keep plain
# copies at their own paths and never see git.
#   default     copy + sha256 verify, per-line report, append to log
#   -Check      report only, touch nothing (exit 1 on drift)
# targets: propagate.txt, one destination per line, # = comment:
#   plain line       -> source is mem.py (backward compatible)
#   <src>::<dst>     -> source is <src> relative to this repo (28.11: the
#                       opencode session exporter + sweep wrapper ride along)
param([switch]$Check)

$ErrorActionPreference = 'Stop'
$memSrc  = Join-Path $PSScriptRoot "mem.py"
$targets = Join-Path $PSScriptRoot "propagate.txt"
$log     = Join-Path $PSScriptRoot "mem-propagate.log"

function Log($m) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m"
    if (-not $Check) { Add-Content -Path $log -Value $line -Encoding UTF8 }
    Write-Output $line
}

$hashCache = @{}
function SrcHash($path) {
    if (-not $hashCache.ContainsKey($path)) {
        $hashCache[$path] = (Get-FileHash $path -Algorithm SHA256).Hash
    }
    return $hashCache[$path]
}

$drift = 0
$done  = 0
Get-Content $targets | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith("#")) { return }
    # split "src::dst" (IndexOf, not Split: PS 5.1 = .NET Framework)
    $srcPath = $memSrc
    $dst = $line
    $idx = $line.IndexOf("::")
    if ($idx -ge 0) {
        $srcPath = Join-Path $PSScriptRoot $line.Substring(0, $idx).Trim()
        $dst = $line.Substring($idx + 2).Trim()
    }
    if (-not (Test-Path $srcPath)) {
        Log "MISSING SRC  $srcPath"
        $script:drift++
        return
    }
    $srcHash = SrcHash $srcPath
    if (-not (Test-Path $dst)) {
        if ($Check) { Log "MISSING  $dst"; $script:drift++ }
        else {
            $dir = Split-Path $dst -Parent
            if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
            Copy-Item $srcPath $dst -Force
            $h = (Get-FileHash $dst -Algorithm SHA256).Hash
            if ($h -eq $srcHash) { Log "BOOTSTRAP $dst (sha256 OK)"; $script:done++ }
            else { Log "FAIL     $dst (hash mismatch after copy)"; $script:drift++ }
        }
        return
    }
    $dstHash = (Get-FileHash $dst -Algorithm SHA256).Hash
    if ($dstHash -eq $srcHash) { Log "OK       $dst"; return }
    if ($Check) { Log "DRIFT    $dst"; $script:drift++; return }
    Copy-Item $srcPath $dst -Force
    $h = (Get-FileHash $dst -Algorithm SHA256).Hash
    if ($h -eq $srcHash) { Log "UPDATED  $dst (sha256 OK)"; $script:done++ }
    else { Log "FAIL     $dst (hash mismatch after copy)"; $script:drift++ }
}

if ($Check) {
    if ($drift) { Log "check: $drift target(s) out of sync"; exit 1 }
    Log "check: all targets in sync"
    exit 0
}
Log "propagate: $done updated, $drift failed"
if ($drift) { exit 1 }
exit 0
