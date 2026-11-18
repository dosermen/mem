@echo off
rem status.bat - one-shot read-only status of the embedding stack:
rem adb device -> forwards -> health probe -> llama-server on the phone ->
rem watchdog log tail -> llama-run.log tail. Starts NOTHING, stops NOTHING.
rem Prints OK/FAIL per block and exits 0 = all green, 1 = something is down.
rem DisableDelayedExpansion: a `!` in a config path must survive expansion.
setlocal DisableDelayedExpansion
if not exist "%~dp0config.bat" (
  echo config.bat not found: copy config.example.bat to config.bat and edit it
  exit /b 1
)
call "%~dp0config.bat"
if "%SERVER_ADB%"=="" set SERVER_ADB=adb
if "%SERVER_PORT%"=="" set SERVER_PORT=5031
if "%SERVER_PHONE_PORT%"=="" set SERVER_PHONE_PORT=5131
if "%SERVER_SSH_PORT%"=="" set SERVER_SSH_PORT=8022
if "%SERVER_SSH_KEY%"=="" set SERVER_SSH_KEY=%USERPROFILE%\.ssh\phone-key
set ERRORS=0

echo == 1. adb device ==
"%SERVER_ADB%" start-server >nul 2>&1
"%SERVER_ADB%" devices
set HASDEV=
for /f "skip=1 tokens=2" %%A in ('"%SERVER_ADB%" devices') do if "%%A"=="device" set HASDEV=1
if not defined HASDEV (
  echo FAIL: no adb device - phone not connected / authorized
  echo        rest of the checks need the phone - run phone-forward.bat after plugging in
  set /a ERRORS+=1
  goto :summary
)
echo OK: device present

echo == 2. adb forwards ==
"%SERVER_ADB%" forward --list
set FWD1=0
set FWD2=0
"%SERVER_ADB%" forward --list | findstr /c:"tcp:%SERVER_PORT% tcp:%SERVER_PHONE_PORT%" >nul && set FWD1=1
"%SERVER_ADB%" forward --list | findstr /c:"tcp:%SERVER_SSH_PORT% tcp:%SERVER_SSH_PORT%" >nul && set FWD2=1
if "%FWD1%"=="0" (
  echo FAIL: forward %SERVER_PORT% -^> %SERVER_PHONE_PORT% missing - run phone-forward.bat
  set /a ERRORS+=1
)
if "%FWD2%"=="0" (
  echo FAIL: forward %SERVER_SSH_PORT% missing - run phone-forward.bat
  set /a ERRORS+=1
)
if "%FWD1%"=="1" if "%FWD2%"=="1" echo OK: both forwards present

echo == 3. health probe (GET /health + embed ping) ==
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0health.ps1"
if errorlevel 1 set /a ERRORS+=1

echo == 4. llama-server on the phone (ssh %SERVER_SSH_PORT%) ==
if not exist "%SERVER_SSH_KEY%" (
  echo SKIP: ssh key not found: "%SERVER_SSH_KEY%" - set SERVER_SSH_KEY in config.bat
) else (
  ssh -p %SERVER_SSH_PORT% -i "%SERVER_SSH_KEY%" -o BatchMode=yes -o ConnectTimeout=5 127.0.0.1 "pgrep -af llama-server || echo NONE"
  if errorlevel 1 (
    echo FAIL: ssh did not answer - phone asleep / sshd down / forward missing
    set /a ERRORS+=1
  )
)

echo == 5. watchdog log (last 10 lines) ==
if defined SERVER_WATCHDOG_LOG if exist "%SERVER_WATCHDOG_LOG%" (
  powershell -NoProfile -Command "Get-Content -Tail 10 -Path $env:SERVER_WATCHDOG_LOG"
) else (
  echo SKIP: SERVER_WATCHDOG_LOG not set or missing: "%SERVER_WATCHDOG_LOG%"
)

echo == 6. llama-run.log on the phone (last 10 lines) ==
"%SERVER_ADB%" shell tail -n 10 /sdcard/llama-run.log 2>nul
if errorlevel 1 echo SKIP: /sdcard/llama-run.log not readable

:summary
echo.
if %ERRORS%==0 (
  echo RESULT: OK - all checks passed
  exit /b 0
) else (
  echo RESULT: %ERRORS% check FAILED - see blocks above
  exit /b 1
)
