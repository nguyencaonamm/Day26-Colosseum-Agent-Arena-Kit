#!/usr/bin/env python3
"""kit/arena_ui/tools/preview_sprites.py

COLOSSEUM — renders every sprite in core/sprites.js and a sample of the
5x7 bitmap font in core/font.js to a single PNG, so a human can eyeball the
pixel art without a browser. Python 3.12 STDLIB ONLY: no PIL/Pillow, no
numpy. The PNG encoder (write_png, below) is ~30 lines of struct + zlib, per
the task brief.

This tool does NOT re-implement the sprite/font data. It is a real reader of
core/sprites.js and core/font.js's shipped, browser-consumed source: it
extracts the PALETTE/CHARS/SHEETS/GLYPHS literals straight out of those
files (the sentinel-comment convention documented at the top of each — see
"BEGIN-DATA:<NAME>" there) and validates + renders exactly what a browser
would decode. If someone edits a sprite row and breaks it, this tool is
supposed to fail loudly, not render a stale picture.

Usage:
    python3 preview_sprites.py [--out PATH.png] [--scale N] [--font-scale N]

Exit code is non-zero (with every problem printed) if any invariant fails;
the PNG is still written in that case, best-effort, so a broken sprite is
visible rather than merely reported.
"""
from __future__ import annotations

import argparse
import json
import re
import struct
import sys
import unicodedata
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORE = HERE.parent / "core"
SPRITES_JS = CORE / "sprites.js"
FONT_JS = CORE / "font.js"

SPRITE_SIZE = 16          # every sprite in sprites.js is 16x16
GLYPH_W, GLYPH_H = 5, 7   # every glyph in font.js is 5x7
GLYPH_ADVANCE = 6         # 5 + 1px gap, matches font.js's own ADVANCE


# ---------------------------------------------------------------------------
# Step 1: extract the marked-JSON data blocks straight out of the real .js
# files (the "DATA-EXTRACTION CONTRACT" documented in each file's header).
# ---------------------------------------------------------------------------

def extract_block(js_text: str, name: str, source_label: str):
    """Slice the `// BEGIN-DATA:<name>` ... `// END-DATA:<name>` region out of
    `js_text`, strip the `export const <name> = ` prefix and trailing `;`,
    and json.loads() what remains."""
    pattern = re.compile(
        r"// BEGIN-DATA:" + re.escape(name) + r"\s*\n(.*?)// END-DATA:" + re.escape(name),
        re.S,
    )
    m = pattern.search(js_text)
    if not m:
        raise ValueError(f"{source_label}: no BEGIN-DATA:{name}/END-DATA:{name} block found")
    block = m.group(1)
    prefix = f"export const {name} ="
    idx = block.find(prefix)
    if idx == -1:
        raise ValueError(f"{source_label}: {name} block missing 'export const {name} =' prefix")
    literal = block[idx + len(prefix):].strip()
    if literal.endswith(";"):
        literal = literal[:-1].rstrip()
    try:
        return json.loads(literal)
    except json.JSONDecodeError as e:
        raise ValueError(f"{source_label}: {name} block is not valid JSON: {e}") from e


def extract_quoted_const(js_text: str, name: str, source_label: str) -> str:
    """For a plain `export const NAME = '...';` line outside any marked
    block (e.g. sprites.js's BG), not a JSON block."""
    m = re.search(rf"export const {re.escape(name)} = '([^']*)';", js_text)
    if not m:
        raise ValueError(f"{source_label}: no `export const {name} = '...'` line found")
    return m.group(1)


def load_sprites():
    text = SPRITES_JS.read_text(encoding="utf-8")
    palette = extract_block(text, "PALETTE", "sprites.js")
    chars = extract_block(text, "CHARS", "sprites.js")
    sheets = extract_block(text, "SHEETS", "sprites.js")
    bg = extract_quoted_const(text, "BG", "sprites.js")
    return palette, chars, sheets, bg


def load_glyphs():
    text = FONT_JS.read_text(encoding="utf-8")
    return extract_block(text, "GLYPHS", "font.js")


# ---------------------------------------------------------------------------
# Step 2: invariant checks, mirroring what a browser-side test would assert.
# ---------------------------------------------------------------------------

def validate(palette, chars, sheets, glyphs) -> list[str]:
    errors: list[str] = []

    if len(palette) != len(chars):
        errors.append(f"PALETTE length {len(palette)} != CHARS length {len(chars)}")
    for i, hexcolor in enumerate(palette):
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", hexcolor):
            errors.append(f"PALETTE[{i}] = {hexcolor!r} is not a #rrggbb hex colour")
    if len(set(chars)) != len(chars):
        errors.append(f"CHARS has duplicate characters: {chars!r}")
    if "." in chars:
        errors.append("CHARS must not include '.' (it is the reserved transparent marker)")

    charset = set(chars)

    def walk_sheets(node, path):
        if isinstance(node, list):
            if len(node) != SPRITE_SIZE:
                errors.append(f"{path}: {len(node)} rows, want {SPRITE_SIZE}")
            for i, row in enumerate(node):
                if len(row) != SPRITE_SIZE:
                    errors.append(f"{path}[{i}]: row length {len(row)}, want {SPRITE_SIZE}")
                for x, ch in enumerate(row):
                    if ch != "." and ch not in charset:
                        errors.append(f"{path}[{i}][{x}]: char {ch!r} not in CHARS")
        elif isinstance(node, dict):
            for k, v in node.items():
                walk_sheets(v, f"{path}.{k}")
        else:
            errors.append(f"{path}: unexpected node type {type(node).__name__}")

    walk_sheets(sheets, "SHEETS")

    for ch, rows in glyphs.items():
        label = f"GLYPHS[{ch!r}]"
        if len(rows) != GLYPH_H:
            errors.append(f"{label}: {len(rows)} rows, want {GLYPH_H}")
        for i, row in enumerate(rows):
            if len(row) != GLYPH_W:
                errors.append(f"{label}[{i}]: row length {len(row)}, want {GLYPH_W}")
            for px in row:
                if px not in "#.":
                    errors.append(f"{label}[{i}]: pixel {px!r} not in '#.'")

    for required in ("A", "0", " ", "�"):
        if required not in glyphs:
            errors.append(f"GLYPHS is missing required entry {required!r}")

    return errors


# ---------------------------------------------------------------------------
# Step 3: a minimal PNG encoder. Stdlib only: struct for the chunk framing,
# zlib for the DEFLATE compression PNG's IDAT chunk requires. Truecolour
# (colour type 2, no alpha) since every pixel this tool draws is opaque.
# ---------------------------------------------------------------------------

def write_png(path: Path, width: int, height: int, framebuffer: bytearray) -> None:
    """`framebuffer` is a flat RGB bytearray, length width*height*3, row-major."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 2 = truecolour RGB
    stride = width * 3
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # per-scanline filter type 0 (None)
        raw.extend(framebuffer[y * stride:(y + 1) * stride])
    idat = zlib.compress(bytes(raw), 6)
    with open(path, "wb") as f:
        f.write(sig)
        f.write(chunk(b"IHDR", ihdr))
        f.write(chunk(b"IDAT", idat))
        f.write(chunk(b"IEND", b""))


# ---------------------------------------------------------------------------
# Step 4: a tiny drawing surface over the flat framebuffer.
# ---------------------------------------------------------------------------

def hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


class Canvas:
    def __init__(self, width: int, height: int, bg: tuple[int, int, int]):
        self.w = width
        self.h = height
        self.buf = bytearray(bg * (width * height))

    def set_px(self, x: int, y: int, rgb: tuple[int, int, int]) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            o = (y * self.w + x) * 3
            self.buf[o] = rgb[0]
            self.buf[o + 1] = rgb[1]
            self.buf[o + 2] = rgb[2]

    def fill_rect(self, x: int, y: int, w: int, h: int, rgb: tuple[int, int, int]) -> None:
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                self.set_px(xx, yy, rgb)

    def draw_sprite(self, sprite_rows: list[str], palette: list[str], chars: str,
                     x: int, y: int, scale: int) -> None:
        for ry, row in enumerate(sprite_rows):
            for rx, ch in enumerate(row):
                if ch == ".":
                    continue
                idx = chars.index(ch)
                rgb = hex_to_rgb(palette[idx])
                self.fill_rect(x + rx * scale, y + ry * scale, scale, scale, rgb)

    def draw_text(self, glyphs: dict, text: str, x: int, y: int, scale: int,
                   rgb: tuple[int, int, int]) -> int:
        """Draws with the SAME 5x7 pixel font font.js ships (glyphs already
        folded by the caller if needed). Returns the width drawn."""
        cx = x
        unknown = glyphs.get("�")
        for ch in text:
            rows = glyphs.get(ch, unknown)
            if rows is not None:
                for ry, row in enumerate(rows):
                    for rx, px in enumerate(row):
                        if px == "#":
                            self.fill_rect(cx + rx * scale, y + ry * scale, scale, scale, rgb)
            cx += GLYPH_ADVANCE * scale
        return cx - x


# ---------------------------------------------------------------------------
# Step 5: the same Vietnamese/typographic fold font.js's foldAscii()
# implements, reproduced here in Python so the font sample can show a real
# Vietnamese string actually run through it (not a hand-typed stand-in).
# ---------------------------------------------------------------------------

def fold_ascii(s: str) -> str:
    s = s.replace("đ", "d").replace("Đ", "D")  # d-stroke has no NFD decomposition
    s = unicodedata.normalize("NFD", s)
    s = re.sub("[\u0300-\u036f]", "", s)  # combining marks: breve, circumflex, horn, 5 tones
    s = re.sub("[–—]", "-", s)  # en dash, em dash
    s = s.replace("·", ".")           # middle dot
    s = s.replace("…", "...")         # ellipsis
    s = s.replace("×", "x")           # multiplication sign
    return s


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def flatten_sheets(sheets: dict):
    """[(dotted-name, sprite_rows), ...] in a stable, deterministic order
    (sorted top-level keys, then a fixed frame order for nested entries)."""
    out = []
    frame_order = ["idle", "attack", "hurt"]
    for name in sorted(sheets.keys()):
        node = sheets[name]
        if isinstance(node, dict):
            for frame in frame_order:
                if frame in node:
                    out.append((f"{name}.{frame}", node[frame]))
            for k in sorted(node.keys()):
                if k not in frame_order:
                    out.append((f"{name}.{k}", node[k]))
        else:
            out.append((name, node))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=HERE / "preview.png",
                     help="output PNG path (default: tools/preview.png)")
    ap.add_argument("--scale", type=int, default=8, help="sprite pixel scale (default 8)")
    ap.add_argument("--font-scale", type=int, default=3, help="font sample pixel scale (default 3)")
    args = ap.parse_args()

    palette, chars, sheets, bg_hex = load_sprites()
    glyphs = load_glyphs()

    errors = validate(palette, chars, sheets, glyphs)
    for e in errors:
        print(f"INVALID: {e}", file=sys.stderr)

    bg = hex_to_rgb(bg_hex)
    text_rgb = (231, 236, 247)   # theme.js COLORS.text, for label contrast on bg
    dim_rgb = (136, 146, 166)    # theme.js COLORS.textDim

    sprites = flatten_sheets(sheets)
    scale = max(1, args.scale)
    fscale = max(1, args.font_scale)

    cols = 4
    cell_w = SPRITE_SIZE * scale + 24
    cell_h = SPRITE_SIZE * scale + 24 + (GLYPH_H * 1 + 4)  # + label row at scale 1
    rows = (len(sprites) + cols - 1) // cols
    grid_w = cell_w * cols
    grid_h = cell_h * rows

    font_lines = [
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "abcdefghijklmnopqrstuvwxyz",
        "0123456789",
        ".,:;!?-_/\\()[]{}'\"`=+*%#@&~<>|^$",
        "ROUND 6/10  x1.25 SCALE  cr 43/100",
        "VERIFIED - 10 x 1.25 = 12 DAMAGE",
    ]
    vn_sample = "Đường ống dữ liệu — tài liệu AI20K"
    vn_folded = fold_ascii(vn_sample)
    # The PNG can only show what the 5x7 pixel font can draw, i.e. ASCII -
    # so there is no way to render `vn_sample` itself in this image without
    # ALSO folding it (that would be drawTextUnicode's job, which needs a
    # real browser's system font, not reproducible in a stdlib PNG). Showing
    # a "VN raw" pixel-font line here would silently be the folded text
    # again under a misleading label, which is exactly the kind of quiet
    # mangling this file's whole design exists to avoid - so only the
    # actually-drawable folded line is rendered, and the true raw string is
    # printed to stdout below instead (see the "VN fold check" line).
    font_lines.append(f"VN sample, ASCII-folded for THIS pixel font: {vn_folded}")

    max_line_len = max(len(l) for l in font_lines)
    font_block_w = 16 + max_line_len * GLYPH_ADVANCE * fscale + 16
    font_block_h = 16 + len(font_lines) * (GLYPH_H * fscale + 6) + 16

    total_w = max(grid_w, font_block_w)
    total_h = grid_h + font_block_h + 16

    canvas = Canvas(total_w, total_h, bg)

    # --- sprite grid ---
    for i, (name, sprite_rows) in enumerate(sprites):
        gx = (i % cols) * cell_w + 12
        gy = (i // cols) * cell_h + 12
        canvas.draw_sprite(sprite_rows, palette, chars, gx, gy, scale)
        label = name if len(name) <= 20 else name[:19] + "…"
        canvas.draw_text(glyphs, fold_ascii(label), gx, gy + SPRITE_SIZE * scale + 6, 1, dim_rgb)

    # --- font sample block ---
    fy = grid_h + 16
    for line in font_lines:
        canvas.draw_text(glyphs, fold_ascii(line), 16, fy, fscale, text_rgb)
        fy += GLYPH_H * fscale + 6

    write_png(args.out, total_w, total_h, canvas.buf)

    print(f"wrote {args.out} ({total_w}x{total_h}), {len(sprites)} sprites, {len(glyphs)} glyphs")
    print(f"VN fold check: {vn_sample!r} -> {vn_folded!r}")
    if errors:
        print(f"{len(errors)} invariant violation(s) found - see stderr above", file=sys.stderr)
        return 1
    print("all invariants OK: PALETTE/CHARS aligned, every sprite 16x16, every glyph 5x7")
    return 0


if __name__ == "__main__":
    sys.exit(main())
