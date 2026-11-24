# -*- coding: utf-8 -*-
"""P1 portable ids - end-to-end tests for mem.py against a temp MEM_ROOT.

Covers: stamp on remember, full-rebuild id survival, id conflict healing,
legacy strip restore, incremental silence, update gets a fresh id, mixed
rebuild warning, recall smoke.
"""
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

MEMPY = str(Path(__file__).resolve().parent.parent / "mem.py")
TMP = Path(tempfile.mkdtemp(prefix="memtest-p1-"))
ENV = dict(os.environ)
ENV.update({
    "MEM_ROOT": str(TMP),
    "MEM_EMBED_REMOTE": "0",   # no network: vectors off, no auto-related
    "MEM_ARTIFACTS": "0",      # recall: records only
    "PYTHONIOENCODING": "utf-8",
})
fails = []


def run(*args):
    r = subprocess.run([sys.executable, MEMPY, *args],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=ENV, timeout=120)
    return r.returncode, r.stdout + r.stderr


def q(sql, *a):
    con = sqlite3.connect(str(TMP / "memory" / "memory.db"))
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, a)]
    finally:
        con.close()


def ids_map():
    rows = q("SELECT file,id,related FROM memories")
    return ({r["file"]: r["id"] for r in rows},
            {r["file"]: r["related"] for r in rows})


def fm_id_of(path):
    text = Path(path).read_text(encoding="utf-8-sig")
    m = re.search(r"(?m)^id:\s*(\d+)\s*$", text)
    return int(m.group(1)) if m else None


def strip_id_line(path):
    p = Path(path)
    text = p.read_text(encoding="utf-8-sig")
    text2 = re.sub(r"(?m)^id:\s*\d+\s*\n", "", text, count=1)
    assert text2 != text, f"no id line in {path}"
    p.write_text(text2, encoding="utf-8", newline="\n")


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name +
          ("" if cond else f"  [{detail}]"))
    if not cond:
        fails.append(name)


def delete_db():
    for s in ("", "-wal", "-shm"):
        f = TMP / "memory" / ("memory.db" + s)
        if f.exists():
            f.unlink()


# ---- t1: remember stamps id, citation matches file and db ---------------
rc, out = run("remember", "alpha record body", "--type", "semantic",
              "--path", "memory/semantic/alpha")
check("t1 remember rc=0", rc == 0, out)
m_id = re.search(r"citation: (\d+)@v(\d+)", out)
check("t1 citation printed", bool(m_id), out)
id_alpha = int(m_id.group(1)) if m_id else -1
alpha_md = TMP / "memory" / "semantic" / "alpha.md"
check("t1 fm id line", fm_id_of(alpha_md) == id_alpha,
      f"fm={fm_id_of(alpha_md)} db={id_alpha}")
check("t1 db id", q("SELECT id FROM memories WHERE file='semantic/alpha.md'")
      [0]["id"] == id_alpha)

# ---- t2: related on remember --------------------------------------------
rc, out = run("remember", "beta record body", "--type", "semantic",
              "--path", "memory/semantic/beta", "--related", str(id_alpha))
check("t2 remember rc=0", rc == 0, out)
id_beta = int(re.search(r"citation: (\d+)@", out).group(1))
beta_md = TMP / "memory" / "semantic" / "beta.md"
check("t2 fm id line", fm_id_of(beta_md) == id_beta)
check("t2 related stored",
      q("SELECT related FROM memories WHERE file='semantic/beta.md'")
      [0]["related"] == str(id_alpha))

# ---- t3: gamma + distinct ids -------------------------------------------
rc, out = run("remember", "gamma record body", "--type", "semantic",
              "--path", "memory/semantic/gamma")
check("t3 remember rc=0", rc == 0, out)
id_gamma = int(re.search(r"citation: (\d+)@", out).group(1))
gamma_md = TMP / "memory" / "semantic" / "gamma.md"
ids0, rel0 = ids_map()
check("t3 three distinct ids", len(set(ids0.values())) == 3, str(ids0))

# ---- t4: FULL REBUILD keeps ids and related (the P1 core) ---------------
delete_db()
rc, out = run("sync")
check("t4 sync rc=0", rc == 0, out)
ids1, rel1 = ids_map()
check("t4 ids identical after rebuild", ids1 == ids0,
      f"before={ids0} after={ids1}")
check("t4 related identical", rel1 == rel0, f"{rel0} vs {rel1}")
check("t4 no renumber warning", "renumbers" not in out, out)
check("t4 no stamping needed", "stamped" not in out, out)
check("t4 beta related still points at alpha",
      rel1.get("semantic/beta.md") == str(ids1.get("semantic/alpha.md")))

# ---- t5: id mismatch on existing row heals; conflict on new file --------
text = gamma_md.read_text(encoding="utf-8-sig")
gamma_md.write_text(re.sub(r"(?m)^id:\s*\d+$", f"id: {id_alpha}", text,
                           count=1), encoding="utf-8", newline="\n")
rc, out = run("sync")
check("t5 mismatch warning", "id mismatch" in out, out)
ids2, _ = ids_map()
check("t5 ids still distinct", len(set(ids2.values())) == 3, str(ids2))
check("t5 alpha keeps its id", ids2["semantic/alpha.md"] == id_alpha, str(ids2))
check("t5 gamma fm healed to db id",
      fm_id_of(gamma_md) == ids2["semantic/gamma.md"],
      f"fm={fm_id_of(gamma_md)} db={ids2['semantic/gamma.md']}")

# t5b: a NEW file claiming an occupied id -> insert-branch conflict
delta_md = TMP / "memory" / "semantic" / "delta.md"
delta_md.write_text(
    "---\n"
    f"id: {id_alpha}\n"
    "type: semantic\n"
    "path: memory/semantic/delta\n"
    "status: active\n"
    "version: 1\n"
    "author: opencode\n"
    "updated: 2026-11-24\n"
    "---\n"
    "\n"
    "delta record body\n",
    encoding="utf-8", newline="\n")
rc, out = run("sync")
check("t5b conflict warning", "id conflict" in out, out)
ids2b, _ = ids_map()
check("t5b four distinct ids", len(set(ids2b.values())) == 4, str(ids2b))
check("t5b alpha still owns its id", ids2b["semantic/alpha.md"] == id_alpha)
check("t5b delta fm stamped to reassigned id",
      fm_id_of(delta_md) == ids2b["semantic/delta.md"],
      f"fm={fm_id_of(delta_md)} db={ids2b['semantic/delta.md']}")
id_gamma = ids2b["semantic/gamma.md"]

# ---- t6: legacy file (id stripped) gets restored ------------------------
strip_id_line(beta_md)
rc, out = run("sync")
check("t6 stamp reported", "stamped 1 record id(s)" in out, out)
check("t6 beta id restored", fm_id_of(beta_md) == ids2["semantic/beta.md"],
      f"fm={fm_id_of(beta_md)} db={ids2['semantic/beta.md']}")

# ---- t7: incremental sync is silent -------------------------------------
rc, out = run("sync")
check("t7 no stamping on incremental", "stamped" not in out, out)
check("t7 no warning on incremental", "WARNING" not in out, out)
ids3, _ = ids_map()
check("t7 ids stable", ids3 == ids2b, f"{ids2b} vs {ids3}")

# ---- t8: update creates a NEW id, old file keeps its own ----------------
rc, out = run("update", "semantic/beta.md", "beta record body v2")
check("t8 update rc=0", rc == 0, out)
m_new = re.search(r"wrote (\S+\.md)", out)
check("t8 new file reported", bool(m_new), out)
new_rel = m_new.group(1) if m_new else ""
new_md = TMP / "memory" / new_rel
check("t8 new file has its own id", fm_id_of(new_md) not in (None, id_beta),
      f"new={fm_id_of(new_md)} old={id_beta}")
check("t8 old file keeps old id", fm_id_of(beta_md) == id_beta)
ids4, _ = ids_map()
check("t8 db rows distinct ids", len(set(ids4.values())) == len(ids4),
      str(ids4))

# ---- t9: mixed rebuild (some stamped, some not) + honest warning --------
strip_id_line(beta_md)       # beta carries related -> legacy-linked
strip_id_line(gamma_md)      # gamma has no related -> legacy unlinked
delete_db()
rc, out = run("sync")
check("t9 warning counts only linked legacy",
      "renumbers ids - 1 record(s)" in out, out)
ids5, rel5 = ids_map()
check("t9 alpha keeps id (explicit)", ids5["semantic/alpha.md"] == id_alpha,
      str(ids5))
check("t9 all ids distinct", len(set(ids5.values())) == len(ids5), str(ids5))
check("t9 beta related intact",
      rel5["semantic/beta.md"] == str(id_alpha), str(rel5))
check("t9 beta stamped", fm_id_of(beta_md) == ids5["semantic/beta.md"])
check("t9 gamma stamped", fm_id_of(gamma_md) == ids5["semantic/gamma.md"])
check("t9 new-version file stamped",
      fm_id_of(new_md) == ids5[new_rel], f"fm={fm_id_of(new_md)}")

# ---- t10: recall smoke after all rebuilds -------------------------------
rc, out = run("recall", "beta record")
check("t10 recall rc=0", rc == 0, out)
check("t10 finds beta", "beta" in out and "0 hits" not in out, out)

print()
if fails:
    print(f"FAILED {len(fails)}: {fails}")
    sys.exit(1)
print(f"ALL PASS (root {TMP})")
