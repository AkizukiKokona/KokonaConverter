"""Build a minimal synthetic PMX 2.0 file + a tiny PNG texture for testing."""
import struct
import os
import zlib

OUT_DIR = "/workspace/_test_data"
os.makedirs(OUT_DIR, exist_ok=True)

def make_png(path, rgba=(255, 0, 0, 255)):
    w, h = 2, 2
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
buf += u8(8)
buf += u8(0)
buf += u8(0)
buf += u8(2)
buf += u8(2)
buf += u8(2)
buf += u8(2)
buf += u8(2)
buf += u8(2)
buf += txt("TestModel")
buf += txt("TestModel")
buf += txt("Test")
buf += txt("Test")

verts = [
    ((-1, 0, -1), (0, 1, 0), (0, 0)),
    (( 1, 0, -1), (0, 1, 0), (1, 0)),
    (( 1, 0,  1), (0, 1, 0), (1, 1)),
    ((-1, 0,  1), (0, 1, 0), (0, 1)),
]
buf += i32(len(verts))
for pos, nrm, uv in verts:
    buf += struct.pack("<3f", *pos)
    buf += struct.pack("<3f", *nrm)
    buf += struct.pack("<2f", *uv)
    buf += u8(1)
    buf += s16(0) + s16(1)
    buf += f32(0.5)
    buf += f32(1.0)

buf += i32(6)
buf += struct.pack("<3H", 0, 1, 2)
buf += struct.pack("<3H", 0, 2, 3)

buf += i32(1)
buf += txt("tex/diffuse.png")

buf += i32(1)
buf += txt("Mat")
buf += txt("Mat")
buf += struct.pack("<4f", 0.8, 0.2, 0.2, 1.0)
buf += struct.pack("<3f", 0.5, 0.5, 0.5)
buf += f32(20.0)
buf += struct.pack("<3f", 0.2, 0.2, 0.2)
buf += u8(0)
buf += struct.pack("<4f", 0, 0, 0, 1.0)
buf += f32(1.0)
buf += s16(0)
buf += s16(-1)
buf += struct.pack("<b", 0)
buf += struct.pack("<b", 1)
buf += struct.pack("<b", 0)
buf += txt("")
buf += i32(6)

buf += i32(2)
buf += txt("Root")
buf += txt("Root")
buf += struct.pack("<3f", 0, 0, 0)
buf += s16(-1)
buf += i32(0)
buf += u16(0x0001)
buf += s16(1)
buf += txt("Child")
buf += txt("Child")
buf += struct.pack("<3f", 0, 1, 0)
buf += s16(0)
buf += i32(0)
buf += u16(0x0000)
buf += struct.pack("<3f", 0, 0, 1)

buf += i32(1)
buf += txt("Morph")
buf += txt("Morph")
buf += struct.pack("<b", 4)
buf += struct.pack("<b", 1)
buf += i32(2)
buf += struct.pack("<H", 0)
buf += struct.pack("<3f", 0, 0.5, 0)
buf += struct.pack("<H", 1)
buf += struct.pack("<3f", 0, 0.5, 0)

buf += i32(0)
buf += i32(0)
buf += i32(0)

PMX_PATH = os.path.join(OUT_DIR, "test.pmx")
with open(PMX_PATH, "wb") as f:
    f.write(buf)
print(f"Wrote PMX: {PMX_PATH} ({len(buf)} bytes)")
