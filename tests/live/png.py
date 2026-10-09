"""Minimal PNG reader for chart screenshots: 8-bit RGB or RGBA, no interlace.

The live suite counts label-coloured pixels to tell which set file an expert
is running with, and the test image ships without an imaging library.
"""
from __future__ import annotations

import struct
import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_CHANNELS_BY_COLOR_TYPE = {2: 3, 6: 4}


def decode(data: bytes) -> tuple[int, int, list[tuple[int, int, int]]]:
    """Return (width, height, pixels) with pixels as RGB tuples, row by row."""
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("not a PNG")
    pos, idat, header = len(PNG_SIGNATURE), b"", None
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", chunk)
        elif kind == b"IDAT":
            idat += chunk
        pos += 12 + length
    if header is None:
        raise ValueError("PNG without IHDR")
    width, height, depth, color_type, _, _, interlace = header
    channels = _CHANNELS_BY_COLOR_TYPE.get(color_type)
    if depth != 8 or channels is None or interlace:
        raise ValueError(f"unsupported PNG: depth={depth} color_type={color_type}")
    raw = zlib.decompress(idat)
    stride = width * channels
    rows: list[bytearray] = []
    prev = bytearray(stride)
    for y in range(height):
        start = y * (stride + 1)
        kind = raw[start]
        row = bytearray(raw[start + 1:start + 1 + stride])
        _unfilter(kind, row, prev, channels)
        rows.append(row)
        prev = row
    pixels = [
        (row[x], row[x + 1], row[x + 2])
        for row in rows
        for x in range(0, stride, channels)
    ]
    return width, height, pixels


def _unfilter(kind: int, row: bytearray, prev: bytearray, bpp: int) -> None:
    for i in range(len(row)):
        left = row[i - bpp] if i >= bpp else 0
        up = prev[i]
        up_left = prev[i - bpp] if i >= bpp else 0
        if kind == 1:
            row[i] = (row[i] + left) & 0xFF
        elif kind == 2:
            row[i] = (row[i] + up) & 0xFF
        elif kind == 3:
            row[i] = (row[i] + (left + up) // 2) & 0xFF
        elif kind == 4:
            row[i] = (row[i] + _paeth(left, up, up_left)) & 0xFF


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def count_color(pixels: list[tuple[int, int, int]], rgb: tuple[int, int, int]) -> int:
    return sum(1 for pixel in pixels if pixel == rgb)
