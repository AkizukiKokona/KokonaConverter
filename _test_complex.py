"""Build a complex synthetic PMX 2.0 file mimicking a real character model."""
import struct
import os
import zlib

OUT_DIR = "/workspace/_test_data"
os.makedirs(OUT_DIR, exist_ok=True)

def make_png(path, w=4, h=4, rgba=(255, 0, 0, 255)):
    raw = b""
    for _ in range(h):
        raw += b"\x00"
        raw += bytes(rgba) * w
    def chunk(typ, data):
        c = typ + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    idat = zlib.compress(raw, 9)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", ihdr))
        f.write(chunk(b"IDAT", idat))
        f.write(chunk(b"IEND", b""))

TEX_PATH = os.path.join(OUT_DIR, "tex", "diffuse.png")
os.makedirs(os.path.dirname(TEX_PATH), exist_ok=True)
make_png(TEX_PATH)

TEX_PATH2 = os.path.join(OUT_DIR, "tex", "face.png")
make_png(TEX_PATH2, w=2, h=2, rgba=(0, 255, 0, 255))

def s16(x): return struct.pack("<h", x)
def u8(x): return struct.pack("<B", x)
def u16(x): return struct.pack("<H", x)
def i32(x): return struct.pack("<i", x)
def f32(x): return struct.pack("<f", x)
def txt(s, enc="utf-16-le"):
    b = s.encode(enc)
    return i32(len(b)) + b

buf = b""
buf += b"PMX "
buf += f32(2.0)
buf += u8(8)   # globals_count = 8
buf += u8(0)   # encoding = 0 (UTF-8)
buf += u8(0)   # additional_uv = 0
buf += u8(2)   # vertex index size: 2 bytes
buf += u8(2)   # texture index size: 2 bytes
buf += u8(2)   # material index size: 2 bytes
buf += u8(2)   # bone index size: 2 bytes
buf += u8(2)   # morph index size: 2 bytes
buf += u8(2)   # rigidbody index size: 2 bytes

# Model name (Japanese + English)
model_name_jp = "ここの"
model_name_en = "Kokona"
buf += txt(model_name_jp)
buf += txt(model_name_en)
buf += txt("comment jp")
buf += txt("comment en")

# Vertices: 8 verts (a cube)
verts = [
    ((-1, 0, -1), (0, 1, 0), (0, 0)),
    (( 1, 0, -1), (0, 1, 0), (1, 0)),
    (( 1, 0,  1), (0, 1, 0), (1, 1)),
    ((-1, 0,  1), (0, 1, 0), (0, 1)),
    ((-1, 2, -1), (0, 1, 0), (0, 0)),
    (( 1, 2, -1), (0, 1, 0), (1, 0)),
    (( 1, 2,  1), (0, 1, 0), (1, 1)),
    ((-1, 2,  1), (0, 1, 0), (0, 1)),
]
buf += i32(len(verts))
for pos, nrm, uv in verts:
    buf += struct.pack("<3f", *pos)
    buf += struct.pack("<3f", *nrm)
    buf += struct.pack("<2f", *uv)
    buf += u8(1)  # BDEF2
    buf += s16(0) + s16(1)  # 2 bone indices (2 bytes each)
    buf += f32(0.5)  # weight for bone 0 (bone 1 gets 1.0 - 0.5 = 0.5)
    buf += f32(1.0)  # edge scale

# Faces: 12 triangles (cube)
faces = [
    (0,1,2), (0,2,3),  # bottom
    (4,6,5), (4,7,6),  # top
    (0,4,5), (0,5,1),  # front
    (1,5,6), (1,6,2),  # right
    (2,6,7), (2,7,3),  # back
    (3,7,4), (3,4,0),  # left
]
buf += i32(len(faces) * 3)
for (a, b, c) in faces:
    buf += struct.pack("<3H", a, b, c)

# Textures
buf += i32(2)
buf += txt("tex/diffuse.png")
buf += txt("tex/face.png")

# Materials
buf += i32(2)
for i, mname in enumerate(["Body", "Face"]):
    buf += txt(mname)
    buf += txt(mname)
    buf += struct.pack("<4f", 0.8, 0.2, 0.2, 1.0)  # diffuse
    buf += struct.pack("<3f", 0.5, 0.5, 0.5)  # specular
    buf += f32(20.0)  # shininess
    buf += struct.pack("<3f", 0.2, 0.2, 0.2)  # ambient
    buf += u8(0)  # draw flags
    buf += struct.pack("<4f", 0, 0, 0, 1.0)  # edge color
    buf += f32(1.0)  # edge size
    buf += s16(i)  # texture index (2 bytes)
    buf += s16(-1)  # sphere index (2 bytes)
    buf += struct.pack("<b", 0)  # sphere_mode
    buf += struct.pack("<b", 0)  # toon_shared = False
    buf += s16(-1)  # toon_index (2 bytes, since toon_shared=False)
    buf += txt("")  # comment
    buf += i32(6)  # face count for this material (6 triangles each)

# Bones: 5 bones (root + 4 chain)
bones = [
    ("全ての親", "Root", (0, 0, 0), -1),       # root
    ("腰", "Waist", (0, 1, 0), 0),              # child of root
    ("胸", "Chest", (0, 1.5, 0), 1),            # child of waist
    ("頭", "Head", (0, 2, 0), 2),               # child of chest
    ("腕", "Arm", (1, 1.5, 0), 1),              # child of waist
]
buf += i32(len(bones))
for name_jp, name_en, pos, parent in bones:
    buf += txt(name_jp)
    buf += txt(name_en)
    buf += struct.pack("<3f", *pos)
    buf += s16(parent if parent >= 0 else -1)
    buf += i32(0)  # transform class
    buf += u16(0x0000)  # bone flags (no IK, no additional rotation/translation)
    # If has additional transform... (flag bit 0x0020 not set, so skip)
    # If IK... (flag bit 0x0020 not set, so skip)
    buf += struct.pack("<3f", 0, 0, 0)  # end edge position (not used since flag not set)

# Morphs: 1 vertex morph
buf += i32(1)
buf += txt("微笑み")
buf += txt("Smile")
buf += struct.pack("<b", 1)  # vertex morph
buf += struct.pack("<b", 1)  # morph type
buf += i32(1)  # 1 offset
buf += struct.pack("<H", 0)  # vertex index 0
buf += struct.pack("<3f", 0, 0.1, 0)  # delta

# Display frames
buf += i32(0)
# Rigid bodies
buf += i32(0)
# Joints
buf += i32(0)

PMX_PATH = os.path.join(OUT_DIR, "test_complex.pmx")
with open(PMX_PATH, "wb") as f:
    f.write(buf)
print(f"Wrote PMX: {PMX_PATH} ({len(buf)} bytes)")
