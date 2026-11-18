@echo off
rem Health-check wrapper: run health.ps1 and propagate its exit code
rem (0 = server answers, 1 = down). Probes GET /health and a small
rem POST /v1/embeddings latency ping - read-only, nothing is written.
if not exist "%~dp0health.ps1" (
  echo FAIL: health.ps1 not found next to health.bat
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0health.ps1"
exit /b %errorlevel%
