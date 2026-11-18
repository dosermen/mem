# status-tray.ps1 - read-only tray monitor for the embedding stack + memory.
# Same philosophy as status.bat: starts NOTHING, stops NOTHING, probes only.
#   default      -> tray icon (left-click = details balloon, right-click = menu)
#   -ProbeOnce   -> print probe result to stdout and exit (for testing)
#                  exit 0 = ok, 1 = warn, 2 = fail
# State colors: green = all ok, amber = degraded, red = something down.
# Config: SERVER_* vars from config.bat next to this script (health.ps1 parser).
param([switch]$ProbeOnce)

$ErrorActionPreference = "SilentlyContinue"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# --- load SERVER_* overrides from config.bat (same idiom as health.ps1) ---
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

$port      = if ($env:SERVER_PORT) { $env:SERVER_PORT } else { 5031 }
$phonePort = if ($env:SERVER_PHONE_PORT) { $env:SERVER_PHONE_PORT } else { 5131 }
$sshPort   = if ($env:SERVER_SSH_PORT) { $env:SERVER_SSH_PORT } else { 8022 }
$adb       = if ($env:SERVER_ADB) { $env:SERVER_ADB } else { "adb" }
$memPy     = if ($env:SERVER_MEM_PY) { $env:SERVER_MEM_PY } else { "F:\AI\OLAMA\tools\mem.py" }
$dbPath    = if ($env:SERVER_MEM_DB) { $env:SERVER_MEM_DB } else { "F:\AI\OLAMA\memory\memory.db" }
$stampPath = if ($env:SERVER_MEM_STAMP) { $env:SERVER_MEM_STAMP } else { "F:\AI\OLAMA\logs\mem-backup.stamp" }
$wdLog     = if ($env:SERVER_WATCHDOG_LOG) { $env:SERVER_WATCHDOG_LOG } else { "F:\AI\OLAMA\logs\phone-watchdog.log" }
$runLog    = "/sdcard/llama-run.log"

function Get-Probe {
    # returns @{ level = ok|warn|fail; lines = @(); short = "..." }
    # Step appends to script-scope vars (nested functions do not see the
    # caller's locals in PowerShell), so init them here and read them back.
    $script:pLines = @()
    $script:pLevel = "ok"
    function Step([string]$l, [string]$lv) {
        $script:pLines += $l
        if ($lv -eq "fail") { $script:pLevel = "fail" }
        elseif ($lv -eq "warn" -and $script:pLevel -ne "fail") { $script:pLevel = "warn" }
    }

    # 1. adb device
    $hasDev = $false
    & $adb start-server 2>$null | Out-Null
    foreach ($d in (& $adb devices 2>$null | Select-Object -Skip 1)) {
        if ($d -match "\bdevice$") { $hasDev = $true }
    }
    if (-not $hasDev) {
        Step "device: FAIL (phone not connected/authorized)" "fail"
        Step "forwards: skipped" "fail"
        Step "health: skipped" "fail"
        Step "last request: skipped (no device)" "fail"
    } else {
        Step "device: OK" "ok"
        # 2. forwards
        $fw = (& $adb forward --list 2>$null) -join "`n"
        $fwEmbed = $fw -match "tcp:$port tcp:$phonePort"
        $fwSsh   = $fw -match "tcp:$sshPort tcp:$sshPort"
        Step ("forwards: embed {0}, ssh {1}" -f $(if ($fwEmbed) {"OK"} else {"MISSING"}),
             $(if ($fwSsh) {"OK"} else {"MISSING"})) $(if ($fwEmbed -and $fwSsh) {"ok"} else {"fail"})

        # 3. health (GET only - no wake ping, the watchdog already probes latency)
        try {
            $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$port/health" -TimeoutSec 3 -UseBasicParsing
            if ($resp.StatusCode -eq 200) { Step "health: OK (port $port)" "ok" }
            else { Step "health: HTTP $($resp.StatusCode) (port $port)" "fail" }
        } catch {
            Step "health: FAIL (port $port): $($_.Exception.Message)" "fail"
        }

        # 4. last request to the server: llama-run.log mtime = last write,
        #    last completed task n_tokens tells the type:
        #    <=5 tok = watchdog/health ping, bigger = real embed (recall/remember)
        $epoch = (& $adb shell "stat -c %Y $runLog" 2>$null | Select-Object -First 1)
        $tailTxt = (& $adb shell "tail -n 60 $runLog" 2>$null) -join "`n"
        if ($epoch -match '^\d+$') {
            $t = [DateTimeOffset]::FromUnixTimeSeconds([int64]$epoch.Trim()).LocalDateTime
            $ms = [regex]::Matches($tailTxt, "n_tokens = (\d+)")
            $req = "unknown"
            if ($ms.Count -gt 0) {
                $ntok = [int]$ms[$ms.Count - 1].Groups[1].Value
                $req = if ($ntok -le 5) { "ping ($ntok tok)" } else { "embed ($ntok tok)" }
            }
            Step ("last request: {0:HH:mm:ss} {1}" -f $t, $req) "ok"
        } else {
            Step "last request: log not readable" "warn"
        }
    }

    # 5. memory: active count + db size + backup freshness
    if (Test-Path $dbPath) {
        $dbMb = [math]::Round((Get-Item $dbPath).Length / 1MB, 1)
        $active = -1
        if (Get-Command python -ErrorAction SilentlyContinue) {
            $out = & python $memPy list --status active 2>$null
            $active = @($out | Where-Object { $_ -match "\S" }).Count
        }
        $memTxt = if ($active -ge 0) { "$active active" } else { "count failed" }
        Step ("memory: {0}, db {1} MB" -f $memTxt, $dbMb) $(if ($active -ge 0) {"ok"} else {"warn"})
    } else {
        Step "memory: db NOT FOUND: $dbPath" "fail"
    }
    if (Test-Path $stampPath) {
        try {
            $stamp = [datetime]::ParseExact((Get-Content $stampPath -Raw).Trim(), "yyyy-MM-dd", $null)
            $ageH = ((Get-Date) - $stamp).TotalHours
            Step ("backup: {0:yyyy-MM-dd} ({1:N1}h ago)" -f $stamp, $ageH) $(if ($ageH -gt 40) {"warn"} else {"ok"})
        } catch {
            Step "backup: stamp unreadable" "warn"
        }
    } else {
        Step "backup: stamp NOT FOUND: $stampPath" "warn"
    }

    # 6. watchdog: age + last line (runs every 5 min => stale after 12)
    if (Test-Path $wdLog) {
        $wdAge = ((Get-Date) - (Get-Item $wdLog).LastWriteTime).TotalMinutes
        $wdLast = (Get-Content $wdLog -Tail 1)
        $wdLv = "ok"
        if ($wdAge -gt 12) { $wdLv = "warn" }
        if ($wdLast -match "respawn|FAIL|fail") { $wdLv = "warn" }
        $wdShort = if ($wdLast -match "([\d.]+)s") { $Matches[1] + "s" } else { "-" }
        Step ("watchdog: {0}m ago, probe {1} - {2}" -f [int]$wdAge, $wdShort, $wdLast) $wdLv
    } else {
        Step "watchdog: log NOT FOUND: $wdLog" "warn"
    }

    # short tooltip line (NotifyIcon.Text is capped ~127 chars)
    $wdAgeS = if (Test-Path $wdLog) { [int]((Get-Date) - (Get-Item $wdLog).LastWriteTime).TotalMinutes } else { 999 }
    $bakS = "?"
    if (Test-Path $stampPath) {
        try { $bakS = ([datetime]::ParseExact((Get-Content $stampPath -Raw).Trim(), "yyyy-MM-dd", $null)).ToString("MM.dd") } catch {}
    }
    $short = "mem {0} | wd {1}m | bak {2}" -f $script:pLevel, $wdAgeS, $bakS
    if ($short.Length -gt 120) { $short = $short.Substring(0, 120) }

    return @{ level = $script:pLevel; lines = $script:pLines; short = $short }
}

function New-TrayIcon([string]$hex) {
    $bmp = New-Object System.Drawing.Bitmap(16, 16)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = "AntiAlias"
    $g.Clear([System.Drawing.Color]::Transparent)
    $b = New-Object System.Drawing.SolidBrush([System.Drawing.ColorTranslator]::FromHtml($hex))
    $pen = New-Object System.Drawing.Pen([System.Drawing.Color]::White, 1.5)
    $g.FillEllipse($b, 1, 1, 14, 14)
    $g.DrawEllipse($pen, 1.5, 1.5, 13, 13)
    $g.Dispose(); $b.Dispose(); $pen.Dispose()
    $h = $bmp.GetHicon(); $bmp.Dispose()
    $ico = [System.Drawing.Icon]::FromHandle($h).Clone()
    [System.Runtime.InteropServices.Marshal]::DestroyHIcon($h)
    return $ico
}

# ---------------- test mode: probe once, no tray ----------------
if ($ProbeOnce) {
    $r = Get-Probe
    $r.lines | ForEach-Object { Write-Output $_ }
    Write-Output ("LEVEL: " + $r.level.ToUpper())
    switch ($r.level) { "ok" { exit 0 } "warn" { exit 1 } default { exit 2 } }
}

# ---------------- tray mode ----------------
$script:dir = $PSScriptRoot   # capture: event handlers may not see $PSScriptRoot
# single instance: a crashed owner is treated as gone (AbandonedMutex = we own it)
$mtx = New-Object System.Threading.Mutex($false, "Local\MemServerTray")
$owned = $false
try { $owned = $mtx.WaitOne(0) } catch { $owned = $true }
if (-not $owned) { exit 0 }

$icons = @{
    ok   = New-TrayIcon "#2ea043"
    warn = New-TrayIcon "#f0aa28"
    fail = New-TrayIcon "#dc3c3c"
}
$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = $icons.ok
$notify.Text = "mem-server: probing..."
$notify.Visible = $true

$menu = New-Object System.Windows.Forms.ContextMenuStrip
$miInfo = $menu.Items.Add("Details")
$miRefresh = $menu.Items.Add("Refresh now")
$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator)) | Out-Null
$miBat = $menu.Items.Add("Open status.bat")
$miExit = $menu.Items.Add("Exit")

$script:prevLevel = "ok"
$script:lastLines = @()

function Update-Ui([bool]$forceBalloon) {
    $r = Get-Probe
    $script:lastLines = $r.lines
    $notify.Icon = $icons[$r.level]
    $txt = "mem-server: " + $r.short
    if ($txt.Length -gt 127) { $txt = $txt.Substring(0, 127) }
    $notify.Text = $txt
    if ($forceBalloon -or $r.level -ne $script:prevLevel) {
        $sev = switch ($r.level) { "ok" { "Info" } "warn" { "Warning" } default { "Error" } }
        $body = ($r.lines -join [char]10)
        if ($body.Length -gt 300) { $body = $body.Substring(0, 300) }
        $notify.ShowBalloonTip(8000, "mem-server: " + $r.level.ToUpper(), $body, $sev)
    }
    $script:prevLevel = $r.level
}

$miInfo.Add_Click({ Update-Ui $true })
$miRefresh.Add_Click({ Update-Ui $true })
$miBat.Add_Click({
    Start-Process cmd.exe -ArgumentList '/k ""' + $script:dir + '\status.bat""'
})
$miExit.Add_Click({
    $timer.Stop()
    $notify.Visible = $false
    $notify.Dispose()
    [System.Windows.Forms.Application]::Exit()
})
$notify.Add_MouseClick({
    param($s, $e)
    if ($e.Button -eq "Left") { Update-Ui $true }
})

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 120000   # full probe every 2 min (adb included, ~2 s)
$timer.Add_Tick({ Update-Ui $false })

Update-Ui $false           # first probe before the message loop
[void]$timer.Start()
[System.Windows.Forms.Application]::Run()
$mtx.ReleaseMutex()
$mtx.Dispose()
