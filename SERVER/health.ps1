# Embedding-server health probe: GET /health + a tiny POST /v1/embeddings
# latency ping (the POST is what wakes a sleeping model - it is a probe,
# not a mutation; nothing else is written). Exit 0 = server answers, 1 = down.
$ErrorActionPreference = "Stop"

# Load SERVER_* overrides from config.bat next to this script - same source of
# truth as embed-server.bat / phone-forward.bat. Missing config = defaults.
# Both idioms are understood: `set VAR=val` and `set "VAR=val"` (the quoted
# form silently probed the wrong port before, round-2 audit).
$cfg = Join-Path $PSScriptRoot "config.bat"
if (Test-Path $cfg) {
    foreach ($line in Get-Content $cfg) {
        if ($line -match '^\s*set\s+(?:"([^=]+)=(.*)"|([^=]+)=(.*))\s*$') {
            $k = if ($Matches[1]) { $Matches[1] } else { $Matches[3] }
            $v = if ($Matches[1]) { $Matches[2] } else { $Matches[4] }
            if ($k -match '^SERVER_\w+$') {
                Set-Item -Path ("env:" + $k) -Value $v.Trim()
            }
        }
    }
}

$port = if ($env:SERVER_PORT) { $env:SERVER_PORT } elseif ($env:MEM_EMBED_PORT) { $env:MEM_EMBED_PORT } else { 5031 }
$model = if ($env:SERVER_MODEL_ALIAS) { $env:SERVER_MODEL_ALIAS } elseif ($env:MEM_EMBED_MODEL) { $env:MEM_EMBED_MODEL } else { "Qwen3-Embedding-0.6B" }
try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:$port/health" -TimeoutSec 3 -UseBasicParsing
    if ($r.StatusCode -ne 200) { Write-Output "FAIL (port $port): HTTP $($r.StatusCode)"; exit 1 }
} catch {
    # always name the port: with a reconfigured server a bare FAIL is blind
    # diagnostics (round-2 audit)
    Write-Output "FAIL (port $port): $($_.Exception.Message)"
    exit 1
}
Write-Output "health OK (port $port)"
# latency probe: small embedding call tells if the model is loaded or sleeping
$body = @{ model = $model; input = "ping" } | ConvertTo-Json
$sw = [System.Diagnostics.Stopwatch]::StartNew()
try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:$port/v1/embeddings" -Method Post `
        -ContentType "application/json" -Body $body -TimeoutSec 30 -UseBasicParsing
    $sw.Stop()
    Write-Output ("embed ping: {0} ms" -f [int]$sw.Elapsed.TotalMilliseconds)
    exit 0
} catch {
    $sw.Stop()
    Write-Output ("embed FAIL: {0} ({1} ms)" -f $_.Exception.Message, [int]$sw.Elapsed.TotalMilliseconds)
    exit 1
}
