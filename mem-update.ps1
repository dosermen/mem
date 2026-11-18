# mem-update.ps1 - scheduled auto-update: pull this repo, then propagate.
# Safety gates:
#   - dirty working tree  -> SKIP (uncommitted edits are never overwritten)
#   - no upstream / offline -> SKIP, logged
#   - only what was pushed to dosermen/mem reaches consumers
# runs daily via task Mem-Update (wscript run-hidden.vbs, AGENTS rule 18.11)
$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
$log  = Join-Path $repo "mem-propagate.log"

function Log($m) {
    Add-Content -Path $log -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m" -Encoding UTF8
}

# 1. fetch (network)
git -C $repo fetch origin 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Log "update: fetch failed (offline?) - skip"; exit 1 }

# 2. dirty tree gate
$status = git -C $repo status --porcelain
if ($status) { Log "update: working tree dirty - SKIP pull (commit first)"; exit 1 }

# 3. behind?
$behind = git -C $repo rev-list --count "HEAD..origin/main"
if (-not $behind -or $behind -eq "0") { Log "update: already up to date (HEAD=$(git -C $repo rev-parse --short HEAD))"; exit 0 }

# 4. fast-forward pull
git -C $repo merge --ff-only origin/main 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Log "update: ff-only merge failed - skip"; exit 1 }
Log "update: pulled $(git -C $repo rev-parse --short HEAD)"

# 5. distribute
& (Join-Path $repo "mem-propagate.ps1")
exit $LASTEXITCODE
