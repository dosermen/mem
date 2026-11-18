# DESIGN — контракт модели MEM (XMemo)

Источник истины — `memory\*.md` с frontmatter XMemo; `memory.db` — пересобираемый
индекс (SQLite+FTS5). Этот файл фиксирует контракт: валидная запись, статусы,
id/версии, бюджеты и скоринг.

## Типы записей (5)

`semantic` · `episodic` · `procedural` · `working` · `identity`

## Frontmatter

Обязательные поля (их пишет mem.py): `type`, `path`, `status`, `version`,
`author`, `updated`. Опциональные: `importance` (контракт 1–5, дефолт 3;
валидируется в MCP — на CLI значение передаётся как есть), `related`
(список id через запятую, допустимы цитаты `12@v2`), `supersedes` (путь
предыдущей версии), `session`. `sync` пропускает файлы без `type`/`path`.

## Статусы и жизненный цикл

| Статус | Значение |
|---|---|
| `active` | обычная запись; попадает в recall и list |
| `superseded` | старая версия после `update`; не удаляется, остаётся в истории |
| `deleted` | tombstone после `forget`; recall не возвращает; в list присутствует (без `--status` выводятся **все** статусы, включая `deleted`) |

- `update` → старая версия `superseded`, новая `active` (перезапись запрещена;
  цель должна быть `active` — иначе цепочка вервсий разветвляется).
- `forget` → tombstone (`deleted`), обратимо.
- `restore` → возвращает запись из `deleted` в `active`; **отказ**, если у
  записи уже есть активный ребёнок (`supersedes`) — иначе в цепочке окажутся
  две активные версии.

## Идентификаторы и версии

- `id` — монотонный в пределах жизни `memory.db`, никогда не переиспользуется
  (даже после удаления md); после удаления самого `memory.db` и повторного
  `sync` нумерация начинается заново.
- Новая версия = новый файл с суффиксом `-vN`: `report-2026-v2.md` →
  `report-2026-v3.md` (старое поведение «дата +1» больше не применяется).
- Поле `supersedes` указывает на предыдущий файл; из него `sync`
  восстанавливает `parent_id`.
- Цитата в выводе — `id@vN`; копируется в `update/forget/restore/show` как есть.

## Бюджеты и лимиты

| Что | Значение |
|---|---|
| Бюджет тела выдачи recall (`--max-bytes`) | 6000 B |
| Таймаут эмбеддинга | floor 60 с (`MEM_EMBED_TIMEOUT`), растёт с размером батча, cap = max(600 с, `MEM_EMBED_TIMEOUT`) |
| Батч эмбеддинга (`EMBED_BATCH`) | 4 записи |
| Embed-лок (`memory/MEMORY.md.embed.lock`) | `MEM_EMBED_LOCK_S`, дефолт 3600 с (interlock с watchdog) |

## Ранжирование recall

- Гибрид (живой remote): bm25 **0.45** + recency **0.20** + importance **0.10** + cosine **0.25**.
- Только FTS (нет векторов): bm25 **0.60** + recency **0.25** + importance **0.15**.
- recency — полураспад 30 дней; importance нормируется: min(max(imp,1),5)/5.

## MCP (`mem.py serve`, MEMv2, stdio JSON-RPC 2.0)

Инструменты: `remember`, `recall`, `search` (алиас recall), `update_memory`,
`forget`, `restore`, `list`, `show`, `link`.

Корень проекта у `serve`: `MEM_ROOT` → вверх от рабочего каталога клиента до
ближайшего маркера: сильные (`.git`, `AGENTS.md`, `opencode.json(c)`,
`.opencode`), `memory\` — только если в нём уже есть `MEMORY.md`/`memory.db` →
иначе сам cwd; на локацию скрипта не падает (изоляция проектов).
