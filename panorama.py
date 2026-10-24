# SPDX-License-Identifier: MIT
#
# Minimal Source 2 Panorama "compiler": wraps plain-text CSS / XML / JS in a Source 2 resource
# container that the engine loads directly. No resourcecompiler.exe / CSDK required.
#
# Adapted from build.py in https://github.com/Vergir/deadlock-hud-resize
# (commit 88c7c168064a816859d7b259039066a634dda95d). Original notice:
#
#   MIT License
#
#   Copyright (c) 2026 vergir
#
#   Permission is hereby granted, free of charge, to any person obtaining a copy
#   of this software and associated documentation files (the "Software"), to deal
#   in the Software without restriction, including without limitation the rights
#   to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
#   copies of the Software, and to permit persons to whom the Software is
#   furnished to do so, subject to the following conditions:
#
#   The above copyright notice and this permission notice shall be included in all
#   copies or substantial portions of the Software.
#
#   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#   IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#   FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
#   AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#   LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
#   OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
#   SOFTWARE.
#
# Resource layout (as read by ValveResourceFormat). Valve's own compiler emits RERL/RED2 blocks and,
# for layouts, a KV3 "LaCo" block; the engine also accepts this legacy plain-text form:
#   header: u32 file_size, u16 header_version (12), u16 resource_version,
#           u32 block_table_offset (relative to this field), u32 block_count
#   table:  per block: 4-char type, u32 data_offset (relative to this field), u32 size
#   .vcss_c (v3): DATA = u32 crc32(text), u16 0, text
#   .vxml_c (v3): DATA = u32 crc32(text), u16 0, text
#   .vts_c  (v2): DATA = raw script text
import struct
import zlib
from pathlib import PurePosixPath


def pack_resource(version: int, blocks: list[tuple[bytes, bytes]]) -> bytes:
    """blocks: list of (4-byte type, payload). Payloads are 16-byte aligned like Valve's files."""
    header = struct.pack("<IHHII", 0, 12, version, 8, len(blocks))
    pos = len(header) + 12 * len(blocks)
    offsets = []
    body = bytearray()
    for _, payload in blocks:
        pad = (-pos) % 16
        body += b"\0" * pad
        pos += pad
        offsets.append(pos)
        body += payload
        pos += len(payload)
    table = bytearray()
    for i, (btype, payload) in enumerate(blocks):
        field_pos = len(header) + 12 * i + 4
        table += btype + struct.pack("<II", offsets[i] - field_pos, len(payload))
    out = bytearray(header) + table + body
    struct.pack_into("<I", out, 0, len(out))
    return bytes(out)


def _text_data_block(text: bytes) -> bytes:
    return struct.pack("<IH", zlib.crc32(text) & 0xFFFFFFFF, 0) + text


def compile_text(rel: str, text: str) -> tuple[str, bytes]:
    """Compile one source file. rel is its game path, e.g. "panorama/styles/foo.css".
    Returns (compiled game path, compiled bytes)."""
    path = PurePosixPath(rel)
    raw = text.encode("utf-8")
    match path.suffix.lower():
        case ".css":
            return str(path.with_suffix(".vcss_c")), pack_resource(3, [(b"DATA", _text_data_block(raw))])
        case ".xml":
            return str(path.with_suffix(".vxml_c")), pack_resource(3, [(b"DATA", _text_data_block(raw))])
        case ".vts":
            return rel + "_c", pack_resource(2, [(b"DATA", raw)])
    raise ValueError(f"don't know how to compile {rel}")
