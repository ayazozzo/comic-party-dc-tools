#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
字库工具 — 支持 FONTALL.FNT 和 FONT0-5.FON

界面: 网格中每格下方 = 码位 + 码表字符(左编码, 上方格子=实际渲染字形, 互不校验)
流程: 文件->导入码表 -> 顶部选字体/字号 -> 工具->按码表批量渲染(仅内存)
      -> 网格查看效果 -> 保存(另存为) 手动写盘

格式 A — FONTALL.FNT (DC 版 樱花大战1字库):28号字体
  容器"FONT"+节(大端u32长度); FIDX码位索引; FIMG 字形 32x32@4bpp Morton
  slot→code: ((idx>>8)+0x80)<<8 | (idx&0xFF)  (线性扩展, 含无效 SJIS 码)

格式 B — FONT0-5.FON (DC 版 漫画同人会字库):24号字体
  扁平数组, 无 magic/节结构, 纯 512-byte glyph 槽位
  slot→code: 原始 Shift-JIS 双字节顺序 (lead 0x81-0x9F/0xE0-0xEF, trail 0x40-0x7E/0x80-0xFC)
  47 leads × 188 trails = 8836 codes, FON 文件 7808 槽 (末尾 NEC 扩展区未填充)
"""
from __future__ import annotations

import math
import os
import struct
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageDraw, ImageTk

try:
    import freetype
except Exception:
    freetype = None

try:
    import winreg
except Exception:
    winreg = None

INI_PATH = Path("fontall_tool.ini")
FONT_FILES = ["FONTALL.FNT", "FONT0.FON"]

GLYPH_SIZE = 512  # 每个字形固定 512 字节 (32x32 @ 4bpp Morton)


from pathlib import Path
from typing import Tuple, Dict, List

# ---------------- 字体扫描 ----------------
def scan_system_fonts() -> tuple[dict[str, str], list[str]]:
    """读取当前工作目录下的字体文件，不再读取系统注册表与系统Fonts目录"""
    out: Dict[str, str] = {}
    # 当前脚本所在文件夹
    fonts_dir = Path.cwd()

    # 遍历当前目录，只扫描 ttf / otf / ttc
    for p in fonts_dir.glob("*"):
        if not p.is_file():
            continue
        suffix = p.suffix.lower()
        if suffix not in (".ttf", ".otf", ".ttc"):
            continue
        # 使用文件名(去掉后缀)作为字体显示label
        label = p.stem.strip()
        if label and label not in out:
            out[label] = str(p.resolve())

    names = sorted(out.keys(), key=lambda s: s.lower())
    return out, names


# ---------------- 字库格式 ----------------

def morton(y: int, x: int) -> int:
    r = 0
    b = 0
    while x or y:
        r |= (y & 1) << (2 * b)
        r |= (x & 1) << (2 * b + 1)
        x >>= 1
        y >>= 1
        b += 1
    return r


NIBBLE_ADDR = [[morton(y, x) for x in range(32)] for y in range(32)]


def decode_glyph(blob: bytes) -> list[int]:
    """512 字节 Morton 交织 → 32x32 行主序 nibble 平面 (0-15, 纯重排无语义)"""
    px = [0] * (32 * 32)
    for y in range(32):
        for x in range(32):
            a = NIBBLE_ADDR[y][x]
            byte = blob[a >> 1]
            px[y * 32 + x] = (byte & 0xF) if (a & 1) == 0 else (byte >> 4)
    return px


def encode_glyph(px: list[int]) -> bytes:
    """32x32 行主序 nibble 平面 → 512 字节 Morton 交织"""
    out = bytearray(512)
    for y in range(32):
        for x in range(32):
            a = NIBBLE_ADDR[y][x]
            nib = max(0, min(15, px[y * 32 + x]))
            if a & 1:
                out[a >> 1] = (out[a >> 1] & 0x0F) | (nib << 4)
            else:
                out[a >> 1] = (out[a >> 1] & 0xF0) | nib
    return bytes(out)


@dataclass
class Entry:
    idx: int
    code: int
    seq: int
    width_flag: int


# ---- Shift-JIS 双字节 slot↔code 映射 (FON 格式使用) ----
# CP932 有效双字节编码: lead 0x81-0x9F, 0xE0-0xEF; trail 0x40-0x7E, 0x80-0xFC
_SJIS_LEADS = tuple(range(0x81, 0xA0)) + tuple(range(0xE0, 0xF0))
_SJIS_TRAILS = tuple(range(0x40, 0x7F)) + tuple(range(0x80, 0xFD))


def _build_sjis_tables() -> tuple[list[int], dict[int, int]]:
    """构建 slot→code 列表和 code→slot 字典 (FON 使用全部 SJIS lead×trail, 不过滤)"""
    slot_to_code: list[int] = []
    code_to_slot: dict[int, int] = {}
    for lead in _SJIS_LEADS:
        for trail in _SJIS_TRAILS:
            code = (lead << 8) | trail
            code_to_slot[code] = len(slot_to_code)
            slot_to_code.append(code)
    return slot_to_code, code_to_slot


_SJIS_SLOT_TO_CODE, _SJIS_CODE_TO_SLOT = _build_sjis_tables()
SJIS_SLOT_COUNT = len(_SJIS_SLOT_TO_CODE)


def sjis_slot_to_code(slot: int) -> int:
    """FON 槽位 → Shift-JIS 双字节编码"""
    return _SJIS_SLOT_TO_CODE[slot]


def sjis_code_to_slot(code: int) -> int:
    """Shift-JIS 双字节编码 → FON 槽位 (找不到返回 -1)"""
    return _SJIS_CODE_TO_SLOT.get(code, -1)


class FontAll:
    def __init__(self, path: str):
        data = Path(path).read_bytes()
        if data[:4] != b"FONT":
            raise ValueError("不是 FONTALL.FNT (magic != FONT)")
        self.raw = bytearray(data)
        secs: dict[bytes, tuple[int, int]] = {}
        off = 8
        while off + 8 <= len(data):
            magic = bytes(data[off:off + 4])
            size = struct.unpack(">I", data[off + 4:off + 8])[0]
            secs[magic] = (off, size)
            off += 8 + size
        if b"FIDX" not in secs or b"FIMG" not in secs:
            raise ValueError("缺少 FIDX/FIMG 节")
        self.secs = secs
        fo, _ = secs[b"FIDX"]
        self.count = struct.unpack(">I", data[fo + 8:fo + 12])[0]
        self.max_width = struct.unpack(">H", data[fo + 12:fo + 14])[0]
        self.entries: list[Entry] = []
        for i in range(self.count):
            seq, wf = struct.unpack(">HH", data[fo + 16 + i * 4:fo + 20 + i * 4])
            self.entries.append(Entry(i, self.idx2code(i), seq, wf))
        io_, isz = secs[b"FIMG"]
        self.num_glyphs = struct.unpack(">I", data[io_ + 8:io_ + 12])[0]
        if isz - 8 < self.num_glyphs * 512:
            raise ValueError("FIMG 数据不足")

    @staticmethod
    def idx2code(idx: int) -> int:
        return ((idx >> 8) + 0x80) << 8 | (idx & 0xFF)

    @staticmethod
    def code2idx(code: int) -> int:
        return ((code >> 8) - 0x80) * 256 + (code & 0xFF)

    def blob(self, seq: int) -> bytes:
        io_, _ = self.secs[b"FIMG"]
        return bytes(self.raw[io_ + 16 + seq * 512: io_ + 16 + (seq + 1) * 512])

    def set_blob(self, seq: int, blob: bytes) -> None:
        io_, _ = self.secs[b"FIMG"]
        self.raw[io_ + 16 + seq * 512: io_ + 16 + (seq + 1) * 512] = blob

    # FontAll (PS2 FONTALL.FNT) nibble 语义: 0=浓墨, 15=背景
    NIBBLE_INVERTED = False


class FontFon:
    """FONT0.FON 等扁平字库: 无 magic/节结构, slot→code = 原始 Shift-JIS 双字节顺序"""

    def __init__(self, path: str):
        data = Path(path).read_bytes()
        if len(data) == 0 or len(data) % GLYPH_SIZE != 0:
            raise ValueError("不是有效的 FON 文件 (大小非 512 的倍数)")
        self.raw = bytearray(data)
        self.num_glyphs = len(data) // GLYPH_SIZE
        self.count = self.num_glyphs
        self.entries: list[Entry] = []
        for i in range(self.num_glyphs):
            # FON 每个 slot 有确定 SJIS 编码, 即使字形全零(如空格)也视为有效
            code = sjis_slot_to_code(i) if i < SJIS_SLOT_COUNT else 0xFFFF
            self.entries.append(Entry(i, code, i, 0))

    def blob(self, seq: int) -> bytes:
        return bytes(self.raw[seq * GLYPH_SIZE:(seq + 1) * GLYPH_SIZE])

    def set_blob(self, seq: int, blob: bytes) -> None:
        self.raw[seq * GLYPH_SIZE:(seq + 1) * GLYPH_SIZE] = blob

    # FontFon (DC FONT0-5.FON) nibble 语义: 0=背景, 15=浓墨
    NIBBLE_INVERTED = True


def open_font(path: str) -> FontAll | FontFon:
    """自动检测 FNT (有 FONT magic) 或 FON (扁平 512b 数组) 格式"""
    data = Path(path).read_bytes()
    if data[:4] == b"FONT":
        return FontAll(path)
    if len(data) % GLYPH_SIZE == 0:
        return FontFon(path)
    raise ValueError("无法识别的字库格式 (无 FONT magic 且非 512 的倍数)")


# ---------------- 码表 ----------------

def parse_tbl(path: Path) -> dict[int, str]:
    txt = path.read_text(encoding="utf-16", errors="ignore")
    out: dict[int, str] = {}
    for line in txt.splitlines():
        s = line.strip(" \t\r\n")   # 保留全角空格等字符
        if not s or "=" not in s:
            continue
        l, r = s.split("=", 1)
        l = l.strip()
        r = r.strip(" \t")   # 保留全角空格等字符
        if not l:
            continue
        try:
            code = int(l, 16)
        except ValueError:
            continue
        if r:
            out[code] = r[0]
    return out


def write_tbl(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines), encoding="utf-16")


# ---------------- 字形渲染 ----------------

def make_renderer(font_path: str, size_px: int, nibble_inverted: bool = False):
    """返回 render(ch) -> 512字节.

    FontFon (nibble_inverted=True, DC 漫画同人会): 游戏调色板为
      白色字身 + 橘黄描边。nibble 是调色板索引, 不是灰度:
        0        = 透明背景
        1..6     = 白色字身抗锯齿 (1 最淡)
        7        = 实心白色字身
        8..13(D) = 橘黄描边抗锯齿 (8 最淡)
        14 (E)   = 实心橘黄描边
        15 (F)   = 字库未使用
      实现: 以硬字身(覆盖率>=128)为界, 按距离分层填 nibble —
        字身=7, 字身边缘 AA=1-6, 距字身2px=实心橘黄 E,
        再向外 3px=9、4px=8 作描边外沿抗锯齿, 其余透明。

    FontAll (nibble_inverted=False, 樱花大战): 普通灰度字体
        0=浓墨, 15=背景, 线性灰度映射。
    """
    W = H = 32

    # ===================== FontFon: 白字身 + 橘黄描边 =====================
    if nibble_inverted:
        P_BG = 0x0
        P_WHITE = 0x7
        P_ORANGE = 0xE

        def white_pal(c: int) -> int:
            """字身覆盖率 0-255 → 白色调色板索引 1..7"""
            if c <= 0:
                return P_BG
            if c >= 200:
                return P_WHITE
            if c >= 150:
                return 6
            if c >= 110:
                return 5
            if c >= 80:
                return 4
            if c >= 55:
                return 3
            if c >= 32:
                return 2
            return 1

        def dilate(mask: list[int], w: int, h: int) -> list[int]:
            """3x3 最大池化: 把蒙版向外膨胀 1px"""
            out = [0] * (w * h)
            for y in range(h):
                for x in range(w):
                    m = 0
                    for dy in (-1, 0, 1):
                        ny = y + dy
                        if ny < 0 or ny >= h:
                            continue
                        base = ny * w
                        for dx in (-1, 0, 1):
                            nx = x + dx
                            if 0 <= nx < w:
                                v = mask[base + nx]
                                if v > m:
                                    m = v
                    out[y * w + x] = m
            return out

        def composite(wmask: list[int]) -> list[int]:
            """字身蒙版 + 橘黄描边 → 32x32 nibble 平面
            匹配原始剖面 (以硬字身边缘为界, 向外距离):
              距离0 = 实心白字身 7
              距离1 = 白字界面抗锯齿 (1-2, wmask>0)
              距离2 = 实心橘黄描边 E
              距离3 = 橘黄外沿 AA 9
              距离4 = 橘黄最外沿 AA 8
            用二进制距离分层, 避免软覆盖率扩散导致描边过厚。
            """
            core = [1 if c >= 128 else 0 for c in wmask]   # 硬字身形状
            d1 = dilate(core, W, H)                        # 距字身 ≤1px
            d2 = dilate(d1, W, H)                          # ≤2px (实心橘黄环)
            d3 = dilate(d2, W, H)                          # ≤3px (外沿 AA 9)
            d4 = dilate(d3, W, H)                          # ≤4px (最外沿 AA 8)
            tile = [P_BG] * (W * H)
            for i in range(W * H):
                if core[i]:
                    tile[i] = P_WHITE                      # 白字身
                elif wmask[i] > 0:
                    tile[i] = white_pal(wmask[i])          # 白字界面 AA (1-6)
                elif d2[i]:
                    tile[i] = P_ORANGE                     # 实心橘黄描边
                elif d3[i]:
                    tile[i] = 0x9                          # 橘黄外沿 AA
                elif d4[i]:
                    tile[i] = 0x8                          # 橘黄最外沿 AA
            return tile

        if freetype is not None:
            face = freetype.Face(font_path, index=0)
            face.set_pixel_sizes(0, size_px)

            def render_ft(ch: str) -> bytes:
                face.load_char(ch, freetype.FT_LOAD_RENDER | freetype.FT_LOAD_TARGET_NORMAL)
                slot = face.glyph
                bmp = slot.bitmap
                wmask = [0] * (W * H)
                asc = face.size.ascender >> 6
                desc = face.size.descender >> 6
                base = (H - (asc - desc)) // 2 + asc
                x0 = int(slot.bitmap_left)
                bw, bh = int(bmp.width), int(bmp.rows)
                if bw >= W:
                    x0 = 0
                else:
                    x0 = max(0, min(x0, W - bw))
                y0 = base - int(slot.bitmap_top)
                pitch = int(bmp.pitch)
                buf = bmp.buffer
                for y in range(bh):
                    ty = y0 + y
                    if ty < 0 or ty >= H:
                        continue
                    ro = ((bh - 1 - y) * (-pitch)) if pitch < 0 else y * pitch
                    for x in range(bw):
                        tx = x0 + x
                        if tx < 0 or tx >= W:
                            continue
                        # freetype: 255=墨, 0=透明 → 直接作字身覆盖率
                        wmask[ty * W + tx] = buf[ro + x]
                return encode_glyph(composite(wmask))

            return render_ft

        # PIL 回退
        from PIL import ImageFont
        font = ImageFont.truetype(font_path, size_px)

        def render_pil(ch: str) -> bytes:
            img = Image.new("L", (32, 32), 0)  # 0=透明背景
            d = ImageDraw.Draw(img)
            bb = d.textbbox((0, 0), ch, font=font)
            bw, bh = bb[2] - bb[0], bb[3] - bb[1]
            if bw > 0 and bh > 0:
                d.text((16 - (bb[0] + bw // 2), 16 - (bb[1] + bh // 2)),
                       ch, font=font, fill=255)
            # PIL: 255=墨(字身覆盖率), 0=透明
            wmask = list(img.getdata())
            return encode_glyph(composite(wmask))

        return render_pil

    # ===================== FontAll: 普通灰度 (0=墨, 15=背景) =====================
    INK, BG = 0, 15

    if freetype is not None:
        face = freetype.Face(font_path, index=0)
        face.set_pixel_sizes(0, size_px)

        def render_ft(ch: str) -> bytes:
            face.load_char(ch, freetype.FT_LOAD_RENDER | freetype.FT_LOAD_TARGET_NORMAL)
            slot = face.glyph
            bmp = slot.bitmap
            tile = [BG] * (W * H)
            asc = face.size.ascender >> 6
            desc = face.size.descender >> 6
            base = (H - (asc - desc)) // 2 + asc
            x0 = int(slot.bitmap_left)
            bw, bh = int(bmp.width), int(bmp.rows)
            if bw >= W:
                x0 = 0
            else:
                x0 = max(0, min(x0, W - bw))
            y0 = base - int(slot.bitmap_top)
            pitch = int(bmp.pitch)
            buf = bmp.buffer
            for y in range(bh):
                ty = y0 + y
                if ty < 0 or ty >= H:
                    continue
                ro = ((bh - 1 - y) * (-pitch)) if pitch < 0 else y * pitch
                for x in range(bw):
                    tx = x0 + x
                    if tx < 0 or tx >= W:
                        continue
                    # freetype 255=墨 → INK(0), 0=背景 → BG(15)
                    tile[ty * W + tx] = max(0, min(15,
                        INK + round((1 - buf[ro + x] / 255) * (BG - INK))))
            return encode_glyph(tile)

        return render_ft

    from PIL import ImageFont
    font = ImageFont.truetype(font_path, size_px)

    def render_pil(ch: str) -> bytes:
        img = Image.new("L", (32, 32), 255)  # 白色背景
        d = ImageDraw.Draw(img)
        bb = d.textbbox((0, 0), ch, font=font)
        bw, bh = bb[2] - bb[0], bb[3] - bb[1]
        if bw > 0 and bh > 0:
            d.text((16 - (bb[0] + bw // 2), 16 - (bb[1] + bh // 2)), ch, font=font, fill=0)
        gray = list(img.getdata())
        nibbles = [max(0, min(15, INK + round((1 - g / 255) * (BG - INK)))) for g in gray]
        return encode_glyph(nibbles)

    return render_pil


# ---------------- UI ----------------

COLS = 40
CELL_W, CELL_H = 34, 52


class FontAllApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("字库工具 — FONTALL.FNT / FONT0-5.FON"
                        + ("" if freetype else " [无freetype, 用PIL渲染]"))
        self.root.geometry("1280x800")
        self.fnt: FontAll | FontFon | None = None
        self.fnt_path: Path | None = None
        self.tbl: dict[int, str] = {}
        self.dirty = False
        self.sheet_photo = None
        self.big_photo = None
        self.cells: list[tuple[int, int, int, int, int]] = []
        self.selected: Entry | None = None
        self.font_map, self.font_names = scan_system_fonts()

        import configparser
        self.cfg = configparser.ConfigParser(interpolation=None)
        if INI_PATH.exists():
            self.cfg.read(INI_PATH, encoding="utf-8")
        if not self.cfg.has_section("build"):
            self.cfg.add_section("build")

        self.build_menu()
        self.build_ui()
        # 尝试打开默认字库
        for fname in FONT_FILES:
            if Path(fname).exists():
                self.open_path(fname)
                break

    def cfg_get(self, s: str, k: str, d: str = "") -> str:
        return self.cfg.get(s, k, fallback=d)

    def cfg_set(self, s: str, k: str, v: str) -> None:
        self.cfg.set(s, k, v)

    def cfg_save(self) -> None:
        with INI_PATH.open("w", encoding="utf-8") as f:
            self.cfg.write(f)

    def build_menu(self) -> None:
        m = tk.Menu(self.root)
        f = tk.Menu(m, tearoff=0)
        f.add_command(label="打开字库", command=self.open_fnt)
        f.add_command(label="导入码表", command=self.load_tbl)
        f.add_command(label="导出码表", command=self.export_tbl)
        f.add_separator()
        f.add_command(label="退出", command=self.on_exit)
        m.add_cascade(label="文件", menu=f)
        t = tk.Menu(m, tearoff=0)
        t.add_command(label="按码表批量渲染 (仅内存, 之后手动保存)", command=self.batch_render)
        m.add_cascade(label="工具", menu=t)
        self.root.config(menu=m)

    def build_ui(self) -> None:
        top = ttk.Frame(self.root, padding=6)
        top.pack(side="top", fill="x")
        ttk.Label(top, text="分区").pack(side="left")
        self.part_var = tk.StringVar()
        self.part_box = ttk.Combobox(top, textvariable=self.part_var, state="readonly", width=28)
        self.part_box.pack(side="left", padx=(6, 14))
        self.part_box.bind("<<ComboboxSelected>>", lambda e: self.draw_partition(self.part_var.get()))

        ttk.Label(top, text="字体").pack(side="left")
        self.font_name_var = tk.StringVar(value=self.cfg_get("build", "font_name", ""))
        fb = ttk.Combobox(top, textvariable=self.font_name_var, state="readonly", width=22)
        fb["values"] = self.font_names
        fb.pack(side="left", padx=(6, 4))
        ttk.Label(top, text="字号").pack(side="left")
        self.size_var = tk.StringVar(value=self.cfg_get("build", "size_px", "28"))
        ttk.Entry(top, textvariable=self.size_var, width=4).pack(side="left", padx=(6, 14))

        tk.Button(top, text="保存 (另存为)", command=self.save_as,
                  bg="#2e7d32", fg="white", padx=10).pack(side="right")
        self.info_var = tk.StringVar(value="请打开字库文件 (FNT 或 FON)")
        ttk.Label(top, textvariable=self.info_var).pack(side="right", padx=12)

        main = ttk.Frame(self.root, padding=(6, 0, 6, 6))
        main.pack(side="top", fill="both", expand=True)
        self.canvas = tk.Canvas(main, bg="#10141c", highlightthickness=0)
        vs = ttk.Scrollbar(main, orient="vertical", command=self.canvas.yview)
        hs = ttk.Scrollbar(main, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(0, weight=1)
        self.canvas.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        self.canvas.bind("<Shift-MouseWheel>", lambda e: self.canvas.xview_scroll(-1 if e.delta > 0 else 1, "units"))
        self.canvas.bind("<Button-1>", self.on_click)

        side = ttk.Frame(main, padding=(8, 0, 0, 0))
        side.grid(row=0, column=2, sticky="ns")
        ttk.Label(side, text="实际字形").pack(anchor="w")
        self.preview = tk.Canvas(side, width=160, height=160, bg="white",
                                 highlightthickness=1, highlightbackground="#444")
        self.preview.pack()
        self.l_detail = tk.Label(side, text="点击左侧字形选中", justify="left", anchor="w")
        self.l_detail.pack(anchor="w", pady=(8, 0))

        self.status = ttk.Label(self.root, text="就绪", anchor="w")
        self.status.pack(side="bottom", fill="x")

    # ---- 打开/浏览 ----

    def open_fnt(self) -> None:
        p = filedialog.askopenfilename(title="选择字库文件",
                                       filetypes=[("字库", "*.FNT;*.FON;*.fnt;*.fon"), ("All Files", "*.*")])
        if p:
            self.open_path(p)

    def open_path(self, p: str) -> None:
        try:
            self.fnt = open_font(p)
            self.fnt_path = Path(p)
            self.dirty = False
            es = self.fnt.entries
            nonempty = [e for e in es if e.seq != 0xFFFF]
            self.parts = {
                f"全部 ({len(nonempty)})": nonempty,
                "符号/数字/字母 0x81-0x82": [e for e in nonempty if 0x8140 <= e.code < 0x8300],
                "假名 0x83-0x84": [e for e in nonempty if 0x8340 <= e.code < 0x8500],
                "汉字区 0x85-0x98": [e for e in nonempty if 0x8540 <= e.code < 0x9900],
                "NEC扩展 0xE0+": [e for e in nonempty if e.code >= 0xE040],
            }
            self.part_box["values"] = list(self.parts.keys())
            self.part_var.set(list(self.parts.keys())[0])
            self.draw_partition(self.part_var.get())
            self.info_var.set(
                f"{Path(p).name} | 字形 {self.fnt.num_glyphs} | 码表 {len(self.tbl)} 条")
        except Exception as e:
            messagebox.showerror("错误", f"解析失败:\n{e}")

    def glyph_tile(self, seq: int, size: int = 32) -> Image.Image:
        px = decode_glyph(self.fnt.blob(seq))
        if getattr(self.fnt, "NIBBLE_INVERTED", False):
            # FontFon: nibble 是调色板索引, 不是灰度
            # 0=透明, 1-6=白字AA, 7=白字, 8-D=橘黄描边AA, E=橘黄描边
            # 白字: 7 纯白, 1-6 从极淡到浓的白色渐变 (以灰度值混入)
            # 橘黄: E=RGB(255,168,32) 游戏实测色, 8-D 是从极淡到浓的橘黄渐变
            W_AA = [(0, 0, 0)] + [(255, 255, 255) for _ in range(7)]  # 占位 0, 实际下面按比例算
            def white_color(v: int) -> tuple:
                if v <= 0:
                    return (0, 0, 0, 0)
                # 1→极淡白(rgba≈255,255,255,~40), 7→纯白不透明
                a = int(round(30 + (v - 1) / 6 * 225))
                return (255, 255, 255, a)
            def orange_color(v: int) -> tuple:
                if v <= 0:
                    return (0, 0, 0, 0)
                # E=RGB(255,168,32) 游戏参考, 8→极淡橘, E→满橘
                # v=8→alpha≈30, v=E(14)→alpha=255
                if v >= 14:
                    a = 255
                    r, g, b = 255, 168, 32
                else:
                    t = (v - 8) / (14 - 8)  # 0..1
                    a = int(round(30 + t * 225))
                    r = int(round(120 + t * 135))   # 淡橘→255
                    g = int(round(120 + t * 48))    # 120→168
                    b = int(round(120 - t * 88))    # 120→32
                return (r, g, b, a)
            data = []
            for v in px:
                if v == 0:
                    data.append((0, 0, 0, 0))     # 透明背景
                elif v <= 7:
                    data.append(white_color(v))   # 白字身
                elif v <= 0xE:
                    data.append(orange_color(v))  # 橘黄描边
                else:
                    data.append((0, 0, 0, 0))     # F 未使用
            tile = Image.new("RGBA", (32, 32))
            tile.putdata(data)
            if size != 32:
                tile = tile.resize((size, size), Image.NEAREST)
            return tile
        # FontAll: nibble=灰度 (0=墨, 15=背景)
        gray = [255 - v * 17 for v in px]
        tile = Image.new("L", (32, 32))
        tile.putdata(gray)
        if size != 32:
            tile = tile.resize((size, size), Image.NEAREST)
        return tile

    def draw_partition(self, name: str) -> None:
        if self.fnt is None:
            return
        items = self.parts.get(name, [])
        if not items:
            self.canvas.delete("all")
            return
        rows = math.ceil(len(items) / COLS)
        img = Image.new("RGB", (COLS * CELL_W, rows * CELL_H), (16, 20, 28))
        dr = ImageDraw.Draw(img)
        self.cells = []
        for i, ent in enumerate(items):
            tile = self.glyph_tile(ent.seq)
            x = (i % COLS) * CELL_W + 1
            y = (i // COLS) * CELL_H
            if tile.mode == "RGBA":
                # FontFon: RGBA tile, 用 alpha 通道混合到深蓝背景
                img.paste(tile, (x, y), tile.split()[-1])
            else:
                img.paste(Image.merge("RGB", (tile, tile, tile)), (x, y))
            ch = self.tbl.get(ent.code)
            label = f"{ent.code:04X}" + (f" {ch}" if ch else "")
            dr.text((x + 1, y + 33), label, fill=(120, 255, 140) if ch else (146, 177, 247))
            self.cells.append((x, y, x + 33, y + 32, ent.idx))
        self.sheet_photo = ImageTk.PhotoImage(img)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self.sheet_photo)
        self.canvas.configure(scrollregion=(0, 0, img.width, img.height))
        self.status.config(text=f"{name} | {len(items)} 字形"
                          + (" | 有未保存修改" if self.dirty else ""))

    def on_click(self, ev) -> None:
        for x0, y0, x1, y1, idx in self.cells:
            if x0 <= ev.x <= x1 and y0 <= ev.y <= y1:
                ent = self.fnt.entries[idx]
                self.selected = ent
                tile = self.glyph_tile(ent.seq, 160)
                if tile.mode == "RGBA":
                    # 大预览用白底(突出白字), 或深蓝(突出描边) — 选白底
                    bg = Image.new("RGB", (160, 160), (240, 240, 240))
                    bg.paste(tile, (0, 0), tile.split()[-1])
                    self.big_photo = ImageTk.PhotoImage(bg)
                else:
                    self.big_photo = ImageTk.PhotoImage(tile.convert("RGB"))
                self.preview.delete("all")
                self.preview.create_image(80, 80, image=self.big_photo)
                wdesc = {0x20: "全角32px", 0x10: "半角16px", 26: "窄26px"}.get(
                    ent.width_flag, f"标志{ent.width_flag:#06x}")
                self.l_detail.config(text=(
                    f"码位 {ent.code:04X} (索引 {ent.idx:#06x})\n"
                    f"序号 {ent.seq} | {wdesc}\n"
                    f"码表: {self.tbl.get(ent.code, '(无)')}"))
                self.root.clipboard_clear()
                self.root.clipboard_append(f"{ent.code:04X}")
                return

    # ---- 按码表批量渲染 (修改字库) ----

    def batch_render(self) -> None:
        if self.fnt is None:
            messagebox.showwarning("提示", "请先打开字库")
            return
        if not self.tbl:
            messagebox.showwarning("提示", "请先通过 文件->导入码表 加载码表")
            return
        name = self.font_name_var.get().strip()
        font_path = self.font_map.get(name, "")
        if not font_path:
            messagebox.showwarning("提示", "请先在顶部选择系统字体")
            return
        try:
            size = int(self.size_var.get().strip() or "28")
        except ValueError:
            size = 28
        self.cfg_set("build", "font_name", name)
        self.cfg_set("build", "font", font_path)
        self.cfg_set("build", "size_px", str(size))
        self.cfg_save()

        render = make_renderer(font_path, size, nibble_inverted=self.fnt.NIBBLE_INVERTED)
        done = skipped = 0
        for ent in self.fnt.entries:
            if ent.seq == 0xFFFF:
                continue
            ch = self.tbl.get(ent.code)
            if not ch:
                skipped += 1
                continue
            try:
                self.fnt.set_blob(ent.seq, render(ch))
                done += 1
            except Exception:
                skipped += 1
        self.dirty = True
        self.draw_partition(self.part_var.get())
        self.status.config(
            text=f"批量渲染完成: 替换 {done} 字, 码表未覆盖 {skipped} (保留原字形) — 未保存, 请点右上保存")

    # ---- 保存 ----

    def save_as(self) -> None:
        if self.fnt is None:
            messagebox.showwarning("提示", "请先打开字库")
            return
        is_fon = isinstance(self.fnt, FontFon)
        default_ext = ".FON" if is_fon else ".FNT"
        p = filedialog.asksaveasfilename(
            title="另存为", defaultextension=default_ext,
            initialfile=(self.fnt_path.stem + "_new") if self.fnt_path else f"FONT{default_ext}",
            filetypes=[("FON 字库", "*.FON;*.fon") if is_fon else ("FNT 字库", "*.FNT;*.fnt"),
                       ("All Files", "*.*")])
        if not p:
            return
        Path(p).write_bytes(bytes(self.fnt.raw))
        self.dirty = False
        self.status.config(text=f"已保存: {p}")

    def on_exit(self) -> None:
        if self.dirty:
            r = messagebox.askyesnocancel("未保存", "有已修改未保存的字形, 保存后退出?")
            if r is None:
                return
            if r:
                self.save_as()
                if self.dirty:  # 用户取消了另存为
                    return
        self.root.destroy()

    # ---- 码表 ----

    def load_tbl(self) -> None:
        if self.fnt is None:
            messagebox.showwarning("提示", "请先打开字库")
            return
        p = filedialog.askopenfilename(title="选择码表", filetypes=[("Table", "*.tbl"), ("All Files", "*.*")])
        if not p:
            return
        try:
            self.tbl = parse_tbl(Path(p))
            self.draw_partition(self.part_var.get())
            self.info_var.set(f"{self.fnt_path.name} | 字形 {self.fnt.num_glyphs} | 码表 {len(self.tbl)} 条")
            self.status.config(
                text=f"码表已加载 {len(self.tbl)} 条 — 工具->按码表批量渲染 后请手动保存")
        except Exception as e:
            messagebox.showerror("错误", str(e))

    def export_tbl(self) -> None:
        if self.fnt is None:
            messagebox.showwarning("提示", "请先打开字库")
            return
        out = filedialog.asksaveasfilename(
            title="导出码表", defaultextension=".tbl", initialfile="Out.tbl",
            filetypes=[("Table", "*.tbl"), ("All Files", "*.*")])
        if not out:
            return
        # 全部按 Shift-JIS(cp932) 解码填充实际编码; 与实际字形不符的自行校对
        lines: list[str] = []
        n_fill = 0
        for e in self.fnt.entries:
            if e.seq == 0xFFFF:
                continue
            ch = self.tbl.get(e.code, "")
            if not ch:
                try:
                    ch = bytes([e.code >> 8, e.code & 0xFF]).decode("cp932")
                    n_fill += 1
                except UnicodeDecodeError:
                    ch = ""
            lines.append(f"{e.code:04X}={ch}")
        write_tbl(Path(out), lines)
        messagebox.showinfo(
            "完成", f"已导出 {len(lines)} 个码位到:\n{out}\n"
            f"已按 Shift-JIS 填充 {n_fill} 条, 不符处请对照字形修改")


def main() -> int:
    root = tk.Tk()
    FontAllApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
