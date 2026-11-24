#!/usr/bin/env python3
# mem.py - XMemo local memory: SQLite+FTS5 (+optional embeddings) over memory\*.md.
# Portable: drop this folder anywhere (e.g. <project>\MEM\), data lives in ./memory\.
# Requires Python >= 3.10 (write_text newline=, Path.is_relative_to).
# md files stay the source of truth; memory.db is a rebuildable index (mem.py sync).
# Commands: sync | embed | sweep | recall | remember | update | forget | restore | list | show | link (alias: links) | index | serve (MCP stdio, MEMv2)
# Env: MEM_EMBED_URL (default http://127.0.0.1:5031/v1/embeddings),
#      MEM_EMBED_MODEL (default Qwen3-Embedding-0.6B). Without server: FTS5-only.
import argparse
import json
import math
import os
import re
import sqlite3
import struct
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

# Single portable mem.py for any project (user decision 15.11).
# ROOT resolution: MEM_ROOT env wins; a folder named MEM (or a mem.py lying
# next to memory\) is the portable root; mem.py in tools/bin/scripts belongs
# to the parent project (<project>\tools\mem.py -> <project>\memory).
# Fallback: first run in an unknown layout - create memory\ next to the script.
_mem_root_env = os.environ.get("MEM_ROOT")
if _mem_root_env:
    ROOT = Path(_mem_root_env).absolute()
    if not ROOT.is_dir():
        # exists() is not enough: MEM_ROOT pointing at a FILE crashed later
        # in mkdir with a raw traceback (round-2 audit)
        sys.exit(f"mem.py: MEM_ROOT is not a directory: {ROOT} (typo?)")
else:
    # Path(__file__).parent WITHOUT resolve(): a symlinked/junctioned mem.py
    # must keep binding to the project it was copied into, not jump to the
    # source folder's memory\ (audit 16.11).
    _script_dir = Path(__file__).absolute().parent
    if _script_dir.name.upper() == "MEM":
        ROOT = _script_dir
    elif _script_dir.name.lower() in ("tools", "bin", "scripts"):
        # tools-parent wins over a stray tools\memory\ (audit 16.11)
        ROOT = _script_dir.parent
    elif (_script_dir / "memory").is_dir():
        ROOT = _script_dir
    else:
        ROOT = _script_dir
MEM_DIR = ROOT / "memory"
DB_PATH = MEM_DIR / "memory.db"
INDEX_PATH = MEM_DIR / "MEMORY.md"
# How this script is invoked (shown in MEMORY.md hints): tools\mem.py in a
# project tools\ folder, plain mem.py in a portable MEM\ folder.
try:
    SCRIPT_REF = (Path(__file__).resolve().relative_to(ROOT)
                  .as_posix().replace("/", "\\"))
except ValueError:
    SCRIPT_REF = Path(__file__).name


def set_root(root):
    """Rebind ROOT-derived paths. CLI keeps the script-location root;
    `serve` rebinds to the CLIENT's project root (isolation: one mem.py
    serves many projects, each with its own memory\\ - user decision 16.11)."""
    global ROOT, MEM_DIR, DB_PATH, INDEX_PATH, SCRIPT_REF
    ROOT = Path(root)
    MEM_DIR = ROOT / "memory"
    DB_PATH = MEM_DIR / "memory.db"
    INDEX_PATH = MEM_DIR / "MEMORY.md"
    try:
        SCRIPT_REF = (Path(__file__).resolve().relative_to(ROOT)
                      .as_posix().replace("/", "\\"))
    except ValueError:
        SCRIPT_REF = Path(__file__).name


def mcp_root():
    """Project root for `serve`: MEM_ROOT env wins; else walk up from the
    client cwd to the nearest project marker; else cwd itself. Never falls
    back to the script location - that leaked OLAMA's memory into other
    projects. Strong markers (.git, AGENTS.md, ...) rank above a bare
    memory\\ folder: only memory\\ carrying MEMORY.md/memory.db counts as a
    project root (audit 16.11: any folder with an empty memory\\ won)."""
    env = os.environ.get("MEM_ROOT")
    if env:
        p = Path(env).absolute()
        if not p.is_dir():
            sys.exit(f"mem.py: MEM_ROOT is not a directory: {p} (typo?)")
        return p
    cwd = Path.cwd()
    strong = (".git", "AGENTS.md", "opencode.json", "opencode.jsonc",
              ".opencode")
    for cand in (cwd, *cwd.parents):
        if any((cand / m).exists() for m in strong):
            return cand
        mem = cand / "memory"
        if mem.is_dir() and ((mem / "MEMORY.md").is_file()
                             or (mem / "memory.db").is_file()):
            return cand
    return cwd

def env_flag(name, default="1"):
    """Boolean env, case-insensitive: 0/no/false/off = off (audit 16.11:
    MEM_EMBED_REMOTE=False used to evaluate as ENABLED)."""
    return str(os.environ.get(name, default)).strip().lower() not in (
        "0", "", "false", "no", "off")


def env_int(name, default):
    """Int env with fallback: a garbage value must not kill --help (audit)."""
    try:
        return int(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        return default


def env_float(name, default):
    try:
        return float(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        return default


# Embeddings via a dedicated remote server only (phone over `adb forward`).
# Cascade (user decision 15.11): remote -> FTS. There is NO local server:
# when the remote is down, recall degrades to FTS silently. Vectors live in
# mem_vec, so sync/records work offline; only embed (fill) and the vector
# part of recall need the server.
EMBED_URL = os.environ.get(
    "MEM_EMBED_URL", "http://127.0.0.1:5031/v1/embeddings"
)
EMBED_MODEL = os.environ.get("MEM_EMBED_MODEL", "Qwen3-Embedding-0.6B")
# Remote is the default. MEM_EMBED_REMOTE=0 disables vector search entirely
# (pure FTS, no network at all). The per-REQUEST budget is dynamic (see
# embed_texts): a small record gets ~1 min, a huge batch up to 600 s - so a
# peer that hangs mid-request (15.11: update stalled ~7 min on a dead
# phone) costs ~1 min, while a legitimately slow big embed still fits.
EMBED_REMOTE = env_flag("MEM_EMBED_REMOTE", "1")
_et = env_int("MEM_EMBED_TIMEOUT", 60)
EMBED_TIMEOUT_S = _et if _et >= 1 else 60   # 0/negative would fail instantly
EMBED_BATCH = 4
EMBED_PORT = env_int("MEM_EMBED_PORT", 5031)
# Local health/embed calls must bypass http_proxy (audit 16.11: a corporate
# proxy used to swallow 127.0.0.1 requests and silently degrade recall).
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
W_BM25_V, W_RECENCY_V, W_IMPORTANCE_V, W_COS = 0.45, 0.20, 0.10, 0.25

DEFAULT_LIMIT = 10
DEFAULT_MAX_BYTES = 6000
RECENCY_HALFLIFE_DAYS = 30.0
W_BM25, W_RECENCY, W_IMPORTANCE = 0.60, 0.25, 0.15

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def today():
    return date.today().isoformat()


def parse_md(path):
    """Return (frontmatter dict, body str) or None if not a memory file."""
    try:
        # utf-8-sig: a BOM (PowerShell Out-File, some editors) must not make
        # the file unparseable - that used to silently purge the row from the
        # index on the next sync (audit 16.11)
        text = path.read_text(encoding="utf-8-sig")
    except Exception:
        return None
    # tolerate trailing spaces/tabs on the --- delimiter lines
    m = re.match(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?(.*)$", text, re.S)
    if not m:
        return None
    fm = {}
    for line in m.group(1).splitlines():
        if line[:1] in (" ", "\t") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        fm[k.strip()] = v.strip()
    return fm, m.group(2)


def write_md(path, fm, body):
    lines = ["---"]
    for k, v in fm.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    text = "\n".join(lines) + body.rstrip("\n") + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # atomic: temp file + os.replace - a crash/power loss mid-write must
        # never truncate the source-of-truth md (audit 16.11)
        tmp = path.with_name(path.name + ".tmp-write")
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    except OSError as e:
        print(f"mem.py: cannot write {path}: {e}", file=sys.stderr)
        raise SystemExit(1)


def fm_id(fm):
    """Portable id from frontmatter ('id: 7', citation '7@v2' ok) or None
    when absent/ambiguous (P1: ids must survive a full db rebuild)."""
    ids = parse_ids(fm.get("id", ""))
    return ids[0] if len(ids) == 1 else None


def stamp_id(path, mem_id):
    """Write/update the `id: N` line in an existing record's frontmatter.
    Surgical line edit (keeps frontmatter lines that parse_md skips), same
    atomic tmp+os.replace as write_md. Returns True when the file changed."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return False
    m = re.match(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?(.*)$",
                 text, re.S)
    if not m:
        return False
    lines = m.group(1).splitlines()
    want = f"id: {int(mem_id)}"
    for i, ln in enumerate(lines):
        if re.match(r"^id\s*:", ln):
            if ln.split(":", 1)[1].strip() == str(int(mem_id)):
                return False          # already correct: no write, no mtime bump
            lines[i] = want
            break
    else:
        lines.insert(0, want)
    # read_text already normalized newlines; write back LF like write_md
    text = "---\n" + "\n".join(lines) + "\n---\n" + m.group(2)
    try:
        tmp = path.with_name(path.name + ".tmp-write")
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    except OSError as e:
        print(f"mem.py: cannot stamp id into {path}: {e}", file=sys.stderr)
        return False
    return True


def _next_id(db):
    """Next free record id: max of the table and the AUTOINCREMENT
    sequence (explicit-id inserts must never hand out a taken rowid)."""
    m = db.execute("SELECT COALESCE(MAX(id),0) m FROM memories") \
        .fetchone()["m"]
    try:
        row = db.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='memories'").fetchone()
        s = row["seq"] if row else 0
    except sqlite3.Error:
        s = 0
    return max(m, s) + 1


def iter_memory_files():
    # explicit suffix check: rglob("*.md") is case-sensitive on POSIX, so a
    # NOTE.MD synced on Windows would vanish from `seen` on Linux and its row
    # would be deleted (audit 16.11)
    for p in sorted(MEM_DIR.rglob("*")):
        if not p.is_file() or p.suffix.lower() != ".md":
            continue
        if p.name == "MEMORY.md" or ".backup" in p.parts:
            continue
        yield p


def rel(p):
    p = Path(p)
    if not p.is_absolute():
        # find_file may hand back a cwd-relative "path as given" target;
        # lexical relative_to against an absolute MEM_DIR raised ValueError
        # (round-2 audit: crash in show/update/forget/restore/link)
        p = Path(os.path.abspath(p))
    try:
        return p.relative_to(MEM_DIR).as_posix()
    except ValueError:
        # symlinked MEM_DIR: compare resolved forms instead of crashing
        return p.resolve().relative_to(MEM_DIR.resolve()).as_posix()


def parse_ids(s):
    """Parse 'related: 12, 15@v2, 3' -> [12, 15, 3] (citations ok)."""
    out = []
    for tok in re.split(r"[,\s]+", (s or "").strip()):
        m = re.match(r"^(\d+)(?:@v\d+)?$", tok)
        if m and int(m.group(1)) not in out:
            out.append(int(m.group(1)))
    return out


def parse_version(s):
    """'1' -> 1, '2.1' -> 2 (major part), junk -> 1. The old
    re.sub(r'[^\\d]') turned '2.1' into 21 (audit 16.11)."""
    try:
        return max(1, int(float(str(s).strip() or "1")))
    except (TypeError, ValueError):
        return 1


def links_of(db, mem_id):
    """Return (outgoing ids, incoming ids) for a record."""
    r = db.execute("SELECT related FROM memories WHERE id=?",
                   (mem_id,)).fetchone()
    out = parse_ids(r["related"] if r else "")
    inc = []
    for row in db.execute("SELECT id, related FROM memories").fetchall():
        if mem_id in parse_ids(row["related"]):
            inc.append(row["id"])
    return out, inc


def _retry_schema(fn, attempts=6):
    """Retry a schema step that can race another mem.py process started at
    the same time (round-2 audit: 5 startup races - 'database is locked' on
    the WAL pragma, 'duplicate column name', 'no such table: memories'
    while a peer was mid-rebuild, 'table memories_fts already exists')."""
    import time as _t
    for i in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            retryable = any(s in msg for s in (
                "database is locked", "duplicate column name",
                "no such table", "already exists"))
            if not retryable or i == attempts - 1:
                raise
            _t.sleep(0.05 * (2 ** i))
    raise RuntimeError("unreachable")


_MEMORIES_DDL = """CREATE TABLE IF NOT EXISTS memories(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file TEXT UNIQUE NOT NULL,
            path TEXT NOT NULL,
            type TEXT NOT NULL,
            content TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            version INTEGER NOT NULL DEFAULT 1,
            parent_id INTEGER,
            importance INTEGER NOT NULL DEFAULT 3,
            author TEXT DEFAULT '',
            session TEXT DEFAULT '',
            created_at TEXT DEFAULT '',
            updated_at TEXT DEFAULT '',
            related TEXT DEFAULT ''
        )"""


def _db_init(db):
    """Idempotent schema setup, safe against a concurrent mem.py process."""
    _retry_schema(lambda: db.execute("PRAGMA journal_mode=WAL"))
    _retry_schema(lambda: db.execute(_MEMORIES_DDL))
    _retry_schema(lambda: db.execute(
        """CREATE TABLE IF NOT EXISTS mem_vec(
            mem_id INTEGER PRIMARY KEY,
            model TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            vec BLOB NOT NULL
        )"""
    ))
    # Artifact vectors (sweep): one row per chunk of a logs/*.md report.
    # mtime+model drive the incremental re-embed; chunk_text keeps the
    # snippet available without re-reading the file (audit/sweep 17.11).
    _retry_schema(lambda: db.execute(
        """CREATE TABLE IF NOT EXISTS art_vec(
            relpath TEXT NOT NULL,
            chunk INTEGER NOT NULL,
            mtime INTEGER NOT NULL,
            model TEXT NOT NULL,
            vec BLOB NOT NULL,
            chunk_text TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (relpath, chunk)
        )"""
    ))
    # Migration: related ids from frontmatter ("related: 12, 15@v2")

    def _add_related():
        cols = [r["name"] for r in db.execute("PRAGMA table_info(memories)")]
        if "related" not in cols:
            db.execute("ALTER TABLE memories ADD COLUMN related TEXT "
                       "DEFAULT ''")
        return cols

    cols = _retry_schema(_add_related)
    # Recovery: a crash mid-rebuild used to leave memories_old orphaned and
    # the index silently half-empty (round-2 audit). md files are the source
    # of truth, so copy anything missing back and drop the shadow table.
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                  " AND name='memories_old'").fetchone():
        try:
            db.execute("BEGIN IMMEDIATE")
            old_cols = [r["name"] for r in db.execute(
                "PRAGMA table_info(memories_old)")]
            new_cols = [r["name"] for r in db.execute(
                "PRAGMA table_info(memories)")]
            common = [c for c in new_cols if c in set(old_cols)]
            if common:
                db.execute(
                    f"INSERT OR IGNORE INTO memories ({','.join(common)}) "
                    f"SELECT {','.join(common)} FROM memories_old")
            db.execute("DROP TABLE memories_old")
            db.commit()
        except sqlite3.Error:
            db.rollback()
            raise
    # Migration: ids must never be reused after a row is deleted (audit
    # 16.11: removing a file freed its id, the next remember stole it and
    # old `related: 12` / `12@v1` citations pointed at a different record;
    # a stale mem_vec row even lent the new record a foreign vector).
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='memories'"
    ).fetchone()
    if row and "AUTOINCREMENT" not in (row["sql"] or ""):

        def _migrate():
            # ONE transaction for RENAME+CREATE+INSERT+DROP: sqlite DDL is
            # transactional, but python sqlite3 does not wrap it implicitly -
            # a crash between statements used to orphan memories_old and
            # restart ids at 1 (round-2 audit)
            db.execute("BEGIN IMMEDIATE")
            try:
                old_cols = [r["name"] for r in db.execute(
                    "PRAGMA table_info(memories)")]
                db.execute("ALTER TABLE memories RENAME TO memories_old")
                db.execute(_MEMORIES_DDL)
                new_cols = [r["name"] for r in db.execute(
                    "PRAGMA table_info(memories)")]
                common = [c for c in new_cols if c in set(old_cols)]
                db.execute(
                    f"INSERT INTO memories ({','.join(common)}) "
                    f"SELECT {','.join(common)} FROM memories_old")
                db.execute("DROP TABLE memories_old")
                db.commit()
            except sqlite3.Error:
                db.rollback()
                raise

        _retry_schema(_migrate)
        cols = [r["name"] for r in db.execute("PRAGMA table_info(memories)")]
    # FTS index must be a virtual FTS5 table; recreate legacy/plain ones (index is rebuildable)
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE name='memories_fts'"
    ).fetchone()
    if row and not (row["sql"] or "").lstrip().upper().startswith("CREATE VIRTUAL TABLE"):
        _retry_schema(lambda: db.execute("DROP TABLE memories_fts"))
        row = None
    if row is None:

        def _fts():
            db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5("
                "text, mem_id UNINDEXED)"
            )
            db.execute(
                "INSERT INTO memories_fts(text,mem_id) "
                "SELECT content,id FROM memories"
            )
            db.commit()

        _retry_schema(_fts)
    return db


def open_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        # timeout=30: the sqlite3 default 5 s was too short for concurrent
        # CLI + embed/sync runs (audit 16.11: raw "database is locked")
        db = sqlite3.connect(DB_PATH, timeout=30)
    except sqlite3.Error as e:
        sys.exit(f"mem.py: cannot open {DB_PATH}: {e}")
    db.row_factory = sqlite3.Row
    try:
        _db_init(db)
    except sqlite3.Error as e:
        # readonly dir / corrupt file / lost schema race: a clear message,
        # not a raw traceback (round-2 audit)
        db.close()
        sys.exit(f"mem.py: schema init failed on {DB_PATH}: {e}")
    return db


def _not_synced_yet(db):
    """user_version=0 means cmd_sync never completed for this db. The marker
    keeps the 'fresh deploy' branch from resyncing forever when every file
    fails to parse (rows stay empty and the emptiness is never disproved)."""
    try:
        return db.execute("PRAGMA user_version").fetchone()[0] == 0
    except sqlite3.Error:
        return False


def ensure_fresh(db):
    """Auto-resync when md files drifted from the index: (a) a record file
    was deleted (files are the source of truth), (b) first run on a fresh
    deploy (db just created empty, md copied with older mtimes), (c) any md
    newer than the db (manual edits). Audit 16.11: (a) and (b) were invisible
    before - deleted files haunted recall forever and a fresh portable copy
    reported 0 hits until a manual sync."""
    try:
        files = list(iter_memory_files())
    except OSError:
        files = []
    try:
        row_files = {r[0] for r in db.execute("SELECT file FROM memories")}
    except sqlite3.Error:
        row_files = set()
    stale = False
    disk_files = {rel(p) for p in files}
    if row_files - disk_files:
        stale = True                     # a record file was deleted on disk
    elif files and not row_files and _not_synced_yet(db):
        stale = True                     # fresh deploy: db empty, md present
    else:
        # (d) a parseable md has no row at all (round-2 audit: a remember
        # whose sync lost a race left the file on disk while db mtime was
        # newer than the file - condition (c) then judged everything fresh
        # and recall silently missed the record forever). Only *parseable*
        # files count, otherwise a broken frontmatter would resync forever.
        missing = False
        for p in files:
            if rel(p) in row_files:
                continue
            try:
                parsed = parse_md(p)
            except OSError:
                continue
            if parsed and "type" in parsed[0] and "path" in parsed[0]:
                missing = True
                break
        if missing:
            stale = True
    if not stale:
        try:
            db_m = DB_PATH.stat().st_mtime
        except OSError:
            db_m = 0
        for p in files:
            try:
                if p.stat().st_mtime > db_m:
                    stale = True
                    break
            except OSError:
                continue
    if not stale:
        return db
    db.close()                           # never leak the superseded conn
    return db_sync_quiet()


def db_sync_quiet():
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cmd_sync(None)
    return open_db()


def cmd_sync(args):
    db = open_db()
    # serialize writers: without this, two mem.py processes race on the
    # select-then-write INSERT (UNIQUE constraint failed) and the schema
    # steps above (round-2 audit). 30 s busy timeout backs it up.
    db.execute("BEGIN IMMEDIATE")
    # ids survive even an EMPTY rebuild when frontmatter carries portable
    # `id:` lines (P1); only legacy files without them renumber - the honest
    # warning below counts those. A healthy incremental sync must stay silent
    # (caught by test: the first version compared imported==total and
    # screamed every time)
    n_before = db.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]
    seen = {}
    stats = {"imported": 0, "skipped": 0}
    # one parse pass (round-2 audit: the supersedes pass re-parsed every file)
    parsed_by_rel = {}
    for p in iter_memory_files():
        f = rel(p)
        # the file exists on disk -> keep its row even when frontmatter is
        # broken (BOM / half-saved edit): the delete pass below must never
        # purge a record just because it stopped parsing (audit 16.11)
        seen[f] = True
        parsed = parse_md(p)
        parsed_by_rel[f] = parsed
        if not parsed or "type" not in parsed[0] or "path" not in parsed[0]:
            stats["skipped"] += 1

    # P1 pre-claim: raise the AUTOINCREMENT sequence above every id the
    # frontmatter claims BEFORE any insert - otherwise a legacy row inserted
    # first can eat rowid N that a later file's `id:` line owns, and a
    # fresh rebuild would renumber despite portable ids.
    explicit_max = 0
    for parsed in parsed_by_rel.values():
        if not parsed:
            continue
        fm, _ = parsed
        if "type" not in fm or "path" not in fm:
            continue
        w = fm_id(fm)
        if w and w > explicit_max:
            explicit_max = w
    if explicit_max:
        try:
            row = db.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='memories'"
            ).fetchone()
            cur = row["seq"] if row else 0
            if cur < explicit_max:
                if row:
                    db.execute("UPDATE sqlite_sequence SET seq=?"
                               " WHERE name='memories'", (explicit_max,))
                else:
                    db.execute("INSERT INTO sqlite_sequence(name,seq)"
                               " VALUES('memories',?)", (explicit_max,))
        except sqlite3.Error:
            pass  # no AUTOINCREMENT bookkeeping yet: inserts bump it themselves

    for f, parsed in parsed_by_rel.items():
        if not parsed:
            continue
        fm, body = parsed
        if "type" not in fm or "path" not in fm:
            continue
        status = fm.get("status", "active")
        version = parse_version(fm.get("version", "1"))
        try:
            importance = int(fm.get("importance", "3"))
        except ValueError:
            importance = 3
        updated = fm.get("updated", "")
        created = fm.get("created", updated)
        related = ",".join(str(i) for i in parse_ids(fm.get("related", "")))
        vals = (fm["path"], fm["type"], body.strip(), status, version,
                importance, fm.get("author", ""), fm.get("session", ""),
                created, updated, related)
        want = fm_id(fm)   # P1: frontmatter id is authoritative for NEW rows
        # select-then-write, NOT ON CONFLICT DO UPDATE: an UPSERT that hits
        # the file conflict still allocates a rowid first and bumps
        # sqlite_sequence (measured 16.11), so every sync of an N-row db
        # wasted N ids once the table became AUTOINCREMENT
        try:
            row = db.execute("SELECT id FROM memories WHERE file=?",
                             (f,)).fetchone()
            if row:
                # existing row keeps its db id (vectors/related point at it);
                # a stale frontmatter id is healed back by the stamp pass
                if want is not None and want != row["id"]:
                    print(f"sync: WARNING id mismatch on {f}: frontmatter "
                          f"id {want} != db id {row['id']} - keeping db id, "
                          f"frontmatter healed")
                db.execute(
                    """UPDATE memories SET path=?,type=?,content=?,status=?,
                         version=?,importance=?,author=?,session=?,
                         created_at=?,updated_at=?,related=? WHERE file=?""",
                    (*vals, f))
            else:
                if want is not None:
                    holder = db.execute(
                        "SELECT file FROM memories WHERE id=?",
                        (want,)).fetchone()
                    if holder is not None:
                        # two records claim one id (hand-copied md): keep the
                        # holder, give this file a fresh id; the stamp pass
                        # rewrites its frontmatter to match
                        want = _next_id(db)
                        print(f"sync: WARNING id conflict on {f}: "
                              f"id already held by {holder['file']}, "
                              f"reassigned to {want}")
                    db.execute(
                        """INSERT INTO memories(id,file,path,type,content,
                             status,version,importance,author,session,
                             created_at,updated_at,related)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (want, f, *vals))
                else:
                    db.execute(
                        """INSERT INTO memories(file,path,type,content,status,
                             version,importance,author,session,created_at,
                             updated_at,related)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (f, *vals))
        except sqlite3.IntegrityError:
            # lost a race to a peer that inserted the same file between our
            # SELECT and INSERT (BEGIN IMMEDIATE makes this rare, not
            # impossible across processes): fall back to UPDATE (round-2 audit)
            db.execute(
                """UPDATE memories SET path=?,type=?,content=?,status=?,
                     version=?,importance=?,author=?,session=?,
                     created_at=?,updated_at=?,related=? WHERE file=?""",
                (*vals, f))
        stats["imported"] += 1

    # resolve supersedes -> parent_id (second pass over the cached parses)
    id_by_file = {r["file"]: r["id"] for r in db.execute("SELECT id,file FROM memories")}
    for f, parsed in parsed_by_rel.items():
        if not parsed:
            continue
        fm, _ = parsed
        sup = fm.get("supersedes", "")
        if not sup:
            # supersedes was removed from frontmatter -> drop the stale parent
            db.execute("UPDATE memories SET parent_id=NULL WHERE file=?",
                       (f,))
            continue
        sup_norm = sup.replace("\\", "/").split("/")[-1]
        parent = id_by_file.get(sup.replace("\\", "/"))
        if parent is None:
            for ff, i in id_by_file.items():
                if ff.split("/")[-1] == sup_norm:
                    parent = i
                    break
        if parent is not None:
            db.execute("UPDATE memories SET parent_id=? WHERE file=?",
                       (parent, f))

    # drop rows for files that no longer exist (files are source of truth)
    for r in db.execute("SELECT id,file FROM memories").fetchall():
        if r["file"] not in seen:
            db.execute("DELETE FROM memories WHERE id=?", (r["id"],))
            # children of the purged row would otherwise keep a dangling
            # parent_id and cmd_show printed a nonexistent id (round-2 audit)
            db.execute("UPDATE memories SET parent_id=NULL WHERE parent_id=?",
                       (r["id"],))
    db.execute(
        "DELETE FROM mem_vec WHERE mem_id NOT IN (SELECT id FROM memories)"
    )

    # full FTS rebuild (small corpus)
    db.execute("DELETE FROM memories_fts")
    db.execute(
        "INSERT INTO memories_fts(text,mem_id) SELECT content,id FROM memories"
    )
    db.commit()
    # P1 portable ids: stamp the durable db id into every record whose
    # frontmatter lacks it (or carries a stale one) - doubles as the
    # one-time migration for md written before ids lived in frontmatter.
    # After commit so only durable ids reach the files; before the DB_PATH
    # utime below so ensure_fresh never sees md newer than the db.
    stamped = 0
    for f, parsed in parsed_by_rel.items():
        if not parsed or f not in id_by_file:
            continue
        if fm_id(parsed[0]) == id_by_file[f]:
            continue
        if stamp_id(MEM_DIR / f, id_by_file[f]):
            stamped += 1
    if stamped:
        print(f"sync: stamped {stamped} record id(s) into frontmatter")
    # first-run marker for ensure_fresh + fold the WAL into the main file so
    # the db mtime actually advances (audit 16.11: an uncheckpointed WAL hid
    # the mtime and every recall repeated the full sync while a reader held
    # the db open)
    db.execute("PRAGMA user_version=1")
    try:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass
    db.commit()
    # belt and braces: if a reader still blocks the checkpoint the db file
    # mtime never moves and ensure_fresh (c) would re-sync on every command
    # (round-2 audit) - bump it explicitly
    try:
        os.utime(DB_PATH, None)
    except OSError:
        pass
    n_active = db.execute(
        "SELECT COUNT(*) c FROM memories WHERE status='active'"
    ).fetchone()["c"]
    n_total = db.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]
    # honest warning: a full rebuild renumbers ids only for LEGACY records
    # that carry related but no portable id in frontmatter (P1: files with
    # `id:` are re-inserted under their own id, so their related survives;
    # round-2 audit finding #2, fix shipped with portable ids).
    # n_before==0 is the real signal; an incremental sync has nothing to warn
    # about even when every file was re-imported.
    if n_before == 0 and n_total:
        legacy_linked = 0
        for f, parsed in parsed_by_rel.items():
            if not parsed or f not in id_by_file:
                continue
            if (parse_ids(parsed[0].get("related", ""))
                    and fm_id(parsed[0]) is None):
                legacy_linked += 1
        if legacy_linked:
            print(f"sync: WARNING full rebuild renumbers ids - {legacy_linked} "
                  f"record(s) carry related ids but no portable id - "
                  f"related may now point elsewhere")
    write_index(db)  # MEMORY.md stays in sync automatically
    print(f"sync: {stats['imported']} imported, {stats['skipped']} skipped, "
          f"db={n_total} (active={n_active}) -> {DB_PATH}")
    db.close()


def fts_query(q):
    terms = [t for t in re.findall(r"\w+", q, flags=re.UNICODE) if t]
    if not terms:
        return None
    return " OR ".join(f'"{t}"*' for t in terms)


# --- artifact search (reports in logs/*.md are NOT in memory: recall must
# fall back to them so "what did we measure about X" finds the full report,
# not just the excerpt quoted in a memory record) ---
STOPWORDS = {"the", "and", "for", "with", "that", "this", "from", "are",
             "was", "were", "have", "has", "not", "you", "all", "can",
             "что", "как", "это", "для", "при", "из", "на", "по", "не",
             "или", "же", "бы", "его", "её"}
ARTIFACTS_MAX_FILE_B = 2 * 1024 * 1024   # skip monsters (raw dumps)
ARTIFACTS_MAX_TOTAL_B = 50 * 1024 * 1024  # per-recall read budget
ARTIFACTS_SNIPPET_B = 400


def artifacts_dir():
    r"""MEM_ARTIFACTS_DIR wins; else <root>/logs; in the portable layout
    (<project>\MEM\mem.py) also try <project>\logs - audit 16.11: the old
    code looked only at ROOT\logs, which does not exist there, so the
    artifact fallback silently never ran. Off when MEM_ARTIFACTS=0."""
    if not env_flag("MEM_ARTIFACTS", "1"):
        return None
    env = os.environ.get("MEM_ARTIFACTS_DIR")
    candidates = [Path(env)] if env else [ROOT / "logs", ROOT.parent / "logs"]
    for p in candidates:
        if p.is_dir():
            return p
    return None


def skip_artifact(rel):
    r"""True for report paths excluded from scan/index/search: vendor dumps
    under logs\ (node-pkg = extracted npm package - its node_modules READMEs
    / CHANGELOGs are not project reports; 1035 wasted embed chunks, 24.11).
    MEM_ARTIFACTS_SKIP = comma-separated substrings matched against the
    relpath (both slashes); empty disables the filter entirely."""
    pats = os.environ.get("MEM_ARTIFACTS_SKIP", "node-pkg")
    if not pats:
        return False
    low = rel.replace("\\", "/").lower()
    return any(p.strip() and p.strip().lower() in low
               for p in pats.split(","))


def artifact_chunks(text, size=None):
    """Split a report into ~size char chunks on paragraph boundaries
    (sweep 17.11: whole-file vectors blur a 24 KB report into one point)."""
    if size is None:
        size = env_int("MEM_ART_CHUNK", 1600)
    out, cur = [], ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        while len(para) > size:          # giant single paragraph: hard split
            if cur:
                out.append(cur)
                cur = ""
            out.append(para[:size])
            para = para[size:]
        if cur and len(cur) + len(para) + 2 > size:
            out.append(cur)
            cur = para
        else:
            cur = cur + "\n\n" + para if cur else para
    if cur:
        out.append(cur)
    return out


def search_artifacts(query, limit=3, db=None, qvec=None):
    """Search reports in logs/*.md: greedy keyword (always) + art_vec
    vector similarity when db and a query vector are given - semantic hits
    keyword missed (sweep 17.11). Returns [(relpath, score, snippet, src)]
    with src in 'kw' | 'vec' | 'kw+vec'. Keyword half is pure text, ~0.05 s;
    the vector half is a local cosine over art_vec (no network)."""
    root = artifacts_dir()
    if root is None:
        return []
    terms = [t.lower() for t in re.findall(r"\w+", query, flags=re.UNICODE)
             if len(t) >= 3 and t.lower() not in STOPWORDS]
    # relpath -> [score, snippet, kw_hit, vec_hit]
    hits = {}
    if terms:
        try:
            # suffix.lower(): rglob("*.md") is case-sensitive on POSIX - a
            # REPORT.MD was never found (round-2 audit, matches iter_memory_files)
            files = [f for f in root.rglob("*")
                     if f.is_file() and f.suffix.lower() == ".md"
                     and not skip_artifact(str(f.relative_to(root)))]
            # freshest first + total byte budget: a logs\ full of old reports
            # must not be re-read in full on every recall (audit 16.11)
            files.sort(key=lambda f: -f.stat().st_mtime)
        except OSError:
            files = []
        read_b = 0
        for f in files:
            try:
                size = f.stat().st_size
                if (size > ARTIFACTS_MAX_FILE_B
                        or read_b + size > ARTIFACTS_MAX_TOTAL_B):
                    continue
                text = f.read_text(encoding="utf-8", errors="ignore")
                read_b += size
            except OSError:
                continue
            low = text.lower()
            score = sum(1 for t in terms if t in low)
            if not score:
                continue
            # snippet around the first term hit
            pos = min((low.find(t) for t in terms if t in low), default=0)
            start = max(0, pos - ARTIFACTS_SNIPPET_B // 2)
            snippet = text[start:start + ARTIFACTS_SNIPPET_B].replace("\n", " ")
            if start:
                snippet = "..." + snippet
            if start + ARTIFACTS_SNIPPET_B < len(text):
                snippet += "..."
            rel = str(f.relative_to(root))
            hits[rel] = [score / len(terms), snippet, True, False]
    # vector half: cosine over stored art_vec chunks (skipped without db/qvec)
    if db is not None and qvec is not None:
        min_sim = env_float("MEM_ART_VEC_MIN", 0.35)
        try:
            vrows = [vr for vr in db.execute(
                "SELECT relpath, vec, chunk_text FROM art_vec WHERE model=?",
                (EMBED_MODEL,)).fetchall()
                if not skip_artifact(vr["relpath"])]
        except sqlite3.Error:
            vrows = []
        best = {}                        # relpath -> (cos, chunk_text)
        for vr in vrows:
            try:
                c = cosine(qvec, unpack_vec(vr["vec"]))
            except struct.error:
                continue                 # corrupt blob: skip, never crash
            if c >= min_sim and c > best.get(vr["relpath"], (-1.0, ""))[0]:
                best[vr["relpath"]] = (c, vr["chunk_text"])
        for rel, (c, ctxt) in best.items():
            snip = ctxt[:ARTIFACTS_SNIPPET_B].replace("\n", " ")
            if len(ctxt) > ARTIFACTS_SNIPPET_B:
                snip += "..."
            if rel in hits:
                hits[rel][0] = max(hits[rel][0], c)
                hits[rel][3] = True       # keep the keyword snippet: it has
            else:                         # term context, the chunk may not
                hits[rel] = [c, snip, False, True]
    out = []
    for rel, (score, snippet, kw, vec) in hits.items():
        src = "kw+vec" if kw and vec else ("kw" if kw else "vec")
        out.append((rel, score, snippet, src))
    out.sort(key=lambda h: -h[1])
    return out[:limit]


def recency(updated_at):
    try:
        d = datetime.strptime(updated_at[:10], "%Y-%m-%d").date()
        age = (date.today() - d).days
    except Exception:
        return 0.5
    return 0.5 ** (max(age, 0) / RECENCY_HALFLIFE_DAYS)


def health_url():
    """Health endpoint derived from EMBED_URL (audit 16.11: it used to be
    hardcoded 127.0.0.1:EMBED_PORT, so a custom MEM_EMBED_URL made health
    check one host while embeddings went to another - split-brain, a live
    server reported as down). MEM_EMBED_PORT remains as fallback only."""
    try:
        parts = urllib.parse.urlsplit(EMBED_URL)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}/health"
    except ValueError:
        pass
    return f"http://127.0.0.1:{EMBED_PORT}/health"


def server_ready(timeout=3):
    """GET /health: 200 when the model is loaded (or sleeping, which also
    answers 200 and wakes on the next embeddings call)."""
    try:
        with _OPENER.open(health_url(), timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def ensure_server():
    """Cascade remote -> FTS: True only when the dedicated remote server
    answers /health. Nothing is ever started locally - when the remote is
    down (or vectors are disabled), the caller degrades to FTS silently."""
    if not EMBED_REMOTE:
        return False
    # A sleeping remote answers /health immediately and wakes on the
    # embeddings call itself.
    return server_ready()


def embed_budget(texts):
    """Seconds to wait for one /v1/embeddings response, scaled by payload:
    the phone embeds ~100 chars/s (measured 327 tok = 10-16 s); 2.5x margin
    (total//40), floor EMBED_TIMEOUT_S=60, cap 600 s. A dead peer therefore
    stalls a small interactive update for ~1 min, not the old flat 600 s,
    while a big batch (4 x 7 KB) still gets the full 600 s."""
    total = sum(len(t) for t in texts)
    # cap >= 600 so a user-raised MEM_EMBED_TIMEOUT is honored (audit 16.11:
    # the hard min(600, ...) silently clamped larger user values)
    cap = max(600, EMBED_TIMEOUT_S)
    return min(cap, max(EMBED_TIMEOUT_S, total // 40 + EMBED_TIMEOUT_S))


def embed_texts(texts, url=EMBED_URL):
    """POST /v1/embeddings, return list of float vectors (index-ordered)."""
    payload = json.dumps({"model": EMBED_MODEL, "input": texts}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with _OPENER.open(req, timeout=embed_budget(texts)) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # surface the server's own reason (model not found, dim mismatch,
        # 413 payload too large ...) instead of a bare URL (audit 16.11)
        body = e.read()[:300].decode("utf-8", errors="replace")
        raise RuntimeError(f"embed HTTP {e.code}: {body}") from e
    items = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
    vecs = [it.get("embedding") for it in items]
    if len(vecs) != len(texts) or any(v is None for v in vecs):
        raise RuntimeError(f"bad embeddings response: {len(vecs)}/{len(texts)}")
    for v in vecs:
        if any(not math.isfinite(x) for x in v):
            raise RuntimeError("embeddings response contains NaN/inf")
    return vecs


def pack_vec(v):
    return struct.pack(f"<{len(v)}f", *v)


def unpack_vec(b):
    return struct.unpack(f"<{len(b) // 4}f", b)


def cosine(a, b):
    if not a or len(a) != len(b):
        # dim mismatch (embedding model swapped under the same name): never
        # compare partially - zip() used to truncate to the shorter vector
        # and yield garbage similarities / wrong auto-links (audit 16.11)
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def auto_related(db, new_id, top=None, min_sim=None):
    """Cosine top-N over stored vectors: suggest `related` for a fresh record.
    Skips silently when either side has no vector yet. Returns [] on off."""
    if not env_flag("MEM_RELATED", "1"):
        return []
    if top is None:
        # clamp: a negative top would slice from the END of the list and
        # drop the *last* N links instead of disabling auto-linking
        top = max(0, env_int("MEM_RELATED_TOP", 3))
    if min_sim is None:
        min_sim = env_float("MEM_RELATED_MIN", 0.55)
    row = db.execute("SELECT vec FROM mem_vec WHERE mem_id=? AND model=?",
                     (new_id, EMBED_MODEL)).fetchone()
    if not row or not row["vec"]:
        return []
    try:
        v = unpack_vec(row["vec"])
    except struct.error:
        return []                    # corrupt blob: skip, never crash
    cands = db.execute(
        """SELECT m.id, v.vec FROM memories m
           JOIN mem_vec v ON v.mem_id = m.id AND v.model=?
          WHERE m.status='active' AND m.id != ?""",
        (EMBED_MODEL, new_id)).fetchall()
    scored = []
    for c in cands:
        if not c["vec"]:
            continue
        try:
            s = cosine(v, unpack_vec(c["vec"]))
        except struct.error:
            continue
        if s >= min_sim:
            scored.append((s, c["id"]))
    scored.sort(reverse=True)
    # never auto-link to records already linked explicitly
    linked = set(parse_ids(db.execute(
        "SELECT related FROM memories WHERE id=?", (new_id,)
    ).fetchone()[0] or ""))
    return [i for _, i in scored if i not in linked][:top]


def embed_missing_quiet():
    """Best-effort: embed vectors for records missing them, but only while the
    remote server is already answering. remember/update/forget/restore DO
    call this (3 s health probe, immediate fill when alive) - the old comment
    claimed writes never probed (audit 16.11); when the remote is down
    everything degrades to FTS and vectors fill on a later run."""
    import contextlib
    import io

    if not EMBED_REMOTE:
        return
    # Health (3s) instead of a bare TCP probe: adb forward listens even when
    # the phone-side llama is dead - only /health tells the truth. A sleeping
    # remote answers /health immediately and wakes on the embeddings call.
    if not server_ready(timeout=3):
        return
    class _A:
        force = False
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            cmd_embed(_A())
    except Exception:
        return  # server went down mid-run: recall degrades to FTS


def _pid_alive(pid):
    """Windows-safe liveness probe. NEVER os.kill(pid, 0) here: on Windows
    it maps to TerminateProcess and would kill the lock owner (audit 16.11).
    A killed embed used to leave the lock behind until the TTL expired."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            # 0x00100000 = SYNCHRONIZE (the old 0x10000000 was GENERIC_ALL:
            # access-denied on a live owner made _pid_alive say "dead" and
            # the lock got stolen, round-2 audit)
            handle = k32.OpenProcess(0x00100000, False, pid)
            if not handle:
                return False
            try:
                # WAIT_TIMEOUT (0x102) = still running, WAIT_OBJECT_0 = dead
                return k32.WaitForSingleObject(handle, 0) == 0x102
            finally:
                k32.CloseHandle(handle)
        except Exception:
            return True               # can't tell -> respect the lock
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


@contextmanager
def embed_lock():
    """Interlock with Phone-Embed-Watchdog: a mass embed legitimately holds
    the phone busy for minutes (15.11: --force looked like a degraded
    cgroup and the watchdog respawned llama MID-REQUEST). The lock tells
    the watchdog to skip its latency probe/respawn while embed runs.
    Yields True when the caller may embed, False when a live owner holds
    the lock (caller must skip). Same file for records and artifacts -
    the watchdog only knows one lock path (sweep 17.11)."""
    lock = Path(str(INDEX_PATH) + ".embed.lock")
    # clamp: ttl <= 0 made every lock instantly expired -> a live embed could
    # be taken over by a concurrent one (round-2 audit)
    ttl = max(1.0, env_float("MEM_EMBED_LOCK_S", 3600))

    def _hold():
        """True while another run legitimately owns the lock."""
        try:
            if (time.time() - lock.stat().st_mtime) >= ttl:
                return False              # TTL expired: take over
            owner = int(lock.read_text(encoding="ascii").strip())
        except OSError:
            return False                  # vanished: nobody holds it
        except ValueError:
            return True                   # fresh but unreadable: respect it
        return owner < 0 or _pid_alive(owner)

    acquired = False
    broken = False
    try:
        # atomic O_EXCL create: the old exists()+write_text let two racers
        # both pass and both embed (round-2 audit)
        for _ in range(3):
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode("ascii"))
                os.close(fd)
                acquired = True
                break
            except FileExistsError:
                if _hold():
                    break                 # a live owner keeps it
                try:
                    lock.unlink()         # stale (dead owner / expired)
                except OSError:
                    pass
    except OSError:
        broken = True                     # lock dir not writable: run unlocked
    try:
        yield acquired or broken
    finally:
        if acquired:
            try:
                lock.unlink()
            except OSError:
                pass


def _pending_rows(db, force=False):
    """Record rows still needing a vector (all active rows with force)."""
    sql = "SELECT id, path, content, updated_at FROM memories WHERE status='active'"
    params = []
    if not force:
        sql += (" AND id NOT IN (SELECT mem_id FROM mem_vec"
                " WHERE model=? AND updated_at=memories.updated_at)")
        params.append(EMBED_MODEL)
    return db.execute(sql, params).fetchall()


def _make_batches(items, text_of, max_items, max_chars):
    """Split items into batches capped BOTH by count and by combined text
    size. The phone embeds ~15-25 tok/s: the old fixed 4-item batch was
    ~30 KB / 9K tok = 6-10 min per request, blew the HTTP budget and every
    sweep tick died on 'timed out' (18.11). At least one item per batch -
    a single oversized record still goes alone (its own budget scales)."""
    batches, cur, cur_len = [], [], 0
    for it in items:
        n = len(text_of(it))
        if cur and (len(cur) >= max_items or cur_len + n > max_chars):
            batches.append(cur)
            cur, cur_len = [], 0
        cur.append(it)
        cur_len += n
    if cur:
        batches.append(cur)
    return batches


def _embed_rows(db, rows):
    """Batch-embed the given record rows into mem_vec. Caller must hold
    embed_lock(); db is NOT closed (shared with sweep). Returns (rc, done):
    rc=1 on deferral/failure - nothing is ever started locally."""
    done = 0
    # payload budget per request: default 6000 chars ~= 1.5K tok ~= 1-2 min
    # on the phone, while embed_budget() gives ~210 s - comfortable margin
    limit = env_int("MEM_EMBED_CHARS", 6000)
    batches = _make_batches(
        rows, lambda r: f"{r['path']}\n{r['content']}", EMBED_BATCH, limit)
    try:
        for batch in batches:
            # re-check health before every batch: a peer that accepted the
            # first request may hang mid-run (15.11); fail fast, fill later
            if not server_ready(timeout=3):
                print(f"embed: remote down after {done}/{len(rows)},"
                      " deferring the rest")
                return 1, done
            texts = [f"{r['path']}\n{r['content']}" for r in batch]
            vecs = embed_texts(texts)
            for r, v in zip(batch, vecs):
                db.execute(
                    "INSERT INTO mem_vec(mem_id,model,updated_at,vec) VALUES(?,?,?,?)"
                    " ON CONFLICT(mem_id) DO UPDATE SET model=excluded.model,"
                    " updated_at=excluded.updated_at, vec=excluded.vec",
                    (r["id"], EMBED_MODEL, r["updated_at"], pack_vec(v)),
                )
            done += len(batch)
            db.commit()
    except Exception as e:
        db.commit()
        print(f"embed: stopped after {done}/{len(rows)}: {e}")
        print(f"hint: check the remote server / MEM_EMBED_URL (now {EMBED_URL})"
              f" or run with vectors off")
        return 1, done
    total = db.execute("SELECT COUNT(*) c FROM mem_vec").fetchone()["c"]
    print(f"embed: {done} vectors written (db has {total}, model {EMBED_MODEL})")
    return 0, done


def cmd_embed(args):
    """Fill/refresh vectors in mem_vec via the remote server. Defers (rc=1)
    when the remote is down or vectors are disabled - they fill on a later
    run; nothing is ever started locally."""
    db = ensure_fresh(open_db())
    rows = _pending_rows(db, getattr(args, "force", False))
    if not rows:
        print("embed: nothing to embed (server not needed)")
        db.close()
        return 0
    if not ensure_server():
        print("embed: remote server unavailable (or vector search off),"
              " deferring")
        db.close()
        return 1
    with embed_lock() as locked:
        if not locked:
            print("embed: another run holds the lock, skipping")
            db.close()
            return 0
        rc, _done = _embed_rows(db, rows)
    db.close()
    return rc


def embed_artifacts(db):
    r"""Vectorize reports (logs/*.md) into art_vec, incremental on
    mtime+model; chunks of removed files are dropped. Caller must hold
    embed_lock(). Returns (chunks_written, deferred): deferred=True when the
    remote was down/died - a later sweep finishes the rest. Old vectors of a
    file are replaced only after ALL its chunks embedded, so a mid-file
    failure keeps the previous state (mtime mismatch => retried)."""
    root = artifacts_dir()
    if root is None:
        return 0, False
    try:
        files = [f for f in root.rglob("*")
                 if f.is_file() and f.suffix.lower() == ".md"]
    except OSError:
        files = []
    live = {}
    for f in files:
        try:
            rel = str(f.relative_to(root))
        except ValueError:
            continue
        if skip_artifact(rel):        # vendor dumps: never embed
            continue
        try:
            live[rel] = (f, int(f.stat().st_mtime))
        except OSError:
            continue
    stored = {}
    for row in db.execute(
            "SELECT relpath, mtime, model FROM art_vec GROUP BY relpath"):
        stored[row["relpath"]] = (row["mtime"], row["model"])
    gone = sorted(set(stored) - set(live))
    for rel in gone:                     # report deleted/renamed
        db.execute("DELETE FROM art_vec WHERE relpath=?", (rel,))
    if gone:
        db.commit()
        print(f"sweep: dropped vectors of {len(gone)} removed report(s)")
    # freshest first: even a timed-out run leaves the new reports indexed
    stale = sorted((rel for rel in live
                    if stored.get(rel) != (live[rel][1], EMBED_MODEL)),
                   key=lambda rel: -live[rel][1])
    if not stale:
        return 0, False
    if not ensure_server():
        print(f"sweep: remote unavailable - {len(stale)} report(s) deferred")
        return 0, True
    written = 0
    max_chunks = env_int("MEM_ART_MAX_CHUNKS", 256)
    for rel in stale:
        f, mt = live[rel]
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        chunks = artifact_chunks(text)[:max_chunks]
        if not chunks:                   # empty report: nothing to store
            db.execute("DELETE FROM art_vec WHERE relpath=?", (rel,))
            db.commit()
            continue
        vecs = []
        # chunks are ~MEM_ART_CHUNK chars each; batch by count AND payload
        # so one request never exceeds the phone's time budget (18.11)
        cbatch_size = max(1, env_int("MEM_EMBED_CHARS", 6000)
                          // max(1, env_int("MEM_ART_CHUNK", 1600)))
        for cbatch in _make_batches(chunks, lambda c: c,
                                    min(EMBED_BATCH, cbatch_size),
                                    env_int("MEM_EMBED_CHARS", 6000)):
            if not server_ready(timeout=3):
                print(f"sweep: remote down ({written} chunks done),"
                      " artifact vectors deferred")
                db.commit()
                return written, True
            try:
                vecs.extend(embed_texts(cbatch))
            except Exception as e:
                print(f"sweep: embed failed on logs/{rel}: {e}")
                db.commit()
                return written, True
        db.execute("DELETE FROM art_vec WHERE relpath=?", (rel,))
        for ci, (ct, v) in enumerate(zip(chunks, vecs)):
            db.execute(
                "INSERT INTO art_vec(relpath,chunk,mtime,model,vec,chunk_text)"
                " VALUES(?,?,?,?,?,?)",
                (rel, ci, mt, EMBED_MODEL, pack_vec(v), ct))
        db.commit()
        written += len(chunks)
        print(f"sweep: logs/{rel} -> {len(chunks)} chunk(s)")
    return written, False


def doctor_links(db):
    """Suggest related pairs the auto-linker missed: active records sharing
    a vector with cosine >= MEM_LINK_MIN (0.75) that are NOT linked in
    either direction. Writes logs/link-suggestions.md - deterministic
    content, rewritten only on change (a churned file would force art_vec
    re-embeds). Never edits records: the user applies a pair with
    `mem.py link <id> <ids>` (sweep 17.11). Returns (count, file_written)."""
    min_sim = env_float("MEM_LINK_MIN", 0.75)
    rows = db.execute(
        "SELECT v.mem_id, v.vec, m.path FROM mem_vec v"
        " JOIN memories m ON m.id=v.mem_id"
        " WHERE v.model=? AND m.status='active'",
        (EMBED_MODEL,)).fetchall()
    vecs = []
    for r in rows:
        try:
            vecs.append((r["mem_id"], r["path"], unpack_vec(r["vec"])))
        except struct.error:
            continue                     # corrupt blob: skip, never crash
    if len(vecs) < 2:
        return 0, False
    # one pass builds the bidirectional "already linked" map (A->B implies
    # B~A for the skip check) instead of links_of()'s per-id table scan
    rel_of = {}
    for row in db.execute("SELECT id, related FROM memories").fetchall():
        rel_of[row["id"]] = set(parse_ids(row["related"]))
    for a in list(rel_of):
        for b in list(rel_of.get(a, ())):
            rel_of.setdefault(b, set()).add(a)
    sugg = []
    for i in range(len(vecs)):
        for j in range(i + 1, len(vecs)):
            ia, pa, va = vecs[i]
            ib, pb, vb = vecs[j]
            if ib in rel_of.get(ia, ()) or ia in rel_of.get(ib, ()):
                continue
            c = cosine(va, vb)
            if c >= min_sim:
                sugg.append((c, ia, pa, ib, pb))
    sugg.sort(key=lambda s: (-s[0], s[1], s[3]))
    out = ["# link suggestions (doctor)",
           "",
           f"cosine >= {min_sim}, active records not linked yet. Apply with:",
           "`python tools\\mem.py link <id> <ids>` (no new version).",
           ""]
    for c, ia, pa, ib, pb in sugg:
        out.append(f"- {ia} ~ {ib} (cos {c:.2f})  {pa}  |  {pb}")
    if not sugg:
        out.append("no suggestions")
    text = "\n".join(out) + "\n"
    root = artifacts_dir()
    if root is None:
        return len(sugg), False          # artifacts off: count only
    try:
        path = root / "link-suggestions.md"
        try:
            old = path.read_text(encoding="utf-8")
        except OSError:
            old = None
        if old == text:
            return len(sugg), False
        path.write_text(text, encoding="utf-8", newline="\n")
    except OSError as e:
        print(f"doctor: cannot write suggestions: {e}")
        return len(sugg), False
    return len(sugg), True


def cmd_sweep(args):
    """Scheduled maintenance (wrapper runs it every 10 min, gated 9:00-24:00
    - the phone is off after midnight): 1) fill/refresh record vectors,
    2) doctor link suggestions, 3) vectorize changed reports. All three
    under ONE embed lock so the phone watchdog stays hands-off for the
    whole run. Doctor runs before artifacts so link-suggestions.md is
    vectorized in the same pass. rc=1 when something was deferred (remote
    down): nothing is started locally, a later run fills the rest."""
    db = ensure_fresh(open_db())
    with embed_lock() as locked:
        if not locked:
            print("sweep: another run holds the embed lock, skipping")
            db.close()
            return 0
        nrec = nart = 0
        rc = 0
        rows = _pending_rows(db)
        if rows:
            if ensure_server():
                rc, nrec = _embed_rows(db, rows)
            else:
                print(f"sweep: remote unavailable - {len(rows)}"
                      " record vector(s) deferred")
                rc = 1
        nsug, _changed = doctor_links(db)
        nart, deferred = embed_artifacts(db)
        if deferred:
            rc = 1
    db.close()
    print(f"sweep: done - records {nrec}, artifact chunks {nart},"
          f" link suggestions {nsug}, rc={rc}")
    return rc


def query_vec(query, no_vector):
    """Embed the recall query for rerank AND artifact vector search; None
    when vectors are off or the remote is down (never raises, never starts
    anything locally). Computed before the 0-hits branch too - the artifact
    fallback ranks by the same vector (sweep 17.11)."""
    if no_vector or not ensure_server():
        return None
    try:
        return embed_texts([query])[0]
    except Exception:
        return None


def cmd_recall(args):
    # validate the query BEFORE any work: an empty query used to run a 3 s
    # health probe + possible embed first, then bail (round-2 audit)
    q = fts_query(args.query)
    if not q:
        print("recall: empty query")
        return 1
    db = ensure_fresh(open_db())
    # Direct md edits bypass remember/update: refill stale/missing vectors
    # here, quietly (no-op fast when the remote is down or nothing changed).
    # --no-vector promises ZERO network calls, so no fill either (audit).
    if not args.no_vector:
        embed_missing_quiet()
    sql = """SELECT m.*, bm25(memories_fts) AS b
             FROM memories_fts JOIN memories m ON m.id = memories_fts.mem_id
             WHERE memories_fts MATCH ? AND m.status='active'"""
    params = [q]
    if args.type:
        sql += " AND m.type=?"
        params.append(args.type)
    if args.path:
        # rstrip BEFORE escaping: a trailing user % must become the wildcard
        # (escaped first it ended up as a dangling '\%' with no wildcard at
        # all -> '--path x%' silently returned 0 hits, round-2 audit);
        # '*' kept for backward compat with round-1 style prefixes.
        base = args.path.rstrip("*%")
        esc = base.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql += " AND m.path LIKE ? ESCAPE '\\'"
        params.append(esc + "%")
    rows = db.execute(sql, params).fetchall()
    if not rows:
        print("recall: 0 hits")
        # vector half needs the query embed even with 0 record hits
        if not args.no_artifacts and args.limit > 0:
            show_artifacts(args.query, args.limit, db,
                           query_vec(args.query, args.no_vector))
        db.close()
        return 0
    # Hybrid rerank: remote -> FTS. When the remote is down the caller gets
    # pure bm25/recency/importance and mode says so - no error.
    qvec = query_vec(args.query, args.no_vector)
    cos_by_id = {}
    if qvec is not None:
        # one IN query instead of one round-trip per hit (audit: N+1)
        ids = [r["id"] for r in rows]
        qmarks = ",".join("?" * len(ids))
        for vr in db.execute(
            f"SELECT mem_id, vec FROM mem_vec WHERE model=?"
            f" AND mem_id IN ({qmarks})",
            [EMBED_MODEL, *ids],
        ).fetchall():
            try:
                cos_by_id[vr["mem_id"]] = cosine(qvec, unpack_vec(vr["vec"]))
            except struct.error:
                continue
    bs = [r["b"] for r in rows]
    bmin, bmax = min(bs), max(bs)
    cs_vals = list(cos_by_id.values())
    cmin, cmax = (min(cs_vals), max(cs_vals)) if cs_vals else (0.0, 0.0)
    scored = []
    for r in rows:
        bn = 1.0 if bmax == bmin else (bmax - r["b"]) / (bmax - bmin)
        rn = recency(r["updated_at"])
        inn = min(max(r["importance"], 1), 5) / 5.0
        if r["id"] in cos_by_id:
            c = cos_by_id[r["id"]]
            if cmax == cmin:
                # <2 distinct cosine values: min-max normalization would hand
                # a free cn=1.0 to any lone vector even at cos=0.1 (audit
                # 16.11) - use the raw cosine clamped to [0,1] instead
                cn = min(1.0, max(0.0, c))
            else:
                cn = (c - cmin) / (cmax - cmin)
            score = (W_BM25_V * bn + W_RECENCY_V * rn
                     + W_IMPORTANCE_V * inn + W_COS * cn)
        else:
            score = W_BM25 * bn + W_RECENCY * rn + W_IMPORTANCE * inn
        scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    if args.no_vector:
        mode = "fts (--no-vector)"
    elif not EMBED_REMOTE:
        mode = "fts (vector search off)"
    elif qvec is None:
        mode = "fts (remote server down)"   # down, or died mid-embed
    elif not cos_by_id:
        mode = "fts (no vectors yet)"       # remote OK, corpus not embedded
    elif len(cos_by_id) == len(rows):
        mode = "hybrid (remote)"
    else:
        mode = f"hybrid partial {len(cos_by_id)}/{len(rows)}"

    budget = args.max_bytes
    shown = 0
    if args.json:
        out = []
        for score, r in scored[: args.limit]:
            out_ids, inc_ids = links_of(db, r["id"])
            # honor --max-bytes like the text branch (round-2 audit: json
            # used to dump full bodies regardless of the budget)
            body = r["content"]
            if budget <= 0:
                body = ""
            else:
                enc = body.encode("utf-8")
                if len(enc) > budget:
                    body = enc[:budget].decode("utf-8",
                                               errors="ignore") + " ...[truncated]"
                    budget = 0
                else:
                    budget -= len(enc)
            out.append({"id": r["id"], "at": f"{r['id']}@v{r['version']}",
                        "score": round(score, 4), "type": r["type"],
                        "path": r["path"], "file": r["file"],
                        "updated": r["updated_at"], "importance": r["importance"],
                        "related": out_ids, "related_from": inc_ids,
                        "content": body})
        print(json.dumps(out, ensure_ascii=False, indent=2))
        db.close()
        return 0
    for score, r in scored[: args.limit]:
        body = r["content"]
        if budget <= 0:
            print(f"\n[{score:.3f}] {r['id']}@v{r['version']} {r['type']} "
                  f"{r['path']} ({r['updated_at']}, imp {r['importance']}) "
                  f"-> {r['file']} [budget exhausted]")
            shown += 1          # a header was printed: count it (audit)
            continue
        encoded = body.encode("utf-8")
        if len(encoded) > budget:
            cut = encoded[:budget].decode("utf-8", errors="ignore")
            body = cut + " ...[truncated]"
            budget = 0
        else:
            budget -= len(encoded)
        print(f"\n[{score:.3f}] {r['id']}@v{r['version']} {r['type']} "
              f"{r['path']} ({r['updated_at']}, imp {r['importance']}) "
              f"-> {r['file']}")
        print(body)
        out_ids, inc_ids = links_of(db, r["id"])
        if out_ids or inc_ids:
            bits = []
            if out_ids:
                bits.append("-> " + ",".join(str(i) for i in out_ids))
            if inc_ids:
                bits.append("<- " + ",".join(str(i) for i in inc_ids))
            print(f"[related {'  '.join(bits)}]")
        shown += 1
    # top up with raw artifacts (reports in logs/*.md) when memory was thin
    if not args.no_artifacts and shown < args.limit:
        show_artifacts(args.query, args.limit - shown, db, qvec)
    print(f"\n-- {shown}/{len(rows)} hits, budget left {budget} B, mode: {mode}")
    db.close()
    return 0


def show_artifacts(query, limit, db=None, qvec=None):
    """Print keyword+vector hits from logs/*.md (reports/artifacts not in memory)."""
    if limit <= 0:
        return
    for path, score, snippet, src in search_artifacts(query, limit, db, qvec):
        print(f"\n[{score:.2f} artifact:{src}] logs/{path}")
        print(f"  {snippet}")


def slugify(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s or "record"


# Windows reserved device names: a file like CON.md/PRN/AUX/COM1 hangs or
# swallows writes on Windows (audit 16.11).
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL",
                 *(f"COM{i}" for i in range(1, 10)),
                 *(f"LPT{i}" for i in range(1, 10))}


def safe_stem(s):
    """--name goes straight into a path: '..\\..\\evil', 'a/b', 'CON' must not
    escape memory\\ nor break on Windows. Returns a bare stem or None."""
    s = str(s or "").strip().rstrip(". ")
    if not s or s in (".", ".."):
        return None
    if any(c in s for c in '/\\:*?"<>|') or any(ord(c) < 32 for c in s):
        return None
    if s.split(".")[0].upper() in _WIN_RESERVED:
        return None
    return s


def _under_mem(p):
    """True when p resolves inside MEM_DIR - find_file must never hand out
    an external path: callers write the file FIRST and only then crash in
    rel() (write-then-crash, audit 16.11)."""
    try:
        return p.resolve().is_relative_to(MEM_DIR.resolve())
    except (OSError, ValueError):
        return False


def find_file(db, target):
    # Citations copied from recall output look like "22@v1" - accept them.
    target = (target or "").strip()
    m = re.match(r"^(\d+)(?:@v\d+)?$", target)
    if m:
        r = db.execute("SELECT file FROM memories WHERE id=?",
                       (int(m.group(1)),)).fetchone()
        if r:
            p = MEM_DIR / r["file"]
            return p if p.is_file() else None
        # numeric target that does not exist: never fall through to LIKE -
        # 'update 999' used to mutate an unrelated file with '999' in its
        # name (audit 16.11)
        return None
    # exact matches first: path as given, with .md, then bare stem (keeps
    # 'record' from grabbing 'record-2' via the fuzzy fallback)
    for s in (target, target + ".md"):
        try:
            p = Path(s)
            if p.is_file() and _under_mem(p):
                # absolute: callers run rel() on the result; a raw relative
                # path crashed there (round-2 audit)
                return Path(os.path.abspath(p))
        except OSError:
            pass
        p2 = MEM_DIR / s
        if p2.is_file() and _under_mem(p2):
            return p2
        p3 = ROOT / s          # 'memory/semantic/x.md' from ANY cwd
        if p3.is_file() and _under_mem(p3):
            return p3
    # fuzzy fallback with LIKE metacharacters escaped: a literal '%' in
    # 'show 100%' must not match arbitrarily (audit 16.11)
    esc = target.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    cands = []
    for f in db.execute(
            "SELECT file FROM memories WHERE file LIKE ? ESCAPE '\\'",
            (f"%{esc}%",)).fetchall():
        p4 = MEM_DIR / f["file"]
        # same containment guard as every other branch: a poisoned 'file'
        # column must never hand out an external path (round-2 audit)
        if p4.is_file() and _under_mem(p4):
            cands.append(p4)
    if cands:
        # rank: exact stem first ('alpha' must pick alpha.md, not the
        # alphabetically-earlier alpha-v2.md that sorted LIKE returned
        # first), then shortest path (round-2 test)
        cands.sort(key=lambda p: (p.stem != target, len(p.parts), len(p.name)))
        return cands[0]
    return None


def bump_version(stem):
    """Only -vN is a version suffix. The bare -N branch is gone (audit
    16.11): it incremented dates ('notes-2026-11-15' -> '...-11-16', i.e.
    tomorrow's file) and other trailing numbers ('report-2026' ->
    'report-2027'), and could collide with an existing file."""
    m = re.match(r"^(.*?)-v(\d+)$", stem)
    if m:
        return f"{m.group(1)}-v{int(m.group(2)) + 1}"
    return stem + "-v2"


def resolve_text(args, cmd):
    """Record text: positional arg or --file PATH (UTF-8, BOM ok, CRLF ok).
    --file dodges shell quoting: PowerShell breaks on $ and nested quotes in
    long inline strings - write the text to a file, pass --file PATH."""
    f = getattr(args, "file", None)
    t = getattr(args, "text", None)
    if f and t is not None:
        print(f"{cmd}: pass either text or --file, not both", file=sys.stderr)
        return None
    if f:
        try:
            if f == "-":
                data = sys.stdin.read()
            else:
                data = Path(f).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as e:
            print(f"{cmd}: cannot read --file {f}: {e}", file=sys.stderr)
            return None
        return data.replace("\r\n", "\n")
    if t is None:
        print(f"{cmd}: no text (pass text or --file PATH)", file=sys.stderr)
        return None
    return t


def cmd_remember(args):
    text = resolve_text(args, "remember")
    if text is None:
        return 2
    fm = {"type": args.type, "path": args.path, "status": "active", "version": "1",
          "author": args.author, "updated": today(), "importance": str(args.importance)}
    if args.related:
        fm["related"] = ",".join(str(i) for i in parse_ids(args.related))
    if args.name:
        name = safe_stem(args.name)
        if name is None:
            print(f"remember: bad --name (bare stem only, no path separators"
                  f" or reserved names): {args.name!r}", file=sys.stderr)
            return 2
    else:
        name = slugify(args.path.split("/")[-1])
    # frontmatter values go into `key: value` lines raw: a newline in
    # --author/--path would split the record and corrupt the file
    # (round-2 audit)
    for k, v in (("type", args.type), ("path", args.path),
                 ("author", args.author)):
        if "\n" in str(v) or "\r" in str(v):
            print(f"remember: {k} must not contain newlines: {v!r}",
                  file=sys.stderr)
            return 2
    if args.type == "episodic":
        sub = MEM_DIR / "episodic" / today()[:7]
    else:
        sub = MEM_DIR / args.type
    sub.mkdir(parents=True, exist_ok=True)
    # reserve the name atomically (O_EXCL): the old exists()-loop let two
    # concurrent same-name remembers both pass and the second os.replace
    # silently overwrote the first (round-2 audit)
    n = 2
    while True:
        cand = sub / (f"{name}.md" if n == 2 else f"{name}-{n}.md")
        try:
            fd = os.open(cand, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            n += 1
            if n >= 1000:
                print(f"remember: no free name slot in {sub}", file=sys.stderr)
                return 2
            continue
        os.close(fd)
        break
    path = cand
    write_md(path, fm, text)
    print(f"remember: wrote {rel(path)}")
    cmd_sync(args)
    embed_missing_quiet()
    db = open_db()
    try:
        r = db.execute("SELECT id,version FROM memories WHERE file=?",
                       (rel(path),)).fetchone()
        if r:
            print(f"citation: {r['id']}@v{r['version']}")
            if not args.related and not args.no_auto_related:
                ids = auto_related(db, r["id"])
                if ids:
                    parsed = parse_md(path)
                    if parsed:
                        fm2, body2 = parsed
                        fm2["related"] = ",".join(str(i) for i in ids)
                        write_md(path, fm2, body2)
                        db.execute("UPDATE memories SET related=? WHERE file=?",
                                   (",".join(str(i) for i in ids), rel(path)))
                        db.commit()
                        print(f"related auto: {','.join(str(i) for i in ids)}")
    finally:
        db.close()
    return 0


def cmd_update(args):
    text = resolve_text(args, "update")
    if text is None:
        return 2
    db = open_db()
    try:
        old = find_file(db, args.target)
        if not old:
            print(f"update: not found: {args.target}")
            return 1
        parsed = parse_md(old)
        if not parsed:
            print("update: bad frontmatter")
            return 1
        fm, _ = parsed
        old_rel = rel(old)
        # only an active record may be superseded: updating a superseded or
        # deleted target forked the version chain into two active siblings
        # (round-2 audit)
        if fm.get("status", "active") != "active":
            print(f"update: only an active record can be updated "
                  f"({old_rel} is {fm.get('status', 'active')}; use restore "
                  f"for a tombstone)")
            return 1
        ver = parse_version(fm.get("version", "1"))
        new_fm = dict(fm)
        # P1: the new version is a NEW row with its own id - copying the old
        # id would make two files claim one id (sync would reassign it)
        new_fm.pop("id", None)
        new_fm["supersedes"] = old_rel
        new_fm["updated"] = today()
        new_fm["status"] = "active"
        if args.importance:
            new_fm["importance"] = str(args.importance)
        if args.related is not None:
            ids = ",".join(str(i) for i in parse_ids(args.related))
            if ids:
                new_fm["related"] = ids
            else:
                new_fm.pop("related", None)
        # bump until the target name is free: a stale 'x-v2' left on disk
        # (reverted edit, partial copy) must not abort the update (audit)
        stem = old.stem
        bumps = 0
        while True:
            stem = bump_version(stem)
            bumps += 1
            new_path = old.with_name(stem + ".md")
            if not new_path.exists():
                break
            if bumps >= 100:
                print(f"update: no free version slot for {old_rel}",
                      file=sys.stderr)
                return 1
        new_fm["version"] = str(ver + bumps)
        write_md(new_path, new_fm, text)

        old_fm = dict(fm)
        old_fm["status"] = "superseded"
        write_md(old, old_fm, parsed[1])
        print(f"update: {old_rel} -> superseded; wrote {rel(new_path)} "
              f"@v{ver + bumps}")
    finally:
        db.close()
    cmd_sync(args)
    embed_missing_quiet()
    db = open_db()
    try:
        r = db.execute("SELECT id,version FROM memories WHERE file=?",
                       (rel(new_path),)).fetchone()
        if r:
            print(f"citation: {r['id']}@v{r['version']}")
    finally:
        db.close()
    return 0


def set_status(args, status):
    db = open_db()
    try:
        f = find_file(db, args.target)
        if not f:
            print(f"not found: {args.target}")
            return 1
        parsed = parse_md(f)
        if not parsed:
            print("bad frontmatter")
            return 1
        fm, body = parsed
        cur = fm.get("status", "active")
        if status == "active" and cur != "deleted":
            # restore resurrects tombstones only: re-activating an active or
            # superseded record would corrupt the version chain (audit 16.11)
            print(f"restore: only a deleted record can be restored "
                  f"({rel(f)} is {cur})")
            return 1
        fm["status"] = status
        if status == "active":
            # a tombstoned record may have a newer active child (forget of a
            # superseded version): re-activating the parent would leave TWO
            # active versions in the chain (round-2 audit)
            row = db.execute("SELECT id FROM memories WHERE file=?",
                             (rel(f),)).fetchone()
            if row:
                child = db.execute(
                    "SELECT file FROM memories WHERE parent_id=?"
                    " AND status='active'", (row["id"],)).fetchone()
                if child:
                    print(f"restore: refused - a newer active version exists "
                          f"({child['file']}); restoring {rel(f)} would leave "
                          f"two active versions")
                    return 1
        write_md(f, fm, body)
        print(f"{status}: {rel(f)}")
    finally:
        db.close()
    cmd_sync(args)
    embed_missing_quiet()
    return 0


def cmd_forget(args):
    return set_status(args, "deleted")


def cmd_restore(args):
    return set_status(args, "active")


def cmd_list(args):
    db = ensure_fresh(open_db())
    try:
        sql = "SELECT * FROM memories WHERE 1=1"
        params = []
        if args.status:
            sql += " AND status=?"
            params.append(args.status)
        if args.type:
            sql += " AND type=?"
            params.append(args.type)
        sql += " ORDER BY updated_at DESC, id DESC"
        for r in db.execute(sql, params):
            print(f"{r['id']:>4} v{r['version']} {r['status']:<10} "
                  f"{r['type']:<10} {r['updated_at']} imp{r['importance']}  "
                  f"{r['path']}  <- {r['file']}")
    finally:
        db.close()
    return 0


def cmd_show(args):
    db = ensure_fresh(open_db())
    f = find_file(db, args.target)
    if not f:
        print(f"not found: {args.target}")
        db.close()
        return 1
    r = db.execute("SELECT * FROM memories WHERE file=?",
                   (rel(f),)).fetchone()
    if not r:
        print("not in db, run sync")
        db.close()
        return 1
    print(f"{r['id']}@v{r['version']} [{r['status']}] {r['type']} {r['path']} "
          f"({r['updated_at']}, imp {r['importance']}, author {r['author']})")
    print(f"file: {r['file']}")
    if r["parent_id"]:
        print(f"supersedes id: {r['parent_id']}")
    out, inc = links_of(db, r["id"])
    if out:
        print(f"related -> {', '.join(f'{i}@v' + str(ver_of(db, i)) for i in out)}")
    if inc:
        print(f"related <- {', '.join(f'{i}@v' + str(ver_of(db, i)) for i in inc)}")
    print()
    print(r["content"])
    db.close()
    return 0


def ver_of(db, mem_id):
    r = db.execute("SELECT version FROM memories WHERE id=?",
                   (mem_id,)).fetchone()
    return r["version"] if r else "?"


def cmd_link(args):
    """Add/remove related ids in frontmatter WITHOUT a new version.
    Links are metadata, not content: no version bump, no re-embed."""
    db = ensure_fresh(open_db())
    f = find_file(db, args.target)
    if not f:
        print(f"link: not found: {args.target}")
        db.close()
        return 1
    parsed = parse_md(f)
    if not parsed:
        print("link: bad frontmatter")
        db.close()
        return 1
    if not args.ids:
        if args.off:
            print("link: give ids to remove: mem.py link 14 --off 15")
            db.close()
            return 1
        db.close()               # cmd_links opens its own connection
        return cmd_links(args)   # bare `link <id>` = show (git remote pattern)
    fm, body = parsed
    cur = parse_ids(fm.get("related", ""))
    want = parse_ids(" ".join(args.ids))
    if args.off:
        new = [i for i in cur if i not in want]
    else:
        new = cur + [i for i in want if i not in cur]
    if new:
        fm["related"] = ",".join(str(i) for i in new)
    else:
        fm.pop("related", None)
    write_md(f, fm, body)
    r = db.execute("SELECT id FROM memories WHERE file=?",
                   (rel(f),)).fetchone()
    if not r:
        db.close()
        cmd_sync(args)
        return 0
    # content untouched -> vectors stay valid; only refresh the related column
    db.execute("UPDATE memories SET related=? WHERE file=?",
               (",".join(str(i) for i in new), rel(f)))
    db.commit()
    known = {row["id"] for row in db.execute("SELECT id FROM memories")}
    db.close()
    print(f"link: {rel(f)} related = {','.join(str(i) for i in new) or '(none)'}")
    unknown = [i for i in want if i not in known]
    if unknown:
        print(f"link: warning: id(s) not in db yet: "
              f"{', '.join(str(i) for i in unknown)}")
    return 0


def cmd_links(args):
    db = ensure_fresh(open_db())
    f = find_file(db, args.target)
    if not f:
        print(f"links: not found: {args.target}")
        db.close()
        return 1
    r = db.execute("SELECT * FROM memories WHERE file=?",
                   (rel(f),)).fetchone()
    if not r:
        print("not in db, run sync")
        db.close()
        return 1
    out, inc = links_of(db, r["id"])
    print(f"{r['id']}@v{r['version']} {r['type']} {r['path']}")
    if not out and not inc:
        print("  no links (add frontmatter: related: <id>, <id>)")
        db.close()
        return 0
    for i in out:
        t = db.execute("SELECT * FROM memories WHERE id=?", (i,)).fetchone()
        if t:
            print(f"  -> {i}@v{t['version']} [{t['status']}] {t['type']} "
                  f"{t['path']}  {one_line(t['content'], 60)}")
        else:
            print(f"  -> {i} [missing]")
    for i in inc:
        t = db.execute("SELECT * FROM memories WHERE id=?", (i,)).fetchone()
        if t:
            print(f"  <- {i}@v{t['version']} [{t['status']}] {t['type']} "
                  f"{t['path']}  {one_line(t['content'], 60)}")
        else:
            print(f"  <- {i} [missing]")
    db.close()
    return 0


def one_line(text, width=90):
    for line in text.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:width] + ("..." if len(line) > width else "")
    return ""


def write_index(db):
    rows = db.execute(
        "SELECT * FROM memories WHERE status!='deleted' "
        "ORDER BY type, updated_at DESC"
    ).fetchall()
    out = [
        f"# MEMORY.md - memory index (regenerated by: python {SCRIPT_REF} index)",
        "",
        "> Источник истины: файлы memory\\*.md (frontmatter XMemo). "
        "memory.db - поисковый индекс (mem.py sync), пересобираемый из файлов.",
        f"> Поиск: `python {SCRIPT_REF} recall \"запрос\"` "
        "(bm25 + recency + importance, active-only).",
        "",
    ]
    for t in ("semantic", "episodic", "procedural", "working", "identity"):
        trows = [r for r in rows if r["type"] == t]
        if not trows:
            continue
        out.append(f"## {t} ({len(trows)})")
        out.append("")
        out.append("| id@v | path | status | updated | факт |")
        out.append("|---|---|---|---|---|")
        for r in trows:
            # escape '|' and flatten newlines: record text goes into a md
            # table cell - an unescaped pipe broke the whole index table
            # (status/updated_at too: a hand-edited `status: a|b` used to
            # shift every column, round-2 audit)
            cell = (r["content"].replace("|", "\\|").replace("\n", " "))
            esc = lambda s: str(s).replace("|", "\\|").replace("\n", " ")
            out.append(f"| {r['id']}@v{r['version']} "
                       f"| {esc(r['path'])} "
                       f"| {esc(r['status'])} | {esc(r['updated_at'])} "
                       f"| {one_line(cell)} |")
        out.append("")
    # atomic like write_md: a crash mid-write used to leave a truncated
    # MEMORY.md behind (round-2 audit)
    tmp = Path(str(INDEX_PATH) + ".tmp-write")
    try:
        tmp.write_text("\n".join(out), encoding="utf-8", newline="\n")
        os.replace(tmp, INDEX_PATH)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return len(rows)


def cmd_index(args):
    # ensure_fresh like every other reader: on a fresh deploy `index` used to
    # overwrite MEMORY.md with an empty header (round-2 audit)
    db = ensure_fresh(open_db())
    n = write_index(db)
    print(f"index: {n} records -> {INDEX_PATH}")
    db.close()
    return 0


# ---- MEMv2: MCP stdio server (JSON-RPC 2.0 over stdin/stdout) ----
# One process, no deps: agents call memory as native tools instead of shell
# invocations (no quoting pitfalls). `mem.py serve` replaces a separate
# MCP server project (XMemo contract: remember/recall/search/update_memory/
# forget + restore/list/show/link).
MCP_PROTOCOL = "2024-11-05"
MCP_SERVER_INFO = {"name": "mem", "version": "2.0.0"}
MCP_INSTRUCTIONS = (
    "XMemo local memory over md files + SQLite index. remember writes a new "
    "record (returns citation id@vN); recall/search finds active records "
    "(bm25+recency+importance+cosine); update supersedes a record (old -> "
    "superseded, new version); forget tombstones; link relates records "
    "without a new version. Prefer recall before writing to avoid duplicates."
)
MCP_TYPES = ["semantic", "episodic", "procedural", "working", "identity"]

MCP_REQUIRED = {
    "remember": ("text", "type", "path"),
    "recall": ("query",),
    "search": ("query",),
    "update_memory": ("target", "text"),
    "forget": ("target",),
    "restore": ("target",),
    "show": ("target",),
    "link": ("target",),
}


def _mcp_tools():
    type_prop = {"type": "string", "enum": MCP_TYPES}
    target_prop = {"type": "string", "description": "record id, citation (12@v1) or file"}
    return [
        {"name": "remember",
         "description": "Write a new memory record (md file + index). Returns "
                        "citation id@vN and auto-links related records.",
         "inputSchema": {"type": "object", "properties": {
             "text": {"type": "string", "description": "record content (markdown)"},
             "type": dict(type_prop, description="record type"),
             "path": {"type": "string",
                      "description": "namespace, e.g. memory/semantic/x"},
             "name": {"type": "string", "description": "file name stem"},
             "importance": {"type": "integer", "minimum": 1, "maximum": 5,
                            "default": 3},
             "author": {"type": "string", "default": "opencode"},
             "related": {"type": "string",
                         "description": "linked record ids, e.g. '12, 15'"},
             "no_auto_related": {"type": "boolean", "default": False},
         }, "required": ["text", "type", "path"]}},
        {"name": "recall",
         "description": "Search active memory records (hybrid ranking) plus "
                        "logs/*.md artifacts. Use before writing to avoid duplicates.",
         "inputSchema": {"type": "object", "properties": {
             "query": {"type": "string"},
             "limit": {"type": "integer", "default": 10},
             "type": dict(type_prop, description="filter by record type"),
             "path": {"type": "string", "description":"namespace prefix filter"},
             "no_vector": {"type": "boolean", "default": False,
                           "description": "pure FTS5, no embedding server calls"},
             "no_artifacts": {"type": "boolean", "default": False},
         }, "required": ["query"]}},
        {"name": "search",
         "description": "Alias of recall (XMemo contract name).",
         "inputSchema": {"type": "object", "properties": {
             "query": {"type": "string"},
             "limit": {"type": "integer", "default": 10},
         }, "required": ["query"]}},
        {"name": "update_memory",
         "description": "Supersede a record with new content: old version -> "
                        "superseded, new version becomes active.",
         "inputSchema": {"type": "object", "properties": {
             "target": dict(target_prop),
             "text": {"type": "string", "description": "new full content"},
             "importance": {"type": "integer", "minimum": 1, "maximum": 5},
             "related": {"type": "string", "description": "replace related ids"},
         }, "required": ["target", "text"]}},
        {"name": "forget",
         "description": "Tombstone a record (status=deleted); recall stops returning it.",
         "inputSchema": {"type": "object", "properties": {
             "target": dict(target_prop),
         }, "required": ["target"]}},
        {"name": "restore",
         "description": "Restore a tombstoned record back to active.",
         "inputSchema": {"type": "object", "properties": {
             "target": dict(target_prop),
         }, "required": ["target"]}},
        {"name": "list",
         "description": "List memory records with status/type filters.",
         "inputSchema": {"type": "object", "properties": {
             "status": {"type": "string",
                        "enum": ["active", "superseded", "deleted"]},
             "type": dict(type_prop),
         }}},
        {"name": "show",
         "description": "Show one record in full (frontmatter, links, content).",
         "inputSchema": {"type": "object", "properties": {
             "target": dict(target_prop),
         }, "required": ["target"]}},
        {"name": "link",
         "description": "Show or edit related links of a record (no new version).",
         "inputSchema": {"type": "object", "properties": {
             "target": dict(target_prop),
             "ids": {"type": "array", "items": {"type": "string"},
                     "description": "related ids to add"},
             "off": {"type": "boolean", "default": False,
                     "description": "remove these ids instead of adding"},
         }, "required": ["target"]}},
    ]


def _mcp_dispatch(name, a):
    """Run a tool: build the CLI namespace, capture stdout/stderr, map rc."""
    import contextlib
    import io

    missing = [k for k in MCP_REQUIRED.get(name, ()) if not a.get(k)]
    if missing:
        return f"missing required argument(s): {', '.join(missing)}", True

    try:
        if name == "remember":
            # type becomes a directory name (MEM_DIR/type): an arbitrary
            # string would escape memory\ (audit 16.11) - enum only
            if a.get("type") not in MCP_TYPES:
                return (f"invalid type: {a.get('type')!r} "
                        f"(expected one of {', '.join(MCP_TYPES)})", True)
            imp = (int(a["importance"]) if a.get("importance") is not None
                   else 3)
            if not 1 <= imp <= 5:
                return f"invalid importance: {imp} (1-5)", True
            ns = argparse.Namespace(
                text=a.get("text"), file=None, type=a.get("type"),
                path=a.get("path"), name=a.get("name"),
                importance=imp,
                author=a.get("author") or "opencode",
                related=a.get("related") or "",
                no_auto_related=bool(a.get("no_auto_related")))
            fn = cmd_remember
        elif name in ("recall", "search"):
            ns = argparse.Namespace(
                query=a.get("query"),
                # explicit None check: limit:0 must stay 0, not become 10
                limit=(DEFAULT_LIMIT if a.get("limit") is None
                       else max(0, int(a["limit"]))),
                max_bytes=DEFAULT_MAX_BYTES, type=a.get("type"),
                path=a.get("path"), json=False,
                no_vector=bool(a.get("no_vector")),
                no_artifacts=bool(a.get("no_artifacts")))
            fn = cmd_recall
        elif name == "update_memory":
            if a.get("importance") is not None:
                imp = int(a.get("importance"))
                if not 1 <= imp <= 5:
                    return f"invalid importance: {imp} (1-5)", True
            ns = argparse.Namespace(
                target=a.get("target"), text=a.get("text"), file=None,
                importance=a.get("importance"), related=a.get("related"))
            fn = cmd_update
        elif name in ("forget", "restore"):
            ns = argparse.Namespace(target=a.get("target"))
            fn = cmd_forget if name == "forget" else cmd_restore
        elif name == "list":
            if a.get("status") is not None and a.get("status") not in (
                    "active", "superseded", "deleted"):
                return f"invalid status: {a.get('status')!r}", True
            ns = argparse.Namespace(status=a.get("status"), type=a.get("type"))
            fn = cmd_list
        elif name == "show":
            ns = argparse.Namespace(target=a.get("target"))
            fn = cmd_show
        elif name == "link":
            ns = argparse.Namespace(target=a.get("target"),
                                    ids=[str(i) for i in (a.get("ids") or [])],
                                    off=bool(a.get("off")))
            fn = cmd_link
        else:
            return f"unknown tool: {name}", True
    except (TypeError, ValueError) as e:
        # bad typed arg (limit="abc", importance="high") -> clean tool error,
        # not a JSON-RPC -32603 internal fault (audit 16.11)
        return f"invalid arguments for {name}: {e}", True

    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fn(ns)
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 1
    except Exception as e:  # never kill the server on a tool error
        return f"{type(e).__name__}: {e}", True
    text = out.getvalue()
    if rc not in (0, None):
        extra = err.getvalue()
        if extra and extra not in text:
            text = text + ("" if not text or text.endswith("\n") else "\n") + extra
        return text or f"error (rc={rc})", True
    return text or err.getvalue() or "ok", False


def _mcp_send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def cmd_serve(args):
    """MCP stdio server: newline-delimited JSON-RPC 2.0 on stdin/stdout."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    # Isolation: the root follows the client project, not this script.
    set_root(mcp_root())
    print(f"mem serve: root={ROOT}", file=sys.stderr)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _mcp_send({"jsonrpc": "2.0", "id": None,
                       "error": {"code": -32700, "message": "parse error"}})
            continue
        # a JSON array/string/number is not a request object: msg.get used to
        # raise AttributeError and kill the whole server (audit 16.11)
        if not isinstance(msg, dict):
            _mcp_send({"jsonrpc": "2.0", "id": None,
                       "error": {"code": -32600,
                                 "message": "invalid request (not an object)"}})
            continue
        mid = msg.get("id")
        method = msg.get("method")
        if not isinstance(method, str):
            # missing method: malformed request (we never send server->client
            # requests, so a stray response has nothing to answer to)
            if mid is not None:
                _mcp_send({"jsonrpc": "2.0", "id": mid,
                           "error": {"code": -32600,
                                     "message": "invalid request: "
                                                "missing method"}})
            continue
        try:
            if method == "initialize":
                ip = msg.get("params")
                ip = ip if isinstance(ip, dict) else {}
                ver = ip.get("protocolVersion") or MCP_PROTOCOL
                _mcp_send({"jsonrpc": "2.0", "id": mid, "result": {
                    "protocolVersion": ver,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": MCP_SERVER_INFO,
                    "instructions": MCP_INSTRUCTIONS}})
            elif method == "ping":
                _mcp_send({"jsonrpc": "2.0", "id": mid, "result": {}})
            elif method == "tools/list":
                _mcp_send({"jsonrpc": "2.0", "id": mid,
                           "result": {"tools": _mcp_tools()}})
            elif method == "tools/call":
                p = msg.get("params")
                if not isinstance(p, dict):
                    # non-dict params is -32602 (invalid params), not a
                    # -32603 internal fault from a crashing .get (round-2 audit)
                    _mcp_send({"jsonrpc": "2.0", "id": mid, "error": {
                        "code": -32602,
                        "message": "invalid params (object expected)"}})
                    continue
                args_ = p.get("arguments")
                args_ = args_ if isinstance(args_, dict) else {}
                text, is_err = _mcp_dispatch(p.get("name"), args_)
                _mcp_send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": text}],
                    "isError": is_err}})
            elif method.startswith("notifications/"):
                pass  # initialized / cancelled: no response
            elif mid is None:
                pass  # unknown notification: swallow
            else:
                _mcp_send({"jsonrpc": "2.0", "id": mid, "error": {
                    "code": -32601, "message": f"method not found: {method}"}})
        except Exception as e:
            if mid is not None:
                try:
                    _mcp_send({"jsonrpc": "2.0", "id": mid, "error": {
                        "code": -32603, "message": f"{type(e).__name__}: {e}"}})
                except (OSError, ValueError):
                    # double fault: stdout is gone - nothing left to serve
                    return 1
    return 0


def main():
    ap = argparse.ArgumentParser(prog="mem.py", description="XMemo local memory CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("sync", help="rebuild memory.db from memory\\*.md")

    p = sub.add_parser("embed", help="fill embedding vectors (needs embedding server)")
    p.add_argument("--force", action="store_true", help="re-embed all records")

    sub.add_parser("sweep",
                   help="scheduled maintenance: record vectors + link "
                        "suggestions + artifact vectors (one embed lock; "
                        "rc=1 = deferred, run again later)")

    p = sub.add_parser("recall", help="search active memories")
    p.add_argument("query")
    p.add_argument("--limit", type=lambda v: max(0, int(v)),
                   default=DEFAULT_LIMIT,
                   help=f"max hits (default {DEFAULT_LIMIT})")
    p.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    p.add_argument("--type", choices=["semantic", "episodic", "procedural",
                                      "working", "identity"])
    p.add_argument("--path", help="namespace prefix, e.g. memory/semantic")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-vector", action="store_true",
                   help="skip embedding rerank (pure FTS5, no server calls)")
    p.add_argument("--no-artifacts", action="store_true",
                   help="skip logs/*.md artifact fallback search")

    p = sub.add_parser("remember", help="write a new memory file + index it")
    p.add_argument("text", nargs="?", help="memory text (or use --file PATH)")
    p.add_argument("--file", metavar="PATH",
                   help="read text from file (UTF-8; '-' = stdin), avoids shell quoting")
    p.add_argument("--type", required=True,
                   choices=["semantic", "episodic", "procedural", "working", "identity"])
    p.add_argument("--path", required=True, help="namespace, e.g. memory/semantic/x")
    p.add_argument("--name", help="file name stem")
    p.add_argument("--importance", type=int, default=3)
    p.add_argument("--author", default="opencode")
    p.add_argument("--related", default="",
                   help="linked record ids, e.g. '12, 15' (bug -> fix guide)")
    p.add_argument("--no-auto-related", action="store_true",
                   help="skip cosine top-N auto-linking")

    p = sub.add_parser("update", help="supersede a memory with new content")
    p.add_argument("target", help="file, relative path or id")
    p.add_argument("text", nargs="?", help="new text (or use --file PATH)")
    p.add_argument("--file", metavar="PATH",
                   help="read text from file (UTF-8; '-' = stdin), avoids shell quoting")
    p.add_argument("--importance", type=int)
    p.add_argument("--related", default=None,
                   help="replace related ids (keeps old ones if omitted)")

    p = sub.add_parser("forget", help="tombstone (status=deleted)")
    p.add_argument("target")

    p = sub.add_parser("restore", help="restore tombstoned memory")
    p.add_argument("target")

    p = sub.add_parser("list", help="list records")
    p.add_argument("--status", choices=["active", "superseded", "deleted"])
    p.add_argument("--type", choices=["semantic", "episodic", "procedural",
                                      "working", "identity"])

    p = sub.add_parser("show", help="show one record in full")
    p.add_argument("target")

    p = sub.add_parser("link", aliases=["links"],
                       help="show related records; add/remove ids "
                            "(no new version)")
    p.add_argument("target")
    p.add_argument("ids", nargs="*",
                   help="ids to link, e.g. 12 15 (none = show)")
    p.add_argument("--off", action="store_true", help="remove these ids")

    sub.add_parser("index", help="regenerate MEMORY.md from db")

    sub.add_parser("serve",
                   help="MCP stdio server (MEMv2): expose memory tools to agents")

    args = ap.parse_args()
    if args.cmd == "sync":
        return cmd_sync(args)
    if args.cmd == "embed":
        return cmd_embed(args)
    if args.cmd == "sweep":
        return cmd_sweep(args)
    if args.cmd == "recall":
        return cmd_recall(args)
    if args.cmd == "remember":
        return cmd_remember(args)
    if args.cmd == "update":
        return cmd_update(args)
    if args.cmd == "forget":
        return cmd_forget(args)
    if args.cmd == "restore":
        return cmd_restore(args)
    if args.cmd == "list":
        return cmd_list(args)
    if args.cmd == "show":
        return cmd_show(args)
    if args.cmd in ("link", "links"):   # `links` kept as alias
        return cmd_link(args)
    if args.cmd == "index":
        return cmd_index(args)
    if args.cmd == "serve":
        return cmd_serve(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
