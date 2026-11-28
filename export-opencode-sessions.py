# export_opencode_sessions.py: incremental export of opencode.db session
# DIGESTS into mem's artifact tree (<root>\logs\sessions\*.md) so recall can
# answer "where/when did we discuss X" (probe verdict 100@v1, 28.11.2026).
#
# UNIVERSAL / per-project scope (user decision 28.11): a project exports ONLY
# sessions whose session_v2.directory is under ITS OWN root - OLAMA indexes
# OLAMA, MEM indexes MEM, no cross-project noise in recall. The root defaults
# to the parent of the script's directory (<project>\tools\..), so the file is
# drop-in for propagation; override with argv[1].
#
# Scope of each session (measured, NOT the whole 8.5 GB DB = 155 phone-days):
#   - title + project dir
#   - first + last user message (capped 1500; the user bucket is 79% giant
#     pastes, one row measured at 60 M chars)
#   - LAST compaction per session (capped 3000; digest the model already made)
# Excluded by measurement: synthetic (tool-echo, 84% = 57 giant pastes),
# assistant (689 M incl. tool output), tool/reasoning parts, credentials.
#
# Read-only (mode=ro, no immutable: live WAL), never touches credential /
# kv / instruction tables. Writes only when content changed (mtime-driven
# embed in sweep then picks up just the delta), and DELETES stale *.md in the
# project's own output dir (session gone / out of scope / renamed).
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB = Path(r"C:\Users\000\.local\share\opencode\opencode.db")
CAP_FIRST = 1500
CAP_LAST = 1500
CAP_COMPACTION = 3000


def cut(text, n):
    text = (text or "").strip()
    if len(text) <= n:
        return text
    return text[:n] + "\n\n...[cut by exporter: %d of %d chars]" % (
        n, len(text))


def msg_text(data):
    """session_message.data is JSON like {"text": "...", "time": {...}}."""
    if not data:
        return ""
    try:
        obj = json.loads(data)
    except (ValueError, TypeError):
        return str(data)
    if isinstance(obj, dict):
        t = obj.get("text")
        if isinstance(t, str):
            return t
        c = obj.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return "\n".join(p.get("text", "") for p in c
                             if isinstance(p, dict))
    return str(obj)


def slugify(title, fallback):
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return (s[:40] or fallback)


def iso(ts_ms):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts_ms / 1000))
    except (OverflowError, OSError, ValueError):
        return "?"


def under_root(directory, root):
    """True when the session belongs to this project (drive-letter case,
    slash direction and trailing separators normalized)."""
    if not directory:
        return False
    d = os.path.normcase(os.path.normpath(directory))
    r = os.path.normcase(os.path.normpath(str(root)))
    return d == r or d.startswith(r + os.sep)


def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 \
        else Path(__file__).resolve().parent.parent
    out = root / "logs" / "sessions"
    if not DB.is_file():
        print("export: opencode.db not found, skipping")
        return 0
    con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    sessions = {}
    for table in ("session_v2", "session"):
        for r in con.execute(
                "SELECT id, title, directory, time_created, time_updated "
                f"FROM {table}"):
            sessions.setdefault(r["id"], r)   # v2 wins (queried first)

    out.mkdir(parents=True, exist_ok=True)
    keep = set()
    written = unchanged = skipped = foreign = 0
    total_chars = 0

    for sid, s in sessions.items():
        if not under_root(s["directory"], root):
            foreign += 1
            continue
        msgs = con.execute(
            "SELECT seq, type, data FROM session_message "
            "WHERE session_id=? ORDER BY seq", (sid,)).fetchall()
        if not msgs:
            skipped += 1
            continue
        users = [m for m in msgs if m["type"] == "user"]
        compactions = [m for m in msgs if m["type"] == "compaction"]
        if not users and not s["title"]:
            skipped += 1
            continue

        title = (s["title"] or "").strip() or "untitled"
        body = [f"# {title}", "",
                f"Session: {sid}",
                f"Project: {s['directory'] or '?'}",
                f"Created: {iso(s['time_created'])}  "
                f"Updated: {iso(s['time_updated'])}", ""]

        if users:
            first = msg_text(users[0]["data"])
            body += ["## First request", "", cut(first, CAP_FIRST), ""]
            if len(users) > 1:
                last = msg_text(users[-1]["data"])
                if last.strip() and last.strip() != first.strip():
                    body += ["## Last request", "",
                             cut(last, CAP_LAST), ""]
        if compactions:
            last_c = msg_text(compactions[-1]["data"])
            body += ["## Session digest (compaction)", "",
                     cut(last_c, CAP_COMPACTION), ""]

        text = "\n".join(body).strip() + "\n"
        slug = slugify(title, "session")
        day = time.strftime("%Y%m%d", time.localtime(s["time_updated"] / 1000)) \
            if s["time_updated"] else "00000000"
        fname = f"{day}-{slug}-{sid[-6:]}.md"
        keep.add(fname)
        path = out / fname

        if path.is_file() and path.read_text(encoding="utf-8") == text:
            unchanged += 1
            continue
        path.write_text(text, encoding="utf-8")
        written += 1
        total_chars += len(text)

    removed = 0
    for f in out.glob("*.md"):          # stale: gone, renamed, out of scope
        if f.name not in keep:
            f.unlink()
            removed += 1

    con.close()
    on_disk = sum(f.stat().st_size for f in out.glob("*.md"))
    print(f"export: root {root}, sessions {len(sessions)} "
          f"(own {len(keep)}, other projects {foreign}), "
          f"written {written}, unchanged {unchanged}, skipped {skipped}, "
          f"removed {removed}; on disk {len(list(out.glob('*.md')))} files, "
          f"{on_disk / 1e6:.2f} MB")
    if written:
        print(f"export: new chars {total_chars} "
              f"(B580 embed ~{total_chars / 4800 / 60:.1f} min)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
