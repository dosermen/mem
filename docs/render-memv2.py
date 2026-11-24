# -*- coding: utf-8 -*-
"""Render MEMv2 architecture diagram to PNG (local, free, exact text)."""
import math
import sys

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "Pillow"],
                   check=True)
    from PIL import Image, ImageDraw, ImageFont

W, H = 1600, 1050
INK = (26, 26, 26)
BLUE = (47, 111, 176)
GREEN = (63, 155, 84)
ORANGE = (208, 138, 52)
GRAY = (110, 110, 110)

img = Image.new("RGB", (W, H), "white")
d = ImageDraw.Draw(img)


def font(size, bold=False):
    for name in (("segoeuib.ttf", "segoeui.ttf") if bold else
                 ("segoeui.ttf", "arial.ttf")):
        try:
            return ImageFont.truetype("C:/Windows/Fonts/" + name, size)
        except OSError:
            continue
    return ImageFont.load_default()


F_TITLE = font(36, True)
F_BOX = font(25, True)
F_CAP = font(17)
F_CHIP = font(19, True)
F_LBL = font(19, True)
F_SM = font(16)


def box(x1, y1, x2, y2, fill, outline, width=3, radius=14):
    d.rounded_rectangle([x1, y1, x2, y2], radius=radius, fill=fill,
                        outline=outline, width=width)


def text_c(cx, cy, s, f, fill=INK):
    d.text((cx, cy), s, font=f, fill=fill, anchor="mm")


def arrow(x1, y1, x2, y2, color=BLUE, w=3, dash=False, head=True):
    if dash:
        dist = math.hypot(x2 - x1, y2 - y1)
        steps = max(int(dist // 12), 1)
        for i in range(steps):
            if i % 2 == 0:
                a = i / steps
                b = (i + 1) / steps
                d.line([(x1 + (x2 - x1) * a, y1 + (y2 - y1) * a),
                        (x1 + (x2 - x1) * b, y1 + (y2 - y1) * b)],
                       fill=color, width=w)
    else:
        d.line([(x1, y1), (x2, y2)], fill=color, width=w)
    if head:
        ang = math.atan2(y2 - y1, x2 - x1)
        L, Wd = 15, 7
        p2 = (x2 - L * math.cos(ang) + Wd * math.sin(ang),
              y2 - L * math.sin(ang) - Wd * math.cos(ang))
        p3 = (x2 - L * math.cos(ang) - Wd * math.sin(ang),
              y2 - L * math.sin(ang) + Wd * math.cos(ang))
        d.polygon([(x2, y2), p2, p3], fill=color)


def label(x, y, s, color=INK, bg="white"):
    f = F_LBL
    tb = d.textbbox((x, y), s, font=f, anchor="mm")
    pad = 5
    d.rectangle([tb[0] - pad, tb[1] - pad, tb[2] + pad, tb[3] + pad],
                fill=bg)
    d.text((x, y), s, font=f, fill=color, anchor="mm")


# ---- Title ----
text_c(W // 2, 38, "MEMv2 \u2014 mem.py serve architecture", F_TITLE)
text_c(W // 2, 74,
       "MCP = default interface (AGENTS 20.11)  \u00b7  "
       "CLI = sweep / doctor / embed / sync",
       F_CAP, GRAY)

# ---- Tier 1: MCP clients ----
clients = [("opencode", "mcp.servers.mem, codemode:false"),
           ("Roo Code", "mcp_settings.json \u2192 mem"),
           ("Cline", "UI: MCP + mem.py serve")]
cw, ch, cy = 330, 96, 110
xs = [135, 635, 1135]
for (name, cap), x in zip(clients, xs):
    box(x, cy, x + cw, cy + ch, (234, 242, 251), BLUE)
    text_c(x + cw // 2, cy + 34, name, F_BOX)
    text_c(x + cw // 2, cy + 68, cap, F_CAP, GRAY)

# ---- Converging arrows + transport label ----
mx1, my1, mx2, my2 = 250, 330, 1500, 545   # middle box
for x in xs:
    arrow(x + cw // 2, cy + ch + 4, x + cw // 2 + (875 - (x + cw // 2)) * 0.85,
          my1 - 6, BLUE, 3)
label(875, 275, "stdio: JSON-RPC 2.0 (newline, UTF-8)")

# ---- Middle: mem.py serve ----
box(mx1, my1, mx2, my2, (220, 233, 249), BLUE, 4)
text_c((mx1 + mx2) // 2, my1 + 34,
       "mem.py serve  \u2014  MCP stdio server (Python stdlib only)", F_BOX)

chips = ["remember", "recall", "search", "update_memory", "forget",
         "restore", "list", "show", "link"]
widths = [len(c) * 11 + 40 for c in chips]
total = sum(widths) + 12 * (len(chips) - 1)
cx = (mx1 + mx2) // 2 - total // 2
chip_y = my1 + 72
for c, w in zip(chips, widths):
    d.rounded_rectangle([cx, chip_y, cx + w, chip_y + 42], radius=21,
                        fill="white", outline=BLUE, width=2)
    text_c(cx + w // 2, chip_y + 21, c, F_CHIP, BLUE)
    cx += w + 12
text_c((mx1 + mx2) // 2, my1 + 148,
       "tools/call \u2192 _mcp_dispatch \u2192 same cmd_* functions as CLI",
       F_CAP, GRAY)
text_c((mx1 + mx2) // 2, my1 + 176,
       "artifacts fallback: logs\\*.md  \u00b7  node-pkg skipped "
       "(MEM_ARTIFACTS_SKIP)", F_CAP, GRAY)

# ---- CLI side entry ----
box(40, 398, 218, 482, (245, 245, 245), GRAY, 2)
text_c(129, 428, "CLI (shell)", F_BOX, INK)
text_c(129, 458, "mem.py <cmd>", F_CAP, GRAY)
arrow(222, 440, mx1 - 4, 440, GRAY, 3)
text_c(129, 512, "same cmd_* functions", F_CAP, GRAY)

# ---- Arrows middle -> storage ----
arrow(520, my2 + 4, 450, 636, GREEN, 3)
label(430, 585, "remember \u2192 write md \u2192 sync", GREEN)
arrow(1240, my2 + 4, 1150, 636, GREEN, 3)
label(1180, 585, "recall \u2192 FTS5 + rerank", GREEN)

# ---- Tier 3: storage ----
box(150, 640, 700, 780, (233, 246, 236), GREEN)
text_c(425, 685, "memory\\*.md files", F_BOX)
text_c(425, 725, "source of truth (frontmatter XMemo)", F_CAP, GRAY)
text_c(425, 755, "versions / supersedes / related", F_CAP, GRAY)

box(840, 640, 1470, 780, (233, 246, 236), GREEN)
text_c(1155, 685, "memory.db  (SQLite)", F_BOX)
text_c(1155, 725, "FTS5 + mem_vec + art_vec", F_CAP, GRAY)
text_c(1155, 755, "rebuildable: mem.py sync", F_CAP, GRAY)

# ---- Recall fallback route (dashed L: middle-left -> artifacts) ----
arrow(250, 530, 90, 530, BLUE, 3, dash=True, head=False)
arrow(90, 530, 90, 925, BLUE, 3, dash=True, head=False)
arrow(90, 925, 154, 925, BLUE, 3, dash=True)
label(200, 815, "0 hits \u2192 artifacts", BLUE)

# ---- Artifacts (reports in logs\) ----
box(150, 850, 760, 1000, (234, 242, 251), BLUE)
text_c(455, 890, "logs\\*.md \u2014 report artifacts", F_BOX)
text_c(455, 928, "recall fallback: keyword + vec (art_vec)", F_CAP, GRAY)
text_c(455, 958, "skip node-pkg \u00b7 MEM_ARTIFACTS_SKIP (24.11)",
       F_CAP, GRAY)

# ---- Sweep task ----
box(790, 850, 1120, 1000, (253, 240, 227), ORANGE)
text_c(955, 890, "Mem-Sweep \u00b7 10 min", F_BOX)
text_c(955, 928, "one embed lock: records + art_vec", F_CAP, GRAY)
text_c(955, 958, "doctor \u2192 link-suggestions.md", F_CAP, GRAY)
arrow(700, 848, 900, 782, GREEN, 3)
label(790, 834, "insert art_vec", GREEN)
arrow(970, 848, 1090, 782, GREEN, 3)
label(1030, 808, "sync + embed", GREEN)

# ---- Embed server (phone) ----
box(1160, 850, 1470, 1000, (253, 240, 227), ORANGE)
text_c(1315, 890, "embedding server :5031", F_BOX, ORANGE)
text_c(1315, 928, "remote phone only", F_CAP, GRAY)
text_c(1315, 958, "down \u2192 FTS; local server NOT started", F_CAP, GRAY)
arrow(1250, 784, 1330, 846, ORANGE, 3, dash=True)
label(1395, 815, "embed / cosine", ORANGE)

# ---- footer ----
d.text((20, H - 26), "docs\\memv2-arch.md  \u00b7  24.11.2026  \u00b7  opencode",
       font=F_SM, fill=GRAY)

out = r"F:\AI\OLAMA\docs\memv2-arch.png"
img.save(out)
print("saved:", out, img.size)
