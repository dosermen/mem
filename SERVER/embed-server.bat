@echo off
rem Local fallback embedding-server (port 5031). READ-ONLY start: only run it
rem when the user explicitly says so - the phone (remote) is the default node.
rem Paths come from config.bat (copy config.example.bat -> config.bat, edit).
rem DisableDelayedExpansion: a `!` in a config path must survive expansion.
setlocal DisableDelayedExpansion
if not exist "%~dp0config.bat" (
  echo config.bat not found: copy config.example.bat to config.bat and edit it
  exit /b 1
)
call "%~dp0config.bat"
if "%SERVER_PORT%"=="" set SERVER_PORT=5031
if "%SERVER_MODEL_ALIAS%"=="" set SERVER_MODEL_ALIAS=Qwen3-Embedding-0.6B
if "%SERVER_LLAMA_BIN%"=="" (
  echo SERVER_LLAMA_BIN is empty in config.bat
  exit /b 1
)
if "%SERVER_MODEL%"=="" (
  echo SERVER_MODEL is empty in config.bat
  exit /b 1
)
if not exist "%SERVER_MODEL%" (
  echo GGUF not found: "%SERVER_MODEL%"
  echo check SERVER_MODEL in config.bat
  exit /b 1
)
if not exist "%SERVER_LLAMA_BIN%\llama-server.exe" (
  echo llama-server.exe not found under SERVER_LLAMA_BIN: "%SERVER_LLAMA_BIN%"
  echo check SERVER_LLAMA_BIN in config.bat
  exit /b 1
)
rem pushd/popd instead of cd /d: the caller's cwd must survive a
rem `call embed-server.bat` (setlocal does NOT localize cwd)
pushd "%SERVER_LLAMA_BIN%"
if errorlevel 1 (
  echo cannot cd to SERVER_LLAMA_BIN: "%SERVER_LLAMA_BIN%"
  exit /b 1
)
llama-server.exe ^
-m "%SERVER_MODEL%" ^
--host 127.0.0.1 --port %SERVER_PORT% ^
--alias "%SERVER_MODEL_ALIAS%" ^
--embedding ^
-c 8192 ^
-t 8 ^
-np 1 ^
--no-warmup ^
--sleep-idle-seconds 300 ^
-ub 512
set RC=%errorlevel%
popd
exit /b %RC%
