# MEMv2 — схема работы MCP stdio-сервера mem.py

Дата: 16.10.2026 → актуализация 16.11.2026. Author: opencode.
Контекст: `mem.py serve` (Фаза 2 закрыта встроенным решением, см. запись памяти `40@v1 memv2-mcp-serve`).

## 1. Компоненты и транспорт

```
 ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
 │ opencode        │  │ Roo Code        │  │ Cline          │
 │ mcp.servers.mem │  │ mcp_settings.   │  │ UI: MCP +      │
 │ codemode:false  │  │ json → mem      │  │ `mem.py serve` │
 └────────┬────────┘  └────────┬────────┘  └────────┬────────┘
          │                    │                    │
          │      JSON-RPC 2.0 (newline-delimited)   │
          │      stdio: stdin/stdout, UTF-8         │
          ▼                    ▼                    ▼
 ┌──────────────────────────────────────────────────────────────┐
 │  mem.py serve   (MEMv2, только stdlib)                       │
 │  initialize → protocolVersion / capabilities / instructions  │
 │  tools/list → 9 схем (JSON Schema)                           │
 │  tools/call → _mcp_dispatch: Namespace + redirect_stdout     │
 │  ошибки → isError; не убивают сервер; SystemExit ловится     │
 └────────────────────────────┬─────────────────────────────────┘
                              │  те же cmd_* функции
          ┌───────────────────┴───────────────────┐
          │                                       │
          ▼                                       ▼
 ┌──────────────────┐                   ┌──────────────────────┐
 │  CLI (shell)     │                   │  любой MCP-клиент    │
 │  mem.py recall…  │                   │  (tool call без      │
 │  (правило кавычек│                   │   кавычек/файлов)    │
 └──────────────────┘                   └──────────────────────┘
```

## 2. Потоки внутри ядра

```
  ЗАПИСЬ (remember / update_memory):
  text (JSON-поле!) ──► write_md (memory\*.md, frontmatter XMemo)
                          │            md = source of truth
                          ▼
                        sync ──► memory\memory.db (FTS5-индекс)
                          │
                          ▼
                        embed_missing_quiet ── телефон :5031 жив?
                          │                      ├─ да → вектора в mem_vec
                          │                      └─ нет → молча пропуск
                          ▼
                        auto_related (cosine top-3, порог 0.55)
                          ▼
                        citation: 40@v1   ◄─── ответ агенту

  ПОИСК (recall / search):
  query ──► FTS5 bm25 ─┐
           recency ────┤──► g-веса 0.45/0.20/0.10/0.25 ──► топ-N + бюджет 6000 B
           importance ─┤        ▲
           cosine ─────┘        │ запрос эмбеддится (1 HTTP)
                                │
              телефон :5031 down? ──► мягкая деградация: mode=fts
                                │
              logs\*.md (артефакты) ──► fallback-поиск, если 0 хитов
```

## 3. Ключевые свойства

- **Изоляция проектов (фикс 16.11)**: `serve` берёт корень от **cwd клиента**, а не от скрипта: `MEM_ROOT` env → подъём от cwd к маркеру проекта (`memory`, `.git`, `AGENTS.md`, `opencode.json(c)`, `.opencode`) → cwd. Один глобальный конфиг opencode — но каждый проект со своим `memory\`. CLI остался script-based.
- **Одна кодовая база**: MCP и CLI — одни и те же `cmd_*`; разница только во входе (JSON vs argv).
- **md — истина, БД — индекс**: `mem.py sync` пересобирает `memory.db` из файлов в любой момент.
- **Многоисточность**: записи из opencode/Roo/CLI идут в одни и те же файлы — версии/supersedes не конфликтуют.
- **Отказоустойчивость**: телефон упал → поиск FTS, записи без векторов (долив при следующем embed); ошибка тула → `isError`, сервер жив.

## 4. Точки подключения

| Клиент | Конфиг | Статус |
|---|---|---|
| opencode | `~\.config\opencode\opencode.jsonc` → `mcp.servers.mem` | ✅ live |
| Roo Code | `...\rooveterinaryinc.roo-cline\settings\mcp_settings.json` | ✅ записан |
| Cline | UI → MCP Servers → `python F:\AI\OLAMA\tools\mem.py serve` | 📋 инструкция |
| CLI (любой) | `python tools\mem.py <cmd>` | ✅ работает |
| dsh / Continue | нет MCP-конфига → shell-вызовы | — не подключены |

Portable: `F:\AI\MEM\mem.py` == `F:\AI\OLAMA\tools\mem.py` (sha256 сверять после правок).
