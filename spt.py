"""
spt.py -- spt script tool for Comic Party(Dreamcast 2001) / Comic Party DCE(2003) / Comic Party Portable（2006）

"""

import os
import struct
import hashlib

import tkinter as tk
from tkinter import filedialog, messagebox


# ════════════════════════════════════════════════════════════════════
# 第一部分: 编码转换
#
# SPT 文件内文本为 Shift-JIS 编码, 其中 F040-F044 为游戏自定义外字
# 标准 Shift-JIS 解码器无法识别, 需先映射再解码:
#   sjis2uni(sjis_bytes) -> str : SJIS 字节 (含外字) -> Unicode 字符串
# ════════════════════════════════════════════════════════════════════


# ─── 外字 (Gaiji) 映射 ───────────────────────────────────────────────
# SPT 文本为 Shift-JIS 编码, 其中 F040-F044 位于 Shift-JIS 用户定义区
GAIJI_MAP = {
    0xF040: "①",
    0xF041: "②",
    0xF042: "③",
    0xF043: "④",
    0xF044: "⑤",
}


def sjis2uni(sjis_bytes: bytes) -> str:
    """
    SJIS 字节 -> Unicode 字符串。
    先在字节层面映射外字 F040-F044 (①-⑤), 其余按 Shift-JIS 解码;
    外字双字节必须在解码前替换, 否则 F0  lead 字节会被解码器当作非法序列。
    """
    out = []
    i, n = 0, len(sjis_bytes)
    while i < n:
        b = sjis_bytes[i]
        # Shift-JIS 双字节前导范围: 0x81-0x9F / 0xE0-0xFC
        if (0x81 <= b <= 0x9F or 0xE0 <= b <= 0xFC) and i + 1 < n:
            code = (b << 8) | sjis_bytes[i + 1]
            ch = GAIJI_MAP.get(code)
            if ch is not None:
                out.append(ch)
                i += 2
                continue
            out.append(sjis_bytes[i:i + 2].decode("shift_jis", errors="replace"))
            i += 2
        else:
            out.append(sjis_bytes[i:i + 1].decode("shift_jis", errors="replace"))
            i += 1
    return "".join(out)


# ─── 外部码表 (.tbl) ────────────────────────────────────────────────
# 导入翻译文本时可选用外部码表替代 GBK 默认编码:
#   - 码表文件 .tbl 以 UTF-16 编码 (Windows 记事本 "Unicode" 保存)
#   - 行格式 'XX=char': 左侧十六进制字节序列, 右侧 Unicode 字符
#     (自动检测: 若右侧为合法十六进制则互换, 兼容 'char=XX' 格式)
#   - 构建 {Unicode char: bytes} 映射, 导入时将译文逐字符转为字节写入 SPT
# tbl_map / tbl_loaded_path 定义于第二部分模块级全局变量区


def load_tbl(tbl_path: str) -> bool:
    """
    加载外部码表 (.tbl, UTF-16 编码) 到全局 tbl_map。
    空行及以 # 或 // 开头的行忽略。
    """
    global tbl_map
    tbl_map = {}
    with open(tbl_path, "r", encoding="utf-16") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if not line or line.startswith("#") or line.startswith("//"):
                continue
            if "=" not in line:
                continue
            left, right = line.split("=", 1)
            left = left.strip()  # hex 部分允许 strip 去空白
            # right 不 strip: 字符可能是全角空格等空白字符, strip 会吃掉
            right = right.rstrip("\r\n")
            # 判断哪一侧为十六进制字节序列
            try:
                bytes.fromhex(left)
                hex_part, char_part = left, right
            except ValueError:
                try:
                    bytes.fromhex(right)
                    hex_part, char_part = right, left
                except ValueError:
                    continue
            if not char_part:
                continue
            try:
                byte_seq = bytes.fromhex(hex_part)
            except ValueError:
                continue
            tbl_map[char_part[0]] = byte_seq
    return True


def uni_to_tbl_bytes(text: str, missing: list = None) -> bytes:
    """
    使用码表将 Unicode 文本逐字符转为字节序列。
    - 码表中找到的字符: 按 tbl_map 映射输出
    - 未找到的字符: 回退为 GBK 编码 (防止文本截断); 若提供 missing 则记录
    """
    out = bytearray()
    for ch in text:
        b = tbl_map.get(ch)
        if b is not None:
            out.extend(b)
        else:
            if missing is not None:
                missing.append(ch)
            try:
                out.extend(ch.encode("gbk"))
            except UnicodeEncodeError:
                pass
    return bytes(out)


# ════════════════════════════════════════════════════════════════════
# 第二部分: SPT 文件访问
#
# 忠实保留 Delphi 单元 FileAccess.pas 的原始逻辑:
#   - 模块级全局变量 (对应 Delphi unit var 段)
#   - spt_open  : 解析 SPT 文件, 提取日文文本
#   - get_str   : 从文件流读取一个长度前缀字符串
#   - import_spt: 导入翻译文本并重建 SPT
#   - read_str  : 解析翻译文本中的一行
#
# 关键逻辑对应说明:
#   Delphi AnsiString 为字节串 (中文 Windows 上 GBK 编码)。
#   Python 用 bytes 表示原始字节, str 表示 Unicode 文本。
#
#   readStr (Delphi):
#     p := strPos(pchar(s), '●');   // 在 GBK 字节串中查找 '●' (2 字节)
#     p := p + 7;                   // 跳过 '●NNN●' = 2+1+1+1+2 = 7 字节
#     cnTxt[index] := p;            // 剩余部分即为译文 (GBK 字节串)
#
#   read_str (Python):
#     s 已是 Unicode 字符串 (从 GBK 文件读入)。
#     '●NNN●' 在 Unicode 中占 5 个字符 (●=1, N=1×3, ●=1)。
#     因此 idx + 5 对应 Delphi 的 p + 7。
#
#   Import (Delphi):
#     p := pchar(cnTxt[txtIndex]);        // GBK 字节指针
#     strLen := Length(cnTxt[txtIndex]);  // 字节长度
#     data.WriteBuffer(p^, strLen);       // 直接写入 GBK 字节
#     data.WriteBuffer(zero, 4 - (strLen mod 4));  // 填充至 4 字节对齐
#
#   import_spt (Python):
#     txt_bytes := cn_txt[txt_index].encode('gbk')  // str -> GBK 字节
#     str_len := len(txt_bytes)                     // 字节长度
#     写入 str_len + txt_bytes + 填充字节
#     填充: 4 - (str_len % 4), 当 str_len 为 4 的倍数时填 4 字节 (与 Delphi mod 一致)
# ════════════════════════════════════════════════════════════════════


# ─── MD5 ────────────────────────────────────────────────────────────
def md5_file(filename: str) -> str:
    """对应 Delphi md5 单元: MD5Print(MD5File(FileName))"""
    h = hashlib.md5()
    with open(filename, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest().upper()


# ─── 常量 ────────────────────────────────────────────────────────────
# 对应 Delphi: const codeLen: array[0..$8C] of Integer
code_len = [
    0, 0, 4, -4, 8, 8, 4, 4, 4, 4, -8, 8, 4, -4, 8, 4, 8, 0, 4, 4, 12, 16, 12, 8, 4, 0,
    -8, 0, 0, 0, 0, 8, 8, 8, 8, 4, -4, 0, 0, 0, 0, 0, 0, 4, 4, 0, 0, 4, 0, 4, -4, 4, 0, 4, 0,
    4, 0, 8, 8, 4, 4, 4, 8, 8, 12, 12, 12, 12, 12, 12, -4, -4, -4, 12, 12, 12, 12, -4,
    -4, -4, -4, 8, -4, -4, 8, 8, -4, 8, 8, -4, 8, 4, 0, 0, 0, 0, 4, 0, -4, -4, -4, 0, 0, 8,
    4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 0, -4, 8, 0, 8, -4, 0, 0, 8, 4, 4, 0, 4, 4, 0, 0, 4,
    -4, 0, -4, 0, 0, 0, 4, 0,
]

# 对应 Delphi: const charName: array[0..60] of String
char_name = [
    '主角', '瑞希', '南', '由宇', '詠美', '彩', 'あさひ', '玲子',
    '千紗', '郁美', '大志', '謎の男', '運送屋さん', '編集長',
    'おたく', 'すばる', 'スタッフ', '女の子の声', '女の子の声Ａ',
    '女の子の声Ｂ', '女の子の声Ｃ', '客', '電話', '？', '従業員',
    '子供', 'おっちゃん', 'ねえちゃん', '母親', '女の子', '店員',
    '司会', '解説', '売り子', 'ウェイトレス', '仲居', 'モモ',
    '男の声', 'まひる', 'へもへも', 'レポーター', '女の人の声',
    'コスプレイヤー', '三人組', '美穂', 'まゆ', '夕香', 'パパ',
    'ママ', '女の人', '駅員', '先輩', '印刷所の子', '放送',
    'お父さん', 'お母さん', 'おたくたて', 'おたくよこ',
    'ジャッキー', 'よっしー', '南',
]


# ─── Opr 记录 ────────────────────────────────────────────────────────
class Opr:
    """对应 Delphi: type opr = record code, startPos, Len: Integer; end;"""
    def __init__(self):
        self.code = 0
        self.start_pos = 0
        self.len = 0


# ─── 模块级全局变量 (对应 Delphi unit var 段) ────────────────────────
ts = []                          # TStrings  (TStringList)
fs = None                        # TFileStream
jp_txt = [""] * 1000             # array[1..999] of String  → Python 用 0 偏移, 索引 0 不用
cn_txt = [""] * 1000             # array[1..999] of String
part_count = 0                   # Integer
offset = [0] * 100               # array[0..99] of Integer
txt_count = 0                    # Integer
cn_check = [False] * 1000       # array[1..999] of boolean
script = []                      # array[1..100000] of Opr  → Python 动态列表
scr_count = 0                    # Integer
illegal_log_lines = []           # 非法字节日志累积 (每次 import 独立清空, 写入 txt 同级目录)
tbl_map = {}                       # 外部码表映射: {Unicode char: bytes} (导入时编码用)


# ─── getStr ──────────────────────────────────────────────────────────
def get_str():

    global fs
    str_len_data = fs.read(4)
    if len(str_len_data) < 4:
        return True, ""
    str_len = struct.unpack("<I", str_len_data)[0]
    if str_len > 1000 or str_len < 0:
        return True, ""
    length = (str_len // 4) * 4 + 4
    buffer = fs.read(length)
    if len(buffer) < length:
        return True, ""
    s_raw = buffer[:str_len]
    if len(s_raw) != str_len:
        return True, ""
    s = sjis2uni(s_raw)
    return False, s


# ─── sptOpen ─────────────────────────────────────────────────────────
def spt_open(filename: str):
    """
    解析 SPT 文件结构:
      4 字节 partCount + partCount×4 字节 part 偏移表 + 指令流
    指令码:
      $0A  : 选项 (ItemCount + 多个 getStr)
      $1A  : 对话 (getStr + 16 字节尾部)
      $8C  : 文件结束标记
      99999: Part 分隔
      其他 : 按 codeLen[code] 跳过
    """
    global ts, fs, jp_txt, cn_txt, part_count, txt_count, scr_count, script

    scr_count = 0
    txt_count = 0
    ts = []
    script = []
    ts.append(f"{os.path.basename(filename)}:{md5_file(filename)}")
    ts.append("")

    fs = open(filename, "rb")

    # 读取 partCount
    part_count_data = fs.read(4)
    part_count = struct.unpack("<I", part_count_data)[0]

    # 跳过 part 偏移表
    for _ in range(part_count):
        fs.read(4)

    file_size = os.path.getsize(filename)
    while fs.tell() < file_size:
        pos = fs.tell()
        code_data = fs.read(4)
        if len(code_data) < 4:
            break
        code = struct.unpack("<I", code_data)[0]
        scr_count += 1

        op = Opr()
        op.code = code
        op.start_pos = fs.tell()
        script.append(op)

        if code == 0x0A:
            item_count_data = fs.read(4)
            item_count = struct.unpack("<I", item_count_data)[0]
            if item_count < 2 or item_count > 20:
                ts.append("0A error")
                continue
            ts.append("//选项")
            for _ in range(item_count):
                error, s = get_str()
                if error:
                    ts.append(f"{pos:08X}H:")
                    ts.append("0A error")
                    fs.seek(pos + 4)
                else:
                    txt_count += 1
                    jp_txt[txt_count] = s
                    cn_txt[txt_count] = ""
                    ts.append(f"○{txt_count:03d}○{s}")
                    ts.append(f"●{txt_count:03d}●")
            ts.append("")

        elif code == 0x1A:
            error, s = get_str()
            if error:
                ts.append(f"{pos:08X}H:")
                ts.append("1A error")
                fs.seek(pos + 4)
            else:
                txt_count += 1
                jp_txt[txt_count] = s
                cn_txt[txt_count] = ""
                ts.append(f"○{txt_count:03d}○{s}")
                ts.append(f"●{txt_count:03d}●")
                ts.append("")
                fs.seek(fs.tell() + 16)

        elif code == 0x8C:
            if fs.tell() < file_size:
                d1_data = fs.read(4)
                d1 = struct.unpack("<I", d1_data)[0]
                if d1 == 0x8C:
                    ts.append("------------End-------------")
                else:
                    ts.append("fileEnd error")
            else:
                ts.append("------------End-------------")

        elif code == 99999:
            d1_data = fs.read(4)
            d1 = struct.unpack("<I", d1_data)[0]
            ts.append("------------------------------")
            ts.append(f"part{d1 + 1}:")

        else:
            if code > 0x8C or code < 0:
                ts.append(f"{pos:08X}: Out of Index")
            else:
                fs.seek(fs.tell() + code_len[code])

        op.len = fs.tell() - op.start_pos

    fs.close()


# ─── readStr ─────────────────────────────────────────────────────────
def read_str(s: str):
    """
    原始逻辑 (AnsiString 为 GBK 字节串):
      p := strPos(pchar(s), '●');    // 查找 '●' (GBK 中 2 字节)
      num[i] := p[i+2];              // 取 '●' 后第 2~4 字节 = NNN
      Index := StrToInt(num);
      p := p + 7;                    // 跳过 '●NNN●' = 2+3+2 = 7 字节
      cnTxt[index] := p;             // 剩余部分即译文
      cnCheck[index] := true;

    Python 中 s 为 Unicode 字符串 (从 GBK 文件读入并解码)。
    '●NNN●' 在 Unicode 中占 5 个字符 (●×2 + N×3 = 5),
    因此 idx+5 对应 Delphi 的 p+7。
    """
    global cn_txt, cn_check
    if not s:
        return
    idx = s.find("●")
    if idx == -1:
        return
    try:
        num_str = s[idx + 1: idx + 4]   # '●' 后 3 个字符 = NNN
        index = int(num_str)
    except ValueError:
        return
    text = s[idx + 5:]                   # 跳过 '●NNN●' (5 个 Unicode 字符 = 7 个 GBK 字节)
    cn_txt[index] = text
    cn_check[index] = True


# ─── Import (码表版) ─────────────────────────────────────────────────
def import_spt_tbl(filename: str) -> bool:
    """
    使用外部码表导入翻译文本 (码表需预先通过 load_tbl 加载)。

    与 import_spt 的区别:
      - 翻译 txt 以 UTF-8 读取 (不使用 GBK 默认编码)
      - 输出编码优先使用码表映射 {Unicode char: bytes} (不使用 GBK)
      - 码表中未找到的字符回退为 GBK 编码, 汇总到 log.txt
      - log.txt 输出到 spt.py 同级目录
    """
    global ts, fs, cn_check, offset, script, txt_count, part_count, scr_count, illegal_log_lines

    if not tbl_map:
        raise RuntimeError("码表未加载, 请先用「加载码表(.tbl)」")

    spt_file = os.path.splitext(filename)[0] + ".spt"
    spt_open(spt_file)

    txt_index = 0

    for i in range(1, txt_count + 1):
        cn_check[i] = False

    for i in range(100):
        offset[i] = 0xCCCCCCCC

    # 清空上一批的日志累积 (每次 import 独立写 log.txt 到 txt 同级目录)
    illegal_log_lines = []

    # 以 UTF-8 读取翻译文本, 失败则回退 GBK (兼容混合编码的 txt)
    try:
        with open(filename, "r", encoding="utf-8-sig") as f:
            source_lines = f.readlines()
    except UnicodeDecodeError:
        with open(filename, "r", encoding="gbk", errors="surrogateescape") as f:
            source_lines = f.readlines()

    for i in range(1, len(source_lines)):
        line = source_lines[i].strip()
        if line.startswith("○"):
            continue
        read_str(line)

    # 检查是否所有文本都已导入
    for i in range(1, txt_count + 1):
        if not cn_check[i]:
            print(f"[WARN] cn_check[{i}] = False")

    # 编码辅助: 用码表将 Unicode 文本转为字节, 同时收集未映射字符
    missing_chars = []

    def _encode(text: str) -> bytes:
        return uni_to_tbl_bytes(text, missing_chars)

    # 构建 Header 和 Data 流
    header_stream = bytearray()
    data_stream = bytearray()

    fs = open(spt_file, "rb")

    for i in range(scr_count):
        op = script[i]
        fs.seek(op.start_pos)
        data_stream.extend(struct.pack("<I", op.code))

        if op.code == 0x0A:
            item_count_data = fs.read(4)
            item_count = struct.unpack("<I", item_count_data)[0]
            data_stream.extend(struct.pack("<I", item_count))
            for _ in range(item_count):
                txt_index += 1
                txt_bytes = _encode(cn_txt[txt_index])
                str_len = len(txt_bytes)
                data_stream.extend(struct.pack("<I", str_len))
                data_stream.extend(txt_bytes)
                pad = 4 - (str_len % 4)
                data_stream.extend(b"\x00" * pad)

        elif op.code == 0x1A:
            txt_index += 1
            txt_bytes = _encode(cn_txt[txt_index])
            str_len = len(txt_bytes)
            data_stream.extend(struct.pack("<I", str_len))
            data_stream.extend(txt_bytes)
            pad = 4 - (str_len % 4)
            data_stream.extend(b"\x00" * pad)
            fs.seek(op.start_pos + op.len - 16)
            tail16 = fs.read(16)
            data_stream.extend(tail16)

        elif op.code == 99999:
            part_index_data = fs.read(4)
            part_index = struct.unpack("<I", part_index_data)[0]
            offset[part_index] = len(data_stream) - 4
            data_stream.extend(struct.pack("<I", part_index))

        else:
            buf = fs.read(op.len)
            data_stream.extend(buf)

    fs.close()

    # 未映射字符汇总到 spt.py 同级目录 log.txt
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "log.txt")
    if missing_chars:
        illegal_log_lines.append(f"── {os.path.basename(filename)} (码表未映射) ──")
        unique = {}
        for ch in missing_chars:
            unique[ch] = unique.get(ch, 0) + 1
        for ch, n in unique.items():
            illegal_log_lines.append(
                f"[MISSING] 字符 '{ch}' (U+{ord(ch):04X}) 出现 {n} 次"
            )
    # 无论是否有 missing_chars, 只要有日志内容就写入 (追加模式, 批量导入时同目录文件共用一个 log.txt)
    if illegal_log_lines:
        with open(log_path, "a", encoding="gbk") as f:
            f.write("\n".join(illegal_log_lines) + "\n")
        if missing_chars:
            print(
                f"[MISSING] {os.path.basename(filename)}: {len(unique)} 种字符未在码表中, 详见 {log_path}"
            )

    # 写 Header: partCount + offset 表
    header_stream.extend(struct.pack("<I", part_count))
    for i in range(part_count):
        header_stream.extend(struct.pack("<I", offset[i]))

    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spt_out")
    os.makedirs(out_dir, exist_ok=True)
    new_file = os.path.join(out_dir, os.path.basename(spt_file))

    with open(new_file, "wb") as dest:
        dest.write(header_stream)
        dest.write(data_stream)

    return True


# ════════════════════════════════════════════════════════════════════
# 第三部分: 主窗体

class MainForm(tk.Tk):

    def __init__(self):
        super().__init__()

        # 窗口标题与大小 (对应 DFM 中的 Caption / Width / Height)
        self.title("SPT脚本工具")
        self.geometry("800x600")

        # ─── MainMenu1 (对应 Delphi MainMenu1) ───
        menubar = tk.Menu(self)

        # N1: 文件菜单
        file_menu = tk.Menu(menubar, tearoff=0)

        # N1 → Button1Click: 打开SPT并输出日志
        file_menu.add_command(label="打开并预览单个SPT文件", command=self.button1_click)

        # N2 → N2Click: 批量解析SPT导出日文文本(UTF-8 → txt_jp)
        file_menu.add_command(label="批量导出SPT日文文本(UTF-8)", command=self.n2_click)
	
        menubar.add_cascade(label="导出日文文本（UTF-8）", menu=file_menu)
	
        # N4: 文件2菜单 (外部码表导入)
        file_menu2 = tk.Menu(menubar, tearoff=0)
        file_menu2.add_command(label="1.选择自定义JIS码表(.tbl)", command=self.select_tbl)
        file_menu2.add_command(
            label="2.批量导入DC版中文文本", command=self.n4_click
        )
        menubar.add_cascade(label="导入文本(UTF-8+自定义码表)", menu=file_menu2)

        self.config(menu=menubar)

        # ─── ListBox1 (对应 Delphi ListBox1) ───
        self.lb1 = tk.Listbox(self, font=("SimSun", 10))
        self.lb1.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        # ─── 启动时自动加载 spt.py 同级目录下的 .tbl 文件 ───
        script_dir = os.path.dirname(os.path.abspath(__file__))
        tbl_files = [f for f in os.listdir(script_dir) if f.lower().endswith(".tbl")]
        if tbl_files:
            tbl_path = os.path.join(script_dir, tbl_files[0])
            try:
                load_tbl(tbl_path)
                self.lb1.insert(tk.END, f"自动加载码表: {tbl_files[0]} ({len(tbl_map)} 条)")
            except Exception as e:
                self.lb1.insert(tk.END, f"码表加载失败: {tbl_files[0]}")

    # ─── Button1Click ───────────────────────────────────────────────
    def button1_click(self):

        path = filedialog.askopenfilename(filetypes=[("SPT脚本文件", "*.spt")])
        if not path:
            return

        spt_open(path)

        # ts.SaveToFile(ChangeFileExt(FileName, '.txt')) — 以 GBK 编码保存
        log_file = os.path.splitext(path)[0] + ".txt"
        with open(log_file, "w", encoding="utf-8") as f:
            f.write("\n".join(ts))

        # ListBox1.Clear + Items.Add
        self.lb1.delete(0, tk.END)
        for i in range(1, txt_count + 1):
            self.lb1.insert(tk.END, jp_txt[i])

    # ─── N2Click (批量解析 SPT, UTF-8 导出到 txt_jp) ─────────────────
    def n2_click(self):
        """
        批量选择 SPT 脚本 (多选), 解析提取日文文本, 以 UTF-8 编码导出到
        各 SPT 同级目录的 txt_jp 子文件夹 (不写 GBK 日志)。
        外字 F040-F044 已在解码阶段映射为 ①-⑤。
        """
        file_list = filedialog.askopenfilenames(filetypes=[("SPT脚本文件", "*.spt")])
        if not file_list:
            return

        done = 0
        errors = []
        for path in file_list:
            try:
                spt_open(path)
                out_dir = os.path.join(os.path.dirname(path), "txt_jp")
                os.makedirs(out_dir, exist_ok=True)
                out_file = os.path.join(
                    out_dir,
                    os.path.splitext(os.path.basename(path))[0] + ".txt",
                )
                # 提取文本以 UTF-8 编码导出 (区别于 button1_click 的 GBK 日志)
                with open(out_file, "w", encoding="utf-8") as f:
                    f.write("\n".join(ts))
                done += 1
            except Exception as e:
                errors.append(f"{os.path.basename(path)}: {e}")

        # 列表框显示最后解析文件的日文文本
        self.lb1.delete(0, tk.END)
        for i in range(1, txt_count + 1):
            self.lb1.insert(tk.END, jp_txt[i])

        msg = f"已导出 {done}/{len(file_list)} 个文件到 txt_jp 文件夹 (UTF-8)"
        if errors:
            messagebox.showwarning("完成(含错误)", msg + "\n\n失败:\n" + "\n".join(errors))
        else:
            messagebox.showinfo("完成", msg)

    # ─── 文件2: 选择码表(.tbl) ──────────────────────────────────────────
    def select_tbl(self):
        """
        选择外部码表文件 (.tbl, UTF-16 编码) 并加载到全局 tbl_map。
        码表行格式 'XX=char', 自动检测反向格式 'char=XX'。
        """
        path = filedialog.askopenfilename(filetypes=[("码表文件", "*.tbl")])
        if not path:
            return
        try:
            load_tbl(path)
        except Exception as e:
            messagebox.showerror("码表加载失败", f"{path}\n{e}")
            return
        self.lb1.delete(0, tk.END)
        self.lb1.insert(tk.END, f"已加载码表: {os.path.basename(path)}")
        self.lb1.insert(tk.END, f"映射条目数: {len(tbl_map)}")
        messagebox.showinfo("码表", f"已加载 {len(tbl_map)} 条映射\n{path}")

    # ─── 文件2: 批量导入中文文本(UTF-8+码表) ──────────────────────────
    def n4_click(self):
        """
        使用外部码表批量导入翻译文本:
          - 翻译 txt 以 UTF-8 读取
          - 输出编码优先使用码表映射 {Unicode char: bytes}
          - 码表未覆盖字符回退为 GBK 编码
          - 输出到 spt_out 子目录, log.txt 写到 spt.py 同级目录
        """
        if not tbl_map:
            messagebox.showwarning("提示", "请先选择码表(.tbl)文件")
            return

        file_list = filedialog.askopenfilenames(
            filetypes=[("翻译文本(UTF-8)", "*.txt")]
        )
        if not file_list:
            return

        for fpath in file_list:
            try:
                import_spt_tbl(fpath)
            except Exception as e:
                messagebox.showerror("错误", f"{fpath}\n{str(e)}")

        messagebox.showinfo("完成", "全部处理完成，输出在同目录spt_out文件夹")

if __name__ == "__main__":
    app = MainForm()
    app.mainloop()
