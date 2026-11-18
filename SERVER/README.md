# SERVER — embedding-сервер для mem.py

Папка запусковых файлов и инструкций. mem.py **никогда не стартует сервер сам**
(каскад: remote → FTS), всё здесь — ручные инструменты.

## Конфигурация (первый шаг на новой машине)

```
cp SERVER/config.example.bat SERVER/config.bat   # и отредактировать пути
```

`config.bat` — **единственное место с личными путями**: он указан в `.gitignore`
и при git init в репозиторий не попадёт. Все скрипты читают его и имеют
рабочие дефолты; порядок разрешения значений: `config.bat` → переменные
окружения → дефолт.

| Переменная | Дефолт | Что |
|---|---|---|
| `SERVER_MODEL` | — | путь к GGUF с embedding-моделью |
| `SERVER_MODEL_ALIAS` | `Qwen3-Embedding-0.6B` | alias в запросах (должен совпадать с `MEM_EMBED_MODEL` в mem.py!) |
| `SERVER_LLAMA_BIN` | — | папка с `llama-server.exe` (для локального fallback) |
| `SERVER_PORT` | `5031` | локальный порт embed-сервера/форвардов (**не читается mem.py!**, см. ниже) |
| `SERVER_ADB` | `adb` | полный путь к `adb.exe` или просто `adb`, если в PATH |
| `SERVER_PHONE_PORT` | `5131` | порт llama-server **на устройстве** |
| `SERVER_SSH_PORT` | `8022` | ssh-порт на устройстве |

⚠️ **`SERVER_PORT` — порт только скриптов `SERVER\`** (phone-forward,
embed-server, health). mem.py его не читает: он знает только `MEM_EMBED_URL`.
Смена порта в `config.bat` требует также поменять `MEM_EMBED_URL` в окружении
mem.py — иначе recall молча деградирует в FTS. `MEM_EMBED_PORT` mem.py
использует только как **последний** fallback в `health_url()`, если
`MEM_EMBED_URL` нечитаем/пуст; штатно health выводится из URL.

## Как это работает

```
mem.py recall/remember/embed
   │
   ├─ MEM_EMBED_URL http://127.0.0.1:5031/v1/embeddings  ← сюда приходят запросы
   │        │
   │        ├─ adb forward tcp:5031 tcp:<SERVER_PHONE_PORT>  → устройство
   │        │   (Termux/SSH, llama-server, embedding-модель)  [ОСНОВНОЙ РЕЖИМ]
   │        │
   │        └─ локальный embed-server.bat              [ТОЛЬКО по явному «да»]
   │
   └─ сервер не отвечает? → молча FTS5 (bm25), mode: "fts (remote server down)"
```

Сервер (оба узла) **спит** (`--sleep-idle-seconds 300`): после 5 мин простоя
веса выгружаются из RAM, следующий вызов грузит их заново — процесс жив, RAM
освобождён. Поэтому 1-й ping после простоя = секунды, последующие — мс.

## Файлы

| Файл | Что делает |
|---|---|
| `config.example.bat` | шаблон конфига → скопировать в `config.bat` |
| `config.bat` | твои пути (gitignored) |
| `phone-forward.bat` | (пере)создаёт adb-форварды `SERVER_PORT→PHONE_PORT` и `SERVER_SSH_PORT`. Запускать при «Connection refused» / после ребута ПК |
| `health.ps1` | проверка: `GET /health` + latency ping `POST /v1/embeddings` (просыпает спящую модель, ничего не пишет). Вывод всегда называет порт. Exit 0 = жив |
| `health.bat` | обёртка над health.ps1: пробрасывает exit-код (0/1) |
| `status.bat` | **одна команда = полная картина**: adb-устройство → форварды → health → llama-server на телефоне (SSH) → хвост watchdog-лога → хвост llama-run.log. Read-only, ничего не стартует. Exit 0 = всё зелёное, 1 = что-то упало |
| `status-tray.ps1` | **трей-монитор** (см. ниже): тултип + баллун со статусом стека и памяти, опрос раз в 2 мин. Read-only, ничего не стартует |
| `embed-server.bat` | **ТОЛЬКО по явному «да»!** Локальный llama-server (fallback) |

## Трей-монитор (`status-tray.ps1`)

Иконка в трее (зелёная/жёлтая/красная), опрос стека **раз в 2 мин**, read-only
(тот же запрет «ничего не стартовать», что у status.bat). Показывает:

- device → форварды → `GET /health` (без wake-ping — латентность уже даёт watchdog);
- **последнее обращение к серверу**: время записи `/sdcard/llama-run.log` + тип
  из последнего `n_tokens` (≤5 = ping watchdog'а, больше = реальный embed);
- память: число active-записей (через `mem.py list`), размер `memory.db`,
  свежесть бэкапа (`mem-backup.stamp`, >40 ч = жёлтый);
- watchdog: возраст лога (>12 мин = жёлтый), последняя строка (respawn/FAIL = жёлтый).

Левый клик — баллун с деталями; правый меню: Details / Refresh now /
Open status.bat / Exit. Режим `-ProbeOnce` — печать статуса в консоль без трея
(exit 0 = ok, 1 = warn, 2 = fail), удобно для проверок.

**Автозагрузка**: задача `Mem-Server-Tray` (ONLOGON, скрытое окно PowerShell) —
создана на этом стенде; на новой машине:

```
schtasks /Create /TN Mem-Server-Tray /SC ONLOGON /TR "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File F:\AI\MEM\SERVER\status-tray.ps1" /F
```

Один экземпляр гарантирован именованным mutex (`Local\MemServerTray`).

## Модель и llama-server

Одна и та же embedding-модель на обоих узлах (alias обязателен: mem.py шлёт
`MEM_EMBED_MODEL` в каждом запросе):

| Узел | Бинарь | Модель |
|---|---|---|
| **Устройство (основной)** | Termux `~/llama-bin/llama-server` | `~/models/embed.gguf` — порт `SERVER_PHONE_PORT` |
| **ПК (fallback, только по «да»)** | `SERVER_LLAMA_BIN\llama-server.exe` | `SERVER_MODEL` — порт `SERVER_PORT` |

Аргументы одинаковы на обоих узлах: `--embedding -c 8192 -t 8 -np 1
--no-warmup -ub 512 --sleep-idle-seconds 300`.

## Быстрый старт

```powershell
# 1. Конфиг (на новой машине)
copy config.example.bat config.bat   # отредактировать

# 2. Форварды (устройство по USB, adb)
.\phone-forward.bat

# 3. Здоровье (обёртка с exit-кодом; либо напрямую health.ps1)
.\health.bat
#    health OK (port 5031) + embed ping: NNN ms  => всё работает
#    FAIL (port 5031): ...  => смотреть порт/форварды, rc=1

# 3b. Полный статус стека одной командой (read-only)
.\status.bat
#    6 блоков: устройство → форварды → health → llama на телефоне →
#    watchdog-лог → llama-run.log; RESULT: OK ⇒ rc=0

# 3c. Трей-монитор (иконка в трее, автозагрузка Mem-Server-Tray)
powershell -NoProfile -ExecutionPolicy Bypass -File .\status-tray.ps1
#    левый клик = баллун с деталями; правый меню: Details / Refresh /
#    Open status.bat / Exit; -ProbeOnce = печать статуса без трея (rc 0/1/2)

# 4. Пользоваться mem.py — он сам пойдёт на remote
python ..\mem.py recall "запрос"
```

## Настройка mem.py (env)

| Переменная | Дефолт | Зачем |
|---|---|---|
| `MEM_EMBED_URL` | `http://127.0.0.1:5031/v1/embeddings` | куда слать эмбеды; из неё же mem.py выводит `/health` (scheme://host/health) — **менять порт = менять этот URL** |
| `MEM_EMBED_PORT` | `5031` | последний fallback для `/health` в `health.ps1` (после `SERVER_PORT` из config.bat) и в `health_url()` mem.py при нечитаемом `MEM_EMBED_URL`; **штатно не задействуется** |
| `MEM_EMBED_MODEL` | `Qwen3-Embedding-0.6B` | alias модели (**= `SERVER_MODEL_ALIAS`**) |
| `MEM_EMBED_REMOTE` | `1` | `0` = вектора выключить совсем (чистый FTS, без сети) |
| `MEM_EMBED_TIMEOUT` | `60` | floor таймаута вызова, с; фактический бюджет растёт с размером батча до cap = max(600 с, значение) |
| `MEM_EMBED_LOCK_S` | `3600` | embed-лок (`MEMORY.md.embed.lock`): пока моложе N сек, второй `embed` пропускается (interlock с Phone-Embed-Watchdog) |

Некорректные числовые значения env → молча берётся дефолт (mem.py не падает);
булевы флаги (`MEM_EMBED_REMOTE`, `MEM_ARTIFACTS`, `MEM_RELATED`) регистронезависимы
(`0` / `false` / `no` / пустое значение — выкл).

Поведение recall — строка `mode:` в конце выдачи: `hybrid (remote)` = гибрид;
`hybrid partial N/M` = часть записей без векторов; `fts (remote server down)`
= сервер не отвечает — не ошибка, всё продолжает работать по FTS;
`fts (no vectors yet)` = сервер жив, но в БД ещё нет ни одного вектора
(свежий деплой до `embed` — запрос тоже уйдёт в FTS);
`fts (vector search off)` = `MEM_EMBED_REMOTE=0`; `fts (--no-vector)` = флаг
`--no-vector` (при нём mem.py не делает ни одного сетевого вызова).

## Если не работает

1. **`Connection refused`** → `phone-forward.bat` (форварды пропали после ребута ПК)
2. **Форвард есть, health FAIL** → llama на устройстве умер:
   `ssh -p <SERVER_SSH_PORT> -i <твой ключ> 127.0.0.1` (именно 127.0.0.1!),
   лог Termux: `/sdcard/llama-run.log`
3. **SSH тоже мёртв** → разбудить устройство и открыть Termux (см. свой AGENTS.md:
   wake → unlock → `am start -n com.termux/.app.TermuxActivity` → screen off)
4. **PC-watchdog** (если настроен) делает это автоматически — вмешиваться обычно не нужно
5. **recall пишет `fts (vector search off)`** → стоит `MEM_EMBED_REMOTE=0`, снять

## Бэкап

Сам бэкап в эту папку не входит — это хостовый инструмент:
`tools\mem-backup.ps1` хост-проекта (реестр корней — `tools\mem-backup-roots.txt`,
запуск раз в сутки из Phone-Embed-Watchdog). Подробности и идея для переноса —
в `../README.md`, раздел «Бэкап».

## Правила проекта

- Устройство — только SSH/adb; экран — только по явному `wake` (если тачскрин битый).
- llama на устройстве **не выгружать**.
- `embed-server.bat` (локальный сервер) — **только по явному «да»** пользователя.
- Чужие llama-server / киперы — только по явному «да».
- Память: `../README.md`; интеграция агента: см. AGENTS.md проекта.
