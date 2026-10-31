#!/usr/bin/env python3
"""Draw preview.png: an illustration (made-up spaces and sessions) of a Herdr window
right after Ctrl+Shift+R brought three agents back. Needs Pillow and DejaVu fonts."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

W, H = 1460, 900
S = 2  # drawn at 2x, scaled down for smooth edges
BG, CHROME, BORDER, FG, MUTED, DIM = "#282c34", "#21252b", "#3a3f4b", "#d7dae0", "#9da5b4", "#6b717d"
ACCENT, DONE, POPOVER, CLAUDE, CODEX = "#61afef", "#98c379", "#2c313a", "#d97757", "#c8ccd4"
MONO = "/usr/share/fonts/TTF/DejaVuSansMono.ttf"
SANS = "/usr/share/fonts/TTF/DejaVuSans.ttf"
SANS_BOLD = "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"


def font(path, size):
    return ImageFont.truetype(path, size * S)


img = Image.new("RGB", (W * S, H * S), CHROME)
d = ImageDraw.Draw(img)


def box(x0, y0, x1, y1, fill=None, outline=None, r=0, width=1):
    d.rounded_rectangle([x0 * S, y0 * S, x1 * S, y1 * S], r * S, fill=fill, outline=outline, width=width * S)


def text(x, y, s, f, color):
    d.text((x * S, y * S), s, font=f, fill=color)


mono, small, ui, bold = font(MONO, 14), font(SANS, 12), font(SANS, 13), font(SANS_BOLD, 13)

# Sidebar: spaces
box(0, 0, 220, H, fill=CHROME)
d.line([220 * S, 0, 220 * S, H * S], fill=BORDER, width=S)
text(18, 20, "SPACES", font(SANS_BOLD, 11), DIM)
for i, (name, active) in enumerate([("api", True), ("web", False), ("docs", False)]):
    y = 48 + i * 34
    if active:
        box(10, y - 6, 210, y + 22, fill="#2c313a", r=6)
    text(22, y, name, ui, FG if active else MUTED)
    d.ellipse([(190) * S, (y + 5) * S, (197) * S, (y + 12) * S], fill=DONE)

# Tab bar
box(221, 0, W, 40, fill=CHROME)
d.line([221 * S, 40 * S, W * S, 40 * S], fill=BORDER, width=S)
for i, (name, active) in enumerate([("1", True), ("review", False)]):
    x = 236 + i * 110
    if active:
        box(x, 7, x + 96, 33, fill=BG, outline=BORDER, r=6)
    text(x + 14, 13, name, ui, FG if active else MUTED)

# Three panes
panes = [
    (221, 41, 840, 470, "~/work/api", "claude", "4f1c2a9e-7b3d-4e8a-9c51-2d6f0a8b3e17", CLAUDE, "Claude Code"),
    (841, 41, W, 470, "~/work/api", "codex", "019a2b7c-3d4e-7f10-8a2b-c3d4e5f60718", CODEX, "OpenAI Codex"),
    (221, 471, W, H, "~/work/api/worker", "claude", "9e2f41c0-3b7d-4c55-a0d1-6f0e8b7a2c19", CLAUDE, "Claude Code"),
]
for x0, y0, x1, y1, folder, agent, sid, color, title in panes:
    box(x0, y0, x1, y1, fill=BG)
    d.rectangle([x0 * S, y0 * S, x1 * S, y1 * S], outline=BORDER, width=S)
    x, y = x0 + 18, y0 + 18
    text(x, y, f"╭─you@laptop in {folder}", mono, DIM)
    cmd = f"cd '{folder.replace('~', '/home/you')}' && " + (f"claude --resume {sid[:8]}…" if agent == "claude" else f"codex resume {sid[:8]}…")
    text(x, y + 22, "╰─λ ", mono, DONE)
    text(x + 36, y + 22, cmd, mono, FG)
    # the agent, back on its conversation
    y += 66
    box(x, y, x1 - 18, y + 76, outline=color, r=8)
    text(x + 16, y + 14, f"✻ {title}" if agent == "claude" else f">_ {title}", bold, color)
    text(x + 16, y + 42, f"Resumed {sid[:8]}… · {folder}", font(MONO, 13), MUTED)
    y += 100
    for i, line in enumerate(["> earlier messages are back above", "  nothing runs until you type"]):
        text(x, y + i * 22, line, mono, DIM if i else MUTED)
    y += 66
    d.line([x * S, y * S, (x1 - 18) * S, y * S], fill=BORDER, width=S)
    text(x, y + 10, "❯ ", mono, FG)
    d.rectangle([(x + 18) * S, (y + 12) * S, (x + 26) * S, (y + 28) * S], fill=FG)

# Toast
tx0, ty0, tx1, ty1 = W - 430, H - 110, W - 24, H - 32
box(tx0 + 3, ty0 + 5, tx1 + 3, ty1 + 5, fill="#1b1e23", r=10)
box(tx0, ty0, tx1, ty1, fill=POPOVER, outline=BORDER, r=10)
d.ellipse([(tx0 + 20) * S, (ty0 + 24) * S, (tx0 + 30) * S, (ty0 + 34) * S], fill=DONE)
text(tx0 + 44, ty0 + 18, "Resumed 3 agents", bold, FG)
text(tx0 + 44, ty0 + 44, "api / 1, api / 1, api / 1", small, MUTED)

# Key hint, top right
hint = "Ctrl+Shift+R"
box(W - 150, 8, W - 16, 32, fill=POPOVER, outline=ACCENT, r=6)
text(W - 136, 13, hint, font(SANS_BOLD, 12), ACCENT)

out = Path(__file__).resolve().parents[1] / "preview.png"
img.resize((W, H), Image.LANCZOS).save(out, optimize=True)
print(out)
