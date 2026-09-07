"""
ZT1 <-> PNG converter (compressed PVR images).

Support Comic Party,Dreamcast,2001,ZT1 file

ZT1 file layout:
  - ZT10 (640x512 UI canvas):
      16-byte header: "ZT10" + u32(compressed_len-6) + u64(decompressed_len)
      then zlib-compressed payload (concatenated PVR files).
  - ZT11 / ZT12 (character tachi-e, e.g. ASA01_1.ZT1 / BAN01_1.ZT1):
      16-byte header: "ZT1x" + u32(compressed_len) + u32(decompressed_len)
                      + u32(metadata_table_len)
      then metadata_table (metadata_table_len bytes; not needed for decode)
      then zlib-compressed payload (concatenated PVR files, ARGB4444).
      file is zero-padded to a multiple of 4.

Each PVR file:
  - 16-byte GBIX section: "GBIX" + u32(8) + u64(global_index)
  - 16-byte PVRT header: "PVRT" + u32(size_field) + 4-byte type + u16(width) + u16(height)
      size_field = 8 + pixel_data_length
  - Pixel data (width*height*2 bytes, square-twiddled Morton order)

Pixel format is determined by type_field[0]:
  0x01 = RGB565  (ZT10 files)
  0x02 = ARGB4444 (ZT11 / ZT12 tachi-e files, e.g. character 立绘)
type_field[1] = 0x01 (SQUARE TWIDDLED) for all supported files.

Usage:
  python zt1.py d filename.zt1   -> decode to filename.png
  python zt1.py d filename.png   -> encode to filename.zt1

Direction is auto-detected from the input file extension (.zt1 -> decode,
.png -> encode). The first command argument is accepted but ignored;
the operation is determined by the file type.
"""
import os
import struct
import sys
import zlib

import numpy as np
from PIL import Image


# ---------------------------------------------------------------------------
# Shared helpers (Morton/Z-order twiddling and RGB565 conversion)
# ---------------------------------------------------------------------------

def morton_index(x, y, bits):
    """Square-twiddled Morton/Z-order: x at odd bit positions, y at even bit positions."""
    result = 0
    for i in range(bits):
        result |= ((y >> i) & 1) << (2 * i)
        result |= ((x >> i) & 1) << (2 * i + 1)
    return result


def build_morton_lut(size):
    """Precompute Morton index lookup table for a square image of given size."""
    bits = (size - 1).bit_length() if size > 1 else 0
    lut = np.zeros(size * size, dtype=np.uint32)
    for y in range(size):
        for x in range(size):
            lut[y * size + x] = morton_index(x, y, bits)
    return lut


def rgb565_to_rgba8888(pixel_arr):
    """Convert RGB565 (LE 2-byte) pixel array to RGBA8888 (alpha = 255)."""
    vals = (pixel_arr[:, :, 1].astype(np.uint32) << 8) | pixel_arr[:, :, 0].astype(np.uint32)
    r5 = ((vals >> 11) & 0x1F) * 255 // 31
    g6 = ((vals >> 5) & 0x3F) * 255 // 63
    b5 = (vals & 0x1F) * 255 // 31
    a8 = np.full(r5.shape, 255, dtype=np.uint8)
    return np.stack([r5.astype(np.uint8), g6.astype(np.uint8),
                     b5.astype(np.uint8), a8], axis=-1)


def argb4444_to_rgba8888(pixel_arr):
    """Convert ARGB4444 (LE 2-byte) pixel array to RGBA8888 (alpha preserved)."""
    vals = (pixel_arr[:, :, 1].astype(np.uint32) << 8) | pixel_arr[:, :, 0].astype(np.uint32)
    a4 = (vals >> 12) & 0xF
    r4 = (vals >> 8) & 0xF
    g4 = (vals >> 4) & 0xF
    b4 = vals & 0xF
    # 4-bit -> 8-bit: replicate nibble (v*17)
    return np.stack([
        (r4 * 17).astype(np.uint8),
        (g4 * 17).astype(np.uint8),
        (b4 * 17).astype(np.uint8),
        (a4 * 17).astype(np.uint8),
    ], axis=-1)


def rgba8888_to_rgb565(rgba):
    """Convert RGBA8888 array (H, W, 4) to RGB565 little-endian bytes (H, W, 2)."""
    r = rgba[:, :, 0].astype(np.uint32)
    g = rgba[:, :, 1].astype(np.uint32)
    b = rgba[:, :, 2].astype(np.uint32)
    r5 = (r * 31 // 255) & 0x1F
    g6 = (g * 63 // 255) & 0x3F
    b5 = (b * 31 // 255) & 0x1F
    val = (r5 << 11) | (g6 << 5) | b5
    low = (val & 0xFF).astype(np.uint8)
    high = ((val >> 8) & 0xFF).astype(np.uint8)
    return np.stack([low, high], axis=-1)


def rgba8888_to_argb4444(rgba):
    """Convert RGBA8888 array (H, W, 4) to ARGB4444 little-endian bytes (H, W, 2).
    Alpha is quantized from 8-bit to 4-bit (v * 15 // 255).
    """
    r = rgba[:, :, 0].astype(np.uint32)
    g = rgba[:, :, 1].astype(np.uint32)
    b = rgba[:, :, 2].astype(np.uint32)
    a = rgba[:, :, 3].astype(np.uint32)
    a4 = (a * 15 // 255) & 0xF
    r4 = (r * 15 // 255) & 0xF
    g4 = (g * 15 // 255) & 0xF
    b4 = (b * 15 // 255) & 0xF
    val = (a4 << 12) | (r4 << 8) | (g4 << 4) | b4
    low = (val & 0xFF).astype(np.uint8)
    high = ((val >> 8) & 0xFF).astype(np.uint8)
    return np.stack([low, high], axis=-1)


# PVR pixel format identifiers (type_field[0])
FMT_RGB565 = 0x01
FMT_ARGB4444 = 0x02


def pvr_to_rgba8888(pixel_arr, fmt):
    """Dispatch to the right 2bpp -> RGBA8888 converter based on pixel format byte."""
    if fmt == FMT_ARGB4444:
        return argb4444_to_rgba8888(pixel_arr)
    # default / FMT_RGB565
    return rgb565_to_rgba8888(pixel_arr)


def untwiddle(pixel_bytes, size):
    """Untwiddle square-twiddled pixel data into row-major order.

    Inverse of twiddle. Returns numpy array of shape (size, size, 2).
    """
    arr = np.frombuffer(pixel_bytes, dtype=np.uint8).reshape(-1, 2)
    lut = build_morton_lut(size)
    ordered = arr[lut]
    return ordered.reshape(size, size, 2)


def twiddle(row_major_pixels, size):
    """Convert row-major RGB565 pixel array to square-twiddled Morton order.

    Inverse of untwiddle. Returns bytes of length size*size*2.
    """
    flat = row_major_pixels.reshape(-1, 2)
    lut = build_morton_lut(size)
    twiddled = np.zeros_like(flat)
    twiddled[lut] = flat
    return twiddled.tobytes()


# ---------------------------------------------------------------------------
# PVR parsing / building
# ---------------------------------------------------------------------------

def parse_pvrs(decompressed):
    """Parse concatenated PVR files from decompressed data."""
    pvrs = []
    offset = 0
    while offset + 32 <= len(decompressed):
        if decompressed[offset:offset + 4] != b'GBIX':
            break
        if decompressed[offset + 16:offset + 20] != b'PVRT':
            break
        size_field = struct.unpack('<I', decompressed[offset + 20:offset + 24])[0]
        type_field = decompressed[offset + 24:offset + 28]
        width = struct.unpack('<H', decompressed[offset + 28:offset + 30])[0]
        height = struct.unpack('<H', decompressed[offset + 30:offset + 32])[0]
        pixel_size = width * height * 2
        pixel_data = decompressed[offset + 32:offset + 32 + pixel_size]
        pvrs.append({
            'offset': offset,
            'width': width,
            'height': height,
            'type': type_field,
            'size_field': size_field,
            'pixel_data': pixel_data,
        })
        offset += 32 + pixel_size
    return pvrs


def build_pvr(width, height, pixel_data_twiddled, global_index=0,
              pixel_format=FMT_RGB565):
    """Build a single PVR file (GBIX + PVRT header + pixel data).

    pixel_format is the PVR pixel format byte (type_field[0]):
      FMT_RGB565 (0x01) or FMT_ARGB4444 (0x02).
    Twiddle mode is always SQUARE TWIDDLED (0x01).
    """
    gbix = b'GBIX' + struct.pack('<I', 8) + struct.pack('<Q', global_index)
    size_field = 8 + len(pixel_data_twiddled)
    type_field = bytes([pixel_format, 0x01, 0x00, 0x00])
    header = (b'PVRT' + struct.pack('<I', size_field) + type_field
              + struct.pack('<HH', width, height))
    return gbix + header + pixel_data_twiddled


# ---------------------------------------------------------------------------
# Decode (ZT1 -> PNG)
# ---------------------------------------------------------------------------

def decode_zt1(zt1_path, png_path):
    """Decode a ZT1 file and save as PNG."""
    with open(zt1_path, 'rb') as f:
        data = f.read()

    magic = data[:4]
    if magic not in (b'ZT10', b'ZT11', b'ZT12'):
        raise ValueError(f'Bad magic: {magic!r}, expected b"ZT10"/b"ZT11"/b"ZT12"')

    if magic == b'ZT10':
        # ZT10: 16-byte header, zlib stream starts at offset 16
        payload_offset = 16
    else:
        # ZT11 / ZT12: 16-byte header + metadata table (length at offset 12)
        metadata_len = struct.unpack('<I', data[12:16])[0]
        payload_offset = 16 + metadata_len

    decompressed = zlib.decompress(data[payload_offset:])
    print(f'ZT1 size: {len(data)}, magic={magic.decode("ascii", "replace")}, '
          f'payload@{payload_offset}, decompressed: {len(decompressed)}')

    pvrs = parse_pvrs(decompressed)
    print(f'Found {len(pvrs)} PVR file(s):')
    for i, p in enumerate(pvrs):
        fmt_name = {FMT_RGB565: 'RGB565', FMT_ARGB4444: 'ARGB4444'}.get(
            p['type'][0], f'unknown(0x{p["type"][0]:02x})')
        print(f'  PVR {i}: {p["width"]}x{p["height"]} '
              f'type={p["type"].hex()} ({fmt_name}) '
              f'pixel_data={len(p["pixel_data"])} bytes')

    main = pvrs[0]
    main_w, main_h = main['width'], main['height']
    main_fmt = main['type'][0]

    # Layout: main PVR at top-left; sub PVRs stacked to the right (column)
    # when there are multiple, or below (row) when there is only one.
    # Supports mixed sub-PVR sizes (e.g. ZT12: 4x128x128 + 1x256x256).
    if len(pvrs) > 1:
        if len(pvrs) == 2:
            # Single sub PVR: place below the main image (ZT11 layout)
            full_w = max(main_w, pvrs[1]['width'])
            full_h = main_h + pvrs[1]['height']
        else:
            # Multiple sub PVRs: stack vertically in a right column
            right_col_w = max(p['width'] for p in pvrs[1:])
            right_col_h = sum(p['height'] for p in pvrs[1:])
            full_w = main_w + right_col_w
            full_h = max(main_h, right_col_h)
    else:
        full_w = main_w
        full_h = main_h

    print(f'Combined image: {full_w}x{full_h}')

    output = np.zeros((full_h, full_w, 4), dtype=np.uint8)

    main_pixels = untwiddle(main['pixel_data'], main_w)
    output[:main_h, :main_w] = pvr_to_rgba8888(main_pixels, main_fmt)

    if len(pvrs) == 2:
        # Single sub: place at (0, main_h)
        sub = pvrs[1]
        sw, sh = sub['width'], sub['height']
        sub_fmt = sub['type'][0]
        sub_pixels = untwiddle(sub['pixel_data'], sw)
        sub_rgba = pvr_to_rgba8888(sub_pixels, sub_fmt)
        output[main_h:main_h + sh, :sw] = sub_rgba
    else:
        # Multiple subs: stack vertically in right column starting at x=main_w
        y_cursor = 0
        for i in range(1, len(pvrs)):
            sub = pvrs[i]
            sw, sh = sub['width'], sub['height']
            sub_fmt = sub['type'][0]
            sub_pixels = untwiddle(sub['pixel_data'], sw)
            sub_rgba = pvr_to_rgba8888(sub_pixels, sub_fmt)
            output[y_cursor:y_cursor + sh, main_w:main_w + sw] = sub_rgba
            y_cursor += sh

    # ZT10 640x512 canvas reserves the bottom 32 rows for UI; ZT11 (tachi-e)
    # uses the full main PVR height and should not be cropped.
    if magic == b'ZT10' and full_h > 480:
        output = output[:480, :]

    Image.fromarray(output, 'RGBA').save(png_path)
    print(f'Saved {png_path} ({output.shape[1]}x{output.shape[0]})')


# ---------------------------------------------------------------------------
# Encode (PNG -> ZT1)
# ---------------------------------------------------------------------------

def encode_zt1(png_path, zt1_path):
    """Encode a PNG image to ZT1 format."""
    img = Image.open(png_path).convert('RGBA')
    arr = np.array(img)
    h, w = arr.shape[:2]
    print(f'PNG size: {w}x{h}')

    # Target canvas: 640x512 (PNG is 640x480, pad bottom 32 rows with white)
    full_w, full_h = 640, 512
    padded = np.ones((full_h, full_w, 4), dtype=np.uint8) * 255
    padded[:h, :w] = arr[:min(h, full_h), :min(w, full_w)]

    payload = b''

    # PVR 0: 512x512 main image (top-left)
    main = padded[:512, :512]
    main_rgb565 = rgba8888_to_rgb565(main)
    main_twiddled = twiddle(main_rgb565, 512)
    payload += build_pvr(512, 512, main_twiddled, global_index=0)

    # PVR 1-4: 128x128 tiles stacked vertically in the right column (x=512..639)
    for i in range(4):
        y0 = i * 128
        sub = padded[y0:y0 + 128, 512:640]
        sub_rgb565 = rgba8888_to_rgb565(sub)
        sub_twiddled = twiddle(sub_rgb565, 128)
        payload += build_pvr(128, 128, sub_twiddled, global_index=0)

    compressed = zlib.compress(payload)
    print(f'Payload: {len(payload)} bytes, compressed: {len(compressed)} bytes')

    zt1_header = (b'ZT10'
                  + struct.pack('<I', len(compressed) - 6)
                  + struct.pack('<Q', len(payload)))
    zt1_data = zt1_header + compressed

    with open(zt1_path, 'wb') as f:
        f.write(zt1_data)
    print(f'Saved {zt1_path} ({len(zt1_data)} bytes)')


# ---------------------------------------------------------------------------
# Command-line interface
# ---------------------------------------------------------------------------

def change_ext(path, new_ext):
    """Return path with its extension replaced by new_ext (includes the dot)."""
    root, _ = os.path.splitext(path)
    return root + new_ext


def main(argv):
    if len(argv) != 3:
        print('Usage:')
        print('  python zt1.py d filename.zt1   -> decode to filename.png')
        print('  python zt1.py d filename.png   -> encode to filename.zt1')
        print('Direction is auto-detected from the input file extension.')
        return 1

    in_path = argv[2]
    if not os.path.isfile(in_path):
        print(f'Error: file not found: {in_path}')
        return 1

    ext = os.path.splitext(in_path)[1].lower()

    if ext in ('.zt1', '.zt'):
        out_path = change_ext(in_path, '.png')
        decode_zt1(in_path, out_path)
    elif ext == '.png':
        out_path = change_ext(in_path, '.zt1')
        encode_zt1(in_path, out_path)
    else:
        print(f'Error: unsupported file extension "{ext}". Expected .zt1 or .png')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
