"""Generate a more complex PMX 2.0 file for testing.

Includes:
  - 6 vertices (a small box-ish shape)
  - 3 bones in a hierarchy: root -> child -> grandchild
  - BDEF4 weights on some vertices
  - 1 IK bone with 2 IK links
  - 1 bone with a fixed axis
  - 2 materials with different face counts
  - 1 vertex morph
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
    # We have < 256 of everything, so 1-byte indices work.
    vsize = tsize = msize = bsize = psize = rsize = 1

    out = bytearray()
    out += b"PMX "
    out += struct.pack("<f", 2.0)
    out += struct.pack("<B", 8)
    out += struct.pack("<B", 0)  # UTF-16LE
    out += struct.pack("<B", 0)  # additional UV
    out += struct.pack("<B", vsize)
    out += struct.pack("<B", tsize)
    out += struct.pack("<B", msize)
    out += struct.pack("<B", bsize)
    out += struct.pack("<B", psize)
    out += struct.pack("<B", rsize)

    out += _text("复杂测试", encoding)
    out += _text("ComplexTest", encoding)
    out += _text("", encoding)
    out += _text("", encoding)

    # --- Vertices (6) ---
    # A small shape with various weights
    verts_data = [
        # pos, normal, uv, weight_type, bones, weights
        ((0, 0, 0), (0, 1, 0), (0, 0), 0, [0], []),  # BDEF1 bone 0
        ((1, 0, 0), (0, 1, 0), (1, 0), 0, [0], []),  # BDEF1 bone 0
        ((1, 1, 0), (0, 1, 0), (1, 1), 1, [0, 1], [0.5]),  # BDEF2 bones 0,1
        ((0, 1, 0), (0, 1, 0), (0, 1), 1, [0, 1], [0.5]),  # BDEF2 bones 0,1
        ((2, 0, 0), (0, 1, 0), (0, 0), 2, [0, 1, 2, 0], [0.25, 0.25, 0.25, 0.25]),  # BDEF4
        ((2, 1, 0), (0, 1, 0), (0, 0), 2, [0, 1, 2, 1], [0.4, 0.3, 0.2, 0.1]),  # BDEF4
    ]
    out += struct.pack("<i", len(verts_data))
    for pos, nrm, uv, wt, bones, weights in verts_data:
        out += struct.pack("<3f", *pos)
        out += struct.pack("<3f", *nrm)
        out += struct.pack("<2f", *uv)
        out += struct.pack("<B", wt)
        if wt == 0:
            out += struct.pack("<b", bones[0])
        elif wt == 1:
            out += struct.pack("<2b", *bones)
            out += struct.pack("<f", weights[0])
        elif wt == 2:
            out += struct.pack("<4b", *bones)
            out += struct.pack("<4f", *weights)
        out += struct.pack("<f", 1.0)  # edge scale

    # --- Faces (4 triangles = 12 indices) ---
    # 2 materials, each with 2 triangles (6 indices)
    faces = [(0, 1, 2), (0, 2, 3), (1, 4, 5), (1, 5, 2)]
    out += struct.pack("<i", len(faces) * 3)
    for f in faces:
        out += struct.pack("<3B", *f)

    # --- Textures (2) ---
    out += struct.pack("<i", 2)
    out += _text("tex/diffuse.png", encoding)
    out += _text("tex/toon.png", encoding)

    # --- Materials (2) ---
    out += struct.pack("<i", 2)
    for i in range(2):
        out += _text(f"材质{i}", encoding)
        out += _text(f"Mat{i}", encoding)
        out += struct.pack("<4f", 0.8, 0.8, 0.8, 1.0)  # diffuse
        out += struct.pack("<3f", 0.2, 0.2, 0.2)  # specular
        out += struct.pack("<f", 20.0)  # spec strength
        out += struct.pack("<3f", 0.2, 0.2, 0.2)  # ambient
        out += struct.pack("<B", 0x11)  # flags
        out += struct.pack("<4f", 0.0, 0.0, 0.0, 1.0)  # edge color
        out += struct.pack("<f", 1.0)  # edge scale
        out += struct.pack("<b", i)  # texture index
        out += struct.pack("<b", -1)  # sphere none
        out += struct.pack("<b", 0)  # sphere mode
        out += struct.pack("<b", 1)  # toon shared
        out += struct.pack("<b", i)  # toon index
        out += _text("", encoding)  # comment
        out += struct.pack("<i", 6)  # 2 triangles = 6 indices

    # --- Bones (4) ---
    # 0: root at (0,0,0), tail offset (1,0,0)
    # 1: child of 0 at (1,0,0), tail offset (2,0,0)
    # 2: child of 1 at (2,0,0), tail bone (3) [tail_is_bone]
    # 3: IK bone, parent 2, at (3,0,0), with IK target bone 2, 2 links
    # 4: bone with fixed axis
    out += struct.pack("<i", 5)

    # bone 0: root, simple
    out += _text("根", encoding); out += _text("Root", encoding)
    out += struct.pack("<3f", 0, 0, 0)
    out += struct.pack("<b", -1); out += struct.pack("<i", 1)
    out += struct.pack("<H", 0x001E)  # rotatable/movable/visible/enabled
    out += struct.pack("<3f", 1, 0, 0)  # tail offset

    # bone 1: child of 0
    out += _text("子", encoding); out += _text("Child", encoding)
    out += struct.pack("<3f", 1, 0, 0)
    out += struct.pack("<b", 0); out += struct.pack("<i", 1)
    out += struct.pack("<H", 0x001E)
    out += struct.pack("<3f", 1, 0, 0)

    # bone 2: child of 1, tail is bone (use bone 3)
    out += _text("孙", encoding); out += _text("Grand", encoding)
    out += struct.pack("<3f", 2, 0, 0)
    out += struct.pack("<b", 1); out += struct.pack("<i", 1)
    out += struct.pack("<H", 0x001F)  # +indexed tail
    out += struct.pack("<b", 3)  # tail bone = 3

    # bone 3: IK bone, parent 2, with IK target=2, 2 links
    out += _text("IK", encoding); out += _text("IKBone", encoding)
    out += struct.pack("<3f", 3, 0, 0)
    out += struct.pack("<b", 2); out += struct.pack("<i", 1)
    out += struct.pack("<H", 0x001E | 0x0020)  # + IK
    out += struct.pack("<3f", 1, 0, 0)  # tail offset
    # IK data
    out += struct.pack("<b", 2)  # IK target bone = 2
    out += struct.pack("<i", 10)  # loop count
    out += struct.pack("<f", 0.05)  # limit radian
    out += struct.pack("<i", 2)  # 2 links
    # link 0: bone 1, no limits
    out += struct.pack("<b", 1); out += struct.pack("<B", 0)
    # link 1: bone 2, with limits
    out += struct.pack("<b", 2); out += struct.pack("<B", 1)
    out += struct.pack("<3f", -1.0, -1.0, -1.0)  # min
    out += struct.pack("<3f", 1.0, 1.0, 1.0)  # max

    # bone 4: bone with fixed axis
    out += _text("轴", encoding); out += _text("Axis", encoding)
    out += struct.pack("<3f", 4, 0, 0)
    out += struct.pack("<b", 0); out += struct.pack("<i", 1)
    out += struct.pack("<H", 0x001E | 0x0400)  # + fixed axis
    out += struct.pack("<3f", 0, 1, 0)  # tail offset
    out += struct.pack("<3f", 0, 0, 1)  # fixed axis direction

    # --- Morphs (1 vertex morph) ---
    out += struct.pack("<i", 1)
    out += _text("变形", encoding); out += _text("MyMorph", encoding)
    out += struct.pack("<b", 3)  # panel Other
    out += struct.pack("<b", 1)  # type vertex
    out += struct.pack("<i", 2)  # 2 offsets
    out += struct.pack("<B", 0); out += struct.pack("<3f", 0, 1, 0)
    out += struct.pack("<B", 5); out += struct.pack("<3f", 0, 1, 0)

    # --- Display frames ---
    out += struct.pack("<i", 1)
    out += _text("Root", encoding); out += _text("Root", encoding)
    out += struct.pack("<B", 1)  # special
    out += struct.pack("<i", 3)  # 3 entries
    for bone_idx in (0, 1, 2):
        out += struct.pack("<B", 0)  # bone
        out += struct.pack("<b", bone_idx)

    # --- Rigid bodies (0) ---
    out += struct.pack("<i", 0)
    # --- Joints (0) ---
    out += struct.pack("<i", 0)

    with open(out_path, "wb") as fh:
        fh.write(bytes(out))
    print(f"wrote {len(out)} bytes to {out_path}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: test_pmx_complex.py out.pmx")
        sys.exit(1)
    main(sys.argv[1])
