"""
ZT1 <-> PNG converter (compressed PVR images).

Suport Comic Party,Dreamcast,2001,ZT1 file

ZT1 file layout:
  - 16-byte header:
      bytes 0-3:   "ZT10" magic
      bytes 4-7:   u32 LE = deflate stream length (compressed_len - 6)
      bytes 8-15:  u64 LE = decompressed payload length
  - zlib-compressed payload containing concatenated PVR files

Each PVR file:
  - 16-byte GBIX section: "GBIX" + u32(8) + u64(global_index)
  - 16-byte PVRT header: "PVRT" + u32(size_field) + 4-byte type + u16(width) + u16(height)
      size_field = 8 + pixel_data_length
  - Pixel data (width*height*2 bytes, RGB565 square-twiddled Morton order)

Type byte 1 = 0x01 (RGB565), Type byte 2 = 0x01 (SQUARE TWIDDLED).

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


def rgb565_to_rgb888(pixel_arr):
    """Convert RGB565 (LE 2-byte) pixel array to RGB888."""
    vals = (pixel_arr[:, :, 1].astype(np.uint32) << 8) | pixel_arr[:, :, 0].astype(np.uint32)
    r5 = ((vals >> 11) & 0x1F) * 255 // 31
    g6 = ((vals >> 5) & 0x3F) * 255 // 63
    b5 = (vals & 0x1F) * 255 // 31
    return np.stack([r5.astype(np.uint8), g6.astype(np.uint8), b5.astype(np.uint8)], axis=-1)


def rgb888_to_rgb565(rgb):
    """Convert RGB888 array (H, W, 3) to RGB565 little-endian bytes (H, W, 2)."""
    r = rgb[:, :, 0].astype(np.uint32)
    g = rgb[:, :, 1].astype(np.uint32)
    b = rgb[:, :, 2].astype(np.uint32)
    r5 = (r * 31 // 255) & 0x1F
    g6 = (g * 63 // 255) & 0x3F
    b5 = (b * 31 // 255) & 0x1F
    val = (r5 << 11) | (g6 << 5) | b5
    low = (val & 0xFF).astype(np.uint8)
    high = ((val >> 8) & 0xFF).astype(np.uint8)
    return np.stack([low, high], axis=-1)


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


def build_pvr(width, height, pixel_data_twiddled, global_index=0):
    """Build a single PVR file (GBIX + PVRT header + pixel data)."""
    gbix = b'GBIX' + struct.pack('<I', 8) + struct.pack('<Q', global_index)
    size_field = 8 + len(pixel_data_twiddled)
    type_field = bytes([0x01, 0x01, 0x00, 0x00])  # RGB565 + SQUARE TWIDDLED
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
    if magic != b'ZT10':
        raise ValueError(f'Bad magic: {magic!r}, expected b"ZT10"')

    decompressed = zlib.decompress(data[16:])
    print(f'ZT1 size: {len(data)}, decompressed: {len(decompressed)}')

    pvrs = parse_pvrs(decompressed)
    print(f'Found {len(pvrs)} PVR file(s):')
    for i, p in enumerate(pvrs):
        print(f'  PVR {i}: {p["width"]}x{p["height"]} '
              f'type={p["type"].hex()} pixel_data={len(p["pixel_data"])} bytes')

    main = pvrs[0]
    main_w, main_h = main['width'], main['height']

    if len(pvrs) > 1:
        sub_w = pvrs[1]['width']
        sub_h = pvrs[1]['height']
        sub_count = len(pvrs) - 1
        if main_h == sub_h * sub_count or sub_count * sub_h >= main_h:
            full_w = main_w + sub_w
            full_h = main_h
        else:
            full_w = main_w
            full_h = main_h + sub_h
    else:
        full_w = main_w
        full_h = main_h

    print(f'Combined image: {full_w}x{full_h}')

    output = np.zeros((full_h, full_w, 3), dtype=np.uint8)

    main_pixels = untwiddle(main['pixel_data'], main_w)
    output[:main_h, :main_w] = rgb565_to_rgb888(main_pixels)

    for i in range(1, len(pvrs)):
        sub = pvrs[i]
        sw, sh = sub['width'], sub['height']
        sub_pixels = untwiddle(sub['pixel_data'], sw)
        sub_rgb = rgb565_to_rgb888(sub_pixels)

        if full_w > main_w:
            y0 = (i - 1) * sh
            x0 = main_w
        else:
            y0 = main_h
            x0 = (i - 1) * sw
        output[y0:y0 + sh, x0:x0 + sw] = sub_rgb

    if full_h > 480:
        output = output[:480, :]

    Image.fromarray(output, 'RGB').save(png_path)
    print(f'Saved {png_path} ({output.shape[1]}x{output.shape[0]})')


# ---------------------------------------------------------------------------
# Encode (PNG -> ZT1)
# ---------------------------------------------------------------------------

def encode_zt1(png_path, zt1_path):
    """Encode a PNG image to ZT1 format."""
    img = Image.open(png_path).convert('RGB')
    arr = np.array(img)
    h, w = arr.shape[:2]
    print(f'PNG size: {w}x{h}')

    # Target canvas: 640x512 (PNG is 640x480, pad bottom 32 rows with white)
    full_w, full_h = 640, 512
    padded = np.ones((full_h, full_w, 3), dtype=np.uint8) * 255
    padded[:h, :w] = arr[:min(h, full_h), :min(w, full_w)]

    payload = b''

    # PVR 0: 512x512 main image (top-left)
    main = padded[:512, :512]
    main_rgb565 = rgb888_to_rgb565(main)
    main_twiddled = twiddle(main_rgb565, 512)
    payload += build_pvr(512, 512, main_twiddled, global_index=0)

    # PVR 1-4: 128x128 tiles stacked vertically in the right column (x=512..639)
    for i in range(4):
        y0 = i * 128
        sub = padded[y0:y0 + 128, 512:640]
        sub_rgb565 = rgb888_to_rgb565(sub)
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
