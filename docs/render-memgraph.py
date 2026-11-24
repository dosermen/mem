# -*- coding: utf-8 -*-
"""Render the memory related-graph (active records) to PNG. Local, free.

Reads memory.db (id, file, type, status, version, related, importance),
draws active records grouped by type in columns, edges = related links.
Orphans (no links either way) are marked. Re-run after memory changes:
    python docs/render-memgraph.py
Output: docs/mem-graph.png
"""
import math
import sqlite3
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "Pillow"],
                   check=True)
    from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "memory" / "memory.db"
OUT = ROOT / "docs" / "mem-graph.png"

TYPE_COLOR = {
    "semantic": (47, 111, 176),    # blue
    "episodic": (63, 155, 84),     # green
    "procedural": (208, 138, 52),  # orange
    "working": (150, 90, 170),     # purple
    "identity": (180, 60, 60),     # red
}
INK = (26, 26, 26)
GRAY = (140, 140, 140)
EDGE = (120, 130, 145)

img = None
d = None
F_TITLE = F_NODE = F_SM = None


def font(size, bold=False):
    for name in (("segoeuib.ttf", "segoeui.ttf") if bold else
                 ("segoeui.ttf", "arial.ttf")):
        try:
            return ImageFont.truetype("C:/Windows/Fonts/" + name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def load():
    con = sqlite3.connect(str(DB))
    rows = list(con.execute(
        "select id, file, type, status, version, related, importance "
        "from memories where status='active' order by id"))
    con.close()
    return rows


def short_name(file_):
    stem = Path(file_).stem
    return stem if len(stem) <= 30 else stem[:29] + "~"


def main():
    global img, d, F_TITLE, F_NODE, F_SM
    rows = load()
    if not rows:
        print("no active records")
        return

    types = []
    for _, _, t, _, _, _, _ in rows:
        if t not in types:
            types.append(t)
    types.sort(key=lambda t: list(TYPE_COLOR).index(t)
               if t in TYPE_COLOR else 99)

    # layout: columns per type, nodes stacked vertically
    COL_W, NODE_H, PAD_TOP, PAD_LEFT = 340, 62, 110, 40
    cols = {t: [r for r in rows if r[2] == t] for t in types}
    maxn = max(len(v) for v in cols.values())
    W = PAD_LEFT * 2 + COL_W * len(types)
    H = PAD_TOP + 60 + NODE_H * maxn + 60

    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    F_TITLE = font(30, True)
    F_NODE = font(17, True)
    F_SM = font(14)

    pos = {}   # id -> (cx, cy)
    boxes = {}  # id -> (x1,y1,x2,y2)
    for ci, t in enumerate(types):
        x1 = PAD_LEFT + ci * COL_W
        # column header
        c = TYPE_COLOR.get(t, GRAY)
        d.rounded_rectangle([x1, 60, x1 + COL_W - 30, 100], radius=10,
                            fill=c)
        d.text((x1 + (COL_W - 30) // 2, 80), f"{t} ({len(cols[t])})",
               font=F_NODE, fill="white", anchor="mm")
        for ri, (mid, file_, _, _, ver, rel, imp) in enumerate(cols[t]):
            y1 = PAD_TOP + 60 + ri * NODE_H
            x2, y2 = x1 + COL_W - 30, y1 + NODE_H - 10
            boxes[mid] = (x1, y1, x2, y2)
            pos[mid] = ((x1 + x2) // 2, (y1 + y2) // 2)

    # edges first (under nodes)
    rel_of = {}
    incoming = {r[0] for r in rows}
    orphans = set()
    for mid, file_, t, _, ver, rel, imp in rows:
        ids = [int(x) for x in rel.replace(",", " ").split() if x.strip().isdigit()]
        ids = [i for i in ids if i in pos or True]  # keep even unknown targets
        rel_of[mid] = ids
        if not ids:
            orphans.add(mid)
    # reverse: a node is orphan only if no outgoing AND no incoming
    has_in = set()
    for mid, ids in rel_of.items():
        for i in ids:
            has_in.add(i)
    orphans = {m for m in orphans if m not in has_in}

    for mid, ids in rel_of.items():
        if mid not in pos:
            continue
        x1, y1 = pos[mid]
        for tgt in ids:
            if tgt not in pos:
                continue
            x2, y2 = pos[tgt]
            mx = (x1 + x2) // 2
            my = (y1 + y2) // 2 - 18
            pts = []
            for k in range(21):
                u = k / 20
                bx = (1 - u) ** 2 * x1 + 2 * (1 - u) * u * mx + u ** 2 * x2
                by = (1 - u) ** 2 * y1 + 2 * (1 - u) * u * my + u ** 2 * y2
                pts.append((bx, by))
            d.line(pts, fill=EDGE, width=2)
            # arrow head
            ax, ay = pts[-1]
            px, py = pts[-2]
            ang = math.atan2(ay - py, ax - px)
            L = 9
            d.polygon([(ax, ay),
                       (ax - L * math.cos(ang - 0.4),
                        ay - L * math.sin(ang - 0.4)),
                       (ax - L * math.cos(ang + 0.4),
                        ay - L * math.sin(ang + 0.4))], fill=EDGE)

    # nodes on top
    for mid, file_, t, _, ver, rel, imp in rows:
        x1, y1, x2, y2 = boxes[mid]
        c = TYPE_COLOR.get(t, GRAY)
        width = 3 if mid in has_in or rel_of.get(mid) else 1
        outline = (200, 60, 60) if mid in orphans else c
        d.rounded_rectangle([x1, y1, x2, y2], radius=9, fill=(248, 249, 251),
                            outline=outline, width=width)
        # importance dot
        for k in range(min(imp, 5)):
            d.ellipse([x2 - 14 - k * 9, y1 + 6, x2 - 8 - k * 9, y1 + 12],
                      fill=c)
        d.text((x1 + 8, y1 + 6), f"{mid}@v{ver}", font=F_NODE, fill=INK)
        d.text((x1 + 8, y1 + 30), short_name(file_), font=F_SM, fill=GRAY)

    d.text((W // 2, 28), f"memory related-graph - {len(rows)} active records",
           font=F_TITLE, fill=INK, anchor="mm")
    leg = "edge = related | red border = orphan (no links) | dots = importance"
    if orphans:
        leg += f" | orphans: {', '.join(str(o) for o in sorted(orphans))}"
    d.text((W // 2, H - 24), leg, font=F_SM, fill=GRAY, anchor="mm")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(OUT))
    print(f"wrote {OUT} ({W}x{H}), {len(rows)} nodes, "
          f"{sum(len(v) for v in rel_of.values())} edges, "
          f"{len(orphans)} orphans")


if __name__ == "__main__":
    main()
