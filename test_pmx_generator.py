"""Generate a small synthetic PMX 2.0 file for end-to-end testing.

The generated file has:
  - 4 vertices forming a quad (2 triangles)
  - 1 bone at the origin (BDEF1 weights)
  - 1 material with one texture reference
  - 1 vertex morph
  - 1 display frame
  - 0 rigid bodies / joints

Run with: python3 test_pmx_generator.py out.pmx
"""

from __future__ import annotations

import struct
import sys
from typing import List


def _text(s: str, encoding: str) -> bytes:
    raw = s.encode(encoding)
    return struct.pack("<i", len(raw)) + raw


def main(out_path: str) -> None:
    encoding = "utf-16-le"
    additional_uv = 0
    vsize = 1  # vertex index: 1 byte (we have only 4 vertices)
    tsize = 1  # texture index: 1 byte
    msize = 1  # material index: 1 byte
    bsize = 1  # bone index: 1 byte
    psize = 1  # morph index: 1 byte
    rsize = 1  # rigid body index: 1 byte

    out = bytearray()
    out += b"PMX "
    out += struct.pack("<f", 2.0)
    out += struct.pack("<B", 8)  # globals count
    out += struct.pack("<B", 0)  # UTF-16LE
    out += struct.pack("<B", additional_uv)
    out += struct.pack("<B", vsize)
    out += struct.pack("<B", tsize)
    out += struct.pack("<B", msize)
    out += struct.pack("<B", bsize)
    out += struct.pack("<B", psize)
    out += struct.pack("<B", rsize)

    out += _text("测试模型", encoding)  # name jp (Chinese)
    out += _text("TestModel", encoding)  # name en
    out += _text("测试注释", encoding)  # comment jp
    out += _text("test comment", encoding)  # comment en

    # --- Vertices ---
    # 4 vertices of a quad in the XY plane (PMX: Y up, Z forward)
    # Make it 10x10 units (will become 80x80 cm at scale=8)
    verts = [
        (-5.0, -5.0, 0.0),
        (5.0, -5.0, 0.0),
        (5.0, 5.0, 0.0),
        (-5.0, 5.0, 0.0),
    ]
    out += struct.pack("<i", len(verts))
    for v in verts:
        out += struct.pack("<3f", *v)  # position
        out += struct.pack("<3f", 0.0, 0.0, 1.0)  # normal (toward +Z = forward in PMX)
        out += struct.pack("<2f", 0.0, 0.0)  # UV
        # additional UVs: 0
        out += struct.pack("<B", 0)  # BDEF1
        out += struct.pack("<b", 0)  # bone 0
        out += struct.pack("<f", 1.0)  # edge scale

    # --- Faces ---
    # 2 triangles: (0,1,2) and (0,2,3) -- CW winding in PMX
    out += struct.pack("<i", 6)  # 6 indices = 2 triangles
    for idx in (0, 1, 2, 0, 2, 3):
        out += struct.pack("<B", idx)  # 1-byte unsigned vertex index

    # --- Textures ---
    out += struct.pack("<i", 1)
    out += _text("tex/diffuse.png", encoding)

    # --- Materials ---
    out += struct.pack("<i", 1)  # 1 material
    out += _text("材质", encoding)  # name jp
    out += _text("Mat", encoding)  # name en
    out += struct.pack("<4f", 0.8, 0.8, 0.8, 1.0)  # diffuse RGBA
    out += struct.pack("<3f", 0.2, 0.2, 0.2)  # specular RGB
    out += struct.pack("<f", 20.0)  # specular strength
    out += struct.pack("<3f", 0.2, 0.2, 0.2)  # ambient
    out += struct.pack("<B", 0x01 | 0x10)  # flags: no-cull + has edge
    out += struct.pack("<4f", 0.0, 0.0, 0.0, 1.0)  # edge color
    out += struct.pack("<f", 1.0)  # edge scale
    out += struct.pack("<b", 0)  # texture index 0
    out += struct.pack("<b", -1)  # sphere index none
    out += struct.pack("<b", 0)  # sphere mode off
    out += struct.pack("<b", 1)  # toon shared
    out += struct.pack("<b", 0)  # toon index 0 (toon01.bmp)
    out += _text("material comment", encoding)
    out += struct.pack("<i", 6)  # face_count (6 indices = 2 triangles)

    # --- Bones ---
    # 1 bone at origin, with vec3 tail
    out += struct.pack("<i", 1)
    out += _text("骨", encoding)  # name jp
    out += _text("Bone", encoding)  # name en
    out += struct.pack("<3f", 0.0, 0.0, 0.0)  # position
    out += struct.pack("<b", -1)  # parent bone none
    out += struct.pack("<i", 1)  # layer
    out += struct.pack("<H", 0x0002 | 0x0004 | 0x0008 | 0x0010)  # rotatable, movable, visible, enabled
    # tail is vec3 (bit 0x0001 not set)
    out += struct.pack("<3f", 0.0, 1.0, 0.0)  # tail offset (1 unit up)

    # --- Morphs ---
    # 1 vertex morph with 2 offsets
    out += struct.pack("<i", 1)
    out += _text("变形", encoding)  # name jp
    out += _text("Morph", encoding)  # name en
    out += struct.pack("<b", 3)  # panel: Other
    out += struct.pack("<b", 1)  # type: vertex
    out += struct.pack("<i", 2)  # offset count
    # offset 1
    out += struct.pack("<B", 0)  # vertex index 0
    out += struct.pack("<3f", 0.0, 0.5, 0.0)  # translation
    # offset 2
    out += struct.pack("<B", 3)  # vertex index 3
    out += struct.pack("<3f", 0.0, 0.5, 0.0)  # translation

    # --- Display frames ---
    # 2 frames: Root (special, bones) and Expressions (special, morphs)
    out += struct.pack("<i", 2)
    # frame 0: Root
    out += _text("Root", encoding)
    out += _text("Root", encoding)
    out += struct.pack("<B", 1)  # special
    out += struct.pack("<i", 1)  # 1 entry
    out += struct.pack("<B", 0)  # type bone
    out += struct.pack("<b", 0)  # bone index 0
    # frame 1: Expressions
    out += _text("表情", encoding)
    out += _text("Expr", encoding)
    out += struct.pack("<B", 1)  # special
    out += struct.pack("<i", 1)  # 1 entry
    out += struct.pack("<B", 1)  # type morph
    out += struct.pack("<b", 0)  # morph index 0

    # --- Rigid bodies ---
    out += struct.pack("<i", 0)

    # --- Joints ---
    out += struct.pack("<i", 0)

    with open(out_path, "wb") as fh:
        fh.write(bytes(out))
    print(f"wrote {len(out)} bytes to {out_path}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: test_pmx_generator.py out.pmx")
        sys.exit(1)
    main(sys.argv[1])
