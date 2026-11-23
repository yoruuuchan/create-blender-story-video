#!/usr/bin/env python3
"""Dependency-free PNG integrity, dimensions and color-layout checks."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))


class PngError(ValueError):
    pass


def _row_lengths(width: int, height: int, bits_per_pixel: int, interlace: int) -> list[int]:
    def rows(pass_width: int, pass_height: int) -> list[int]:
        if not pass_width or not pass_height:
            return []
        return [1 + (pass_width * bits_per_pixel + 7) // 8] * pass_height

    if interlace == 0:
        return rows(width, height)
    lengths = []
    for x0, y0, dx, dy in ADAM7:
        pass_width = 0 if width <= x0 else (width - x0 + dx - 1) // dx
        pass_height = 0 if height <= y0 else (height - y0 + dy - 1) // dy
        lengths.extend(rows(pass_width, pass_height))
    return lengths


def validate_png(path: Path, width: int | None = None, height: int | None = None) -> dict:
    try:
        with path.open("rb") as handle:
            if handle.read(8) != PNG_SIGNATURE:
                raise PngError("invalid PNG signature")
            header = None
            palette = None
            compressed = bytearray()
            ended = False
            while True:
                raw_length = handle.read(4)
                if len(raw_length) != 4:
                    raise PngError("truncated chunk header")
                length = struct.unpack(">I", raw_length)[0]
                if length > 256 * 1024 * 1024:
                    raise PngError("unreasonable PNG chunk size")
                chunk_type = handle.read(4)
                data = handle.read(length)
                crc_raw = handle.read(4)
                if len(chunk_type) != 4 or len(data) != length or len(crc_raw) != 4:
                    raise PngError("truncated PNG chunk")
                expected_crc = struct.unpack(">I", crc_raw)[0]
                if zlib.crc32(chunk_type + data) & 0xFFFFFFFF != expected_crc:
                    raise PngError(f"CRC mismatch in {chunk_type.decode('ascii', 'replace')} chunk")
                if header is None and chunk_type != b"IHDR":
                    raise PngError("IHDR must be the first chunk")
                if chunk_type == b"IHDR":
                    if length != 13 or header is not None:
                        raise PngError("invalid IHDR")
                    header = struct.unpack(">IIBBBBB", data)
                elif chunk_type == b"PLTE":
                    if header is None or palette is not None or compressed:
                        raise PngError("PLTE must appear once before IDAT")
                    if length == 0 or length % 3 or length > 768:
                        raise PngError("invalid PLTE length")
                    palette = data
                elif chunk_type == b"IDAT":
                    if ended:
                        raise PngError("IDAT after IEND")
                    compressed.extend(data)
                elif chunk_type == b"IEND":
                    if length != 0:
                        raise PngError("invalid IEND")
                    ended = True
                    if handle.read(1):
                        raise PngError("trailing data after IEND")
                    break
            if header is None or not ended or not compressed:
                raise PngError("incomplete PNG")
    except OSError as exc:
        raise PngError(str(exc)) from exc

    actual_width, actual_height, bit_depth, color_type, compression, filtering, interlace = header
    if actual_width < 1 or actual_height < 1:
        raise PngError("invalid dimensions")
    if width is not None and actual_width != width or height is not None and actual_height != height:
        raise PngError(f"expected {width}x{height}, got {actual_width}x{actual_height}")
    if color_type not in CHANNELS or bit_depth not in {1, 2, 4, 8, 16}:
        raise PngError("unsupported PNG color layout")
    if color_type in {2, 4, 6} and bit_depth not in {8, 16}:
        raise PngError("invalid bit depth for PNG color type")
    if color_type == 3:
        if bit_depth not in {1, 2, 4, 8}:
            raise PngError("indexed PNG bit depth must be 1, 2, 4 or 8")
        if palette is None:
            raise PngError("indexed PNG requires PLTE before IDAT")
        if len(palette) // 3 > 2 ** bit_depth:
            raise PngError("PLTE contains more entries than the bit depth allows")
    if compression != 0 or filtering != 0 or interlace not in {0, 1}:
        raise PngError("unsupported PNG encoding")

    rows = _row_lengths(
        actual_width, actual_height, CHANNELS[color_type] * bit_depth, interlace
    )
    expected_size = sum(rows)
    decompressor = zlib.decompressobj()
    decoded_size = 0
    row_index = 0
    leftover = b""

    def inspect_scanlines(decoded: bytes) -> bytes:
        nonlocal row_index, leftover
        buffer = leftover + decoded
        offset = 0
        while row_index < len(rows) and len(buffer) - offset >= rows[row_index]:
            if buffer[offset] > 4:
                raise PngError(f"invalid PNG filter type {buffer[offset]} on row {row_index}")
            offset += rows[row_index]
            row_index += 1
        leftover = buffer[offset:]
        return leftover

    try:
        for offset in range(0, len(compressed), 1024 * 1024):
            remaining = expected_size - decoded_size + 1
            decoded = decompressor.decompress(compressed[offset:offset + 1024 * 1024], remaining)
            decoded_size += len(decoded)
            if decoded_size > expected_size:
                raise PngError("decompressed PNG data exceeds expected scanline size")
            inspect_scanlines(decoded)
        tail = decompressor.flush()
        decoded_size += len(tail)
        inspect_scanlines(tail)
    except zlib.error as exc:
        raise PngError(f"invalid PNG compressed data: {exc}") from exc
    if not decompressor.eof or decompressor.unused_data:
        raise PngError("incomplete or multiple PNG compressed streams")
    if decoded_size != expected_size or row_index != len(rows) or leftover:
        raise PngError(f"expected {expected_size} decoded bytes, got {decoded_size}")
    return {
        "width": actual_width,
        "height": actual_height,
        "bit_depth": bit_depth,
        "color_type": color_type,
        "interlace": interlace,
    }
