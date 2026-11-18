@echo off
rem (Re)create adb forwards: PC SERVER_PORT -> phone SERVER_PHONE_PORT,
rem PC SERVER_SSH_PORT -> phone SERVER_SSH_PORT. Run on "Connection refused"
rem in SSH/health or after a PC reboot. Paths/port from config.bat.
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

"%SERVER_ADB%" version >nul 2>&1
if errorlevel 1 (
  echo adb not found: "%SERVER_ADB%"
  echo set SERVER_ADB in config.bat - full path to adb.exe or "adb" on PATH
  exit /b 1
)
rem idempotent: revive a wedged adb server (5037) before forwarding
"%SERVER_ADB%" start-server >nul 2>&1
set "FWD_FAIL="
"%SERVER_ADB%" forward "tcp:%SERVER_PORT%" "tcp:%SERVER_PHONE_PORT%"
if errorlevel 1 set "FWD_FAIL=1"
"%SERVER_ADB%" forward "tcp:%SERVER_SSH_PORT%" "tcp:%SERVER_SSH_PORT%"
if errorlevel 1 set "FWD_FAIL=1"
echo.
"%SERVER_ADB%" forward --list
if defined FWD_FAIL exit /b 1
exit /b 0
