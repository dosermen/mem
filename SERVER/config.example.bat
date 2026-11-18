@echo off
rem ============================================================
rem SERVER config - COPY to config.bat and edit for your machine.
rem config.bat is gitignored: it holds YOUR paths, never the repo.
rem All values have working defaults; override only what you need.
rem ============================================================
rem The values below are EXAMPLES - replace them with paths that
rem exist on YOUR machine before copying to config.bat.
rem ============================================================

rem --- embedding model (same alias mem.py sends: MEM_EMBED_MODEL) ---
set SERVER_MODEL=C:\path\to\Qwen3-Embedding-0.6B-Q8_0.gguf
set SERVER_MODEL_ALIAS=Qwen3-Embedding-0.6B

rem --- local fallback llama-server (embed-server.bat) ---
set SERVER_LLAMA_BIN=C:\path\to\llama.cpp\build\bin
set SERVER_PORT=5031

rem --- phone over adb (phone-forward.bat + remote mode) ---
rem adb: full path to adb.exe, or just "adb" if it is on PATH
set SERVER_ADB=adb
rem phone port where llama-server listens on the device (mem.py -> 5031 -> this)
set SERVER_PHONE_PORT=5131
rem ssh control port on the device (adb forward 8022 -> this)
rem --- ssh control port on the device (adb forward 8022 -> this) ---
set SERVER_SSH_PORT=8022
rem --- ssh identity file for status.bat (phone login over 127.0.0.1) ---
set SERVER_SSH_KEY=C:\path\to\phone-key
rem --- host-side watchdog log shown by status.bat (optional, empty = skip) ---
set SERVER_WATCHDOG_LOG=C:\path\to\logs\phone-watchdog.log
rem --- memory paths for status-tray.ps1 (defaults shown, override if needed) ---
set SERVER_MEM_PY=C:\path\to\OLAMA\tools\mem.py
set SERVER_MEM_DB=C:\path\to\OLAMA\memory\memory.db
set SERVER_MEM_STAMP=C:\path\to\OLAMA\logs\mem-backup.stamp
