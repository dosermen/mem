# mem-sweep.ps1: scheduled wrapper for `python mem.py sweep` (sweep 17.11.2026).
# Runs every 10 min via task Mem-Sweep; the gate lives HERE, inside the
# script: 9:00-24:00 only. The phone (embedding server) is switched off by
# Android settings at 00:00 - outside the window this exits silently with
# no log noise and no network calls.
# Everything the sweep needs is one lock (MEMORY.md.embed.lock): a run that
# overlaps the previous one just prints "skipping" and exits rc=0.
$hour = (Get-Date).Hour
if ($hour -lt 9) { exit 0 }        # Hour is 0..23 => window 9..23 = 9:00-24:00

# universal root: this wrapper lives in <root>\tools, so the project root is
# its parent - drop-in for any consumer (OLAMA, MEM, ...), no hardcoding
$root = Split-Path $PSScriptRoot -Parent
$memPy = Join-Path $root "tools\mem.py"
$log = Join-Path $root "logs\mem-sweep.log"

# keep python output readable in the log (paths/errors may be non-ASCII)
try { $OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$env:PYTHONIOENCODING = "utf-8"

# rotate: one old copy, cap ~1 MB (90 runs/day would grow it forever)
if ((Test-Path $log) -and ((Get-Item $log).Length -gt 1MB)) {
    $old = "$log.1"
    Remove-Item $old -ErrorAction SilentlyContinue
    Move-Item $log $old -ErrorAction SilentlyContinue
}

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

# session digests -> logs\sessions\*.md (probe verdict 100@v1: keyword half
# alone = +4/10 provenance hits; incremental - only changed files are written,
# so sweep's mtime-driven embed picks up just the delta). Failure must never
# block the sweep itself.
$exportPy = Join-Path $root "tools\export-opencode-sessions.py"
$expOut = (& python $exportPy 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) {
    Add-Content -Path $log -Value "[$stamp] export FAILED rc=$LASTEXITCODE" -Encoding UTF8
    if ($expOut) { Add-Content -Path $log -Value $expOut -Encoding UTF8 }
} elseif ($expOut -notmatch 'written 0,') {
    Add-Content -Path $log -Value "[$stamp] $expOut" -Encoding UTF8
}

$out = (& python $memPy sweep 2>&1 | Out-String).Trim()
$rc = $LASTEXITCODE
Add-Content -Path $log -Value "[$stamp] rc=$rc" -Encoding UTF8
if ($out) { Add-Content -Path $log -Value $out -Encoding UTF8 }
exit $rc
