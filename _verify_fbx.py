"""Verify a binary FBX file by parsing it node-by-node."""
import struct
import sys
import zlib

def parse_fbx(path):
    with open(path, "rb") as f:
        data = f.read()

    # Header: 23-byte magic + 4-byte version = 27 bytes
    magic = data[:23]
    if magic != b"Kaydara FBX Binary  \x00\x1a\x00":
        print(f"BAD MAGIC: {magic!r}")
        return False
    version = struct.unpack("<I", data[23:27])[0]
    print(f"Header OK: version={version}, file_size={len(data)}")

    pos = 27
    errors = 0
    node_count = 0

    def parse_node(data, pos, depth, path_prefix=""):
        nonlocal errors, node_count
        if pos + 13 > len(data):
            print(f"{'  '*depth}ERROR: not enough bytes for node header at {pos}")
            errors += 1
            return pos

        end_offset = struct.unpack("<I", data[pos:pos+4])[0]
        num_props = struct.unpack("<I", data[pos+4:pos+8])[0]
        prop_list_len = struct.unpack("<I", data[pos+8:pos+12])[0]
        name_len = data[pos+12]

        # NULL record check
        if end_offset == 0 and num_props == 0 and prop_list_len == 0 and name_len == 0:
            return None  # signals NULL terminator

        name = data[pos+13:pos+13+name_len].decode("utf-8", errors="replace")
        props_start = pos + 13 + name_len
        props_end = props_start + prop_list_len

        node_count += 1
        indent = "  " * depth
        print(f"{indent}[{pos}] Node '{name}' endoff={end_offset} nprops={num_props} proplen={prop_list_len}")

        # Verify end_offset is within file
        if end_offset > len(data):
            print(f"{indent}  ERROR: end_offset {end_offset} > file_size {len(data)}")
            errors += 1
            return end_offset

        # Parse properties (just count, don't fully decode)
        ppos = props_start
        for pi in range(num_props):
            if ppos >= len(data):
                print(f"{indent}  ERROR: property {pi} starts beyond file")
                errors += 1
                break
            tc = chr(data[ppos])
            psize = property_size(data, ppos)
            if psize is None:
                print(f"{indent}  ERROR: cannot determine size of property {pi} type '{tc}' at {ppos}")
                errors += 1
                break
            ppos += psize

        if ppos != props_end:
            print(f"{indent}  ERROR: props end at {ppos} but expected {props_end}")
            errors += 1

        # Parse children
        child_pos = props_end
        while child_pos < end_offset:
            if child_pos + 13 > len(data):
                print(f"{indent}  ERROR: child header beyond file at {child_pos}")
                errors += 1
                break
            result = parse_node(data, child_pos, depth + 1)
            if result is None:
                # NULL terminator
                child_pos += 13
                break
            child_pos = result

        if child_pos != end_offset:
            print(f"{indent}  ERROR: children end at {child_pos} but end_offset={end_offset}")
            errors += 1

        return end_offset

    def property_size(data, pos):
        tc = chr(data[pos])
        if tc in ("Y",): return 1 + 2
        if tc in ("C",): return 1 + 1
        if tc in ("I",): return 1 + 4
        if tc in ("F",): return 1 + 4
        if tc in ("D",): return 1 + 8
        if tc in ("L",): return 1 + 8
        if tc in ("S", "R"):
            n = struct.unpack("<I", data[pos+1:pos+5])[0]
            return 1 + 4 + n
        if tc in ("i", "l", "f", "d", "b"):
            count = struct.unpack("<I", data[pos+1:pos+5])[0]
            encoding = struct.unpack("<I", data[pos+5:pos+9])[0]
            length = struct.unpack("<I", data[pos+9:pos+13])[0]
            return 1 + 4 + 4 + 4 + length
        return None

    # Parse top-level nodes
    # Footer is at least 160 bytes (16+4+4+120+16); leave generous room
    while pos < len(data) - 160:
        result = parse_node(data, pos, 0)
        if result is None:
            # NULL terminator for top-level
            pos += 13
            break
        pos = result

    # Check footer: Footer1(16) + padding(0-15) + Footer2(4) + version(4) + Footer3(120) + Footer4(16)
    footer_start = pos
    remaining = len(data) - footer_start
    print(f"\nFooter starts at {footer_start}, remaining bytes={remaining}")

    if remaining >= 16:
        print(f"  Footer1 (16 bytes): {data[footer_start:footer_start+16].hex()}")

    # Find Footer4 (last 16 bytes) and verify the fixed magic
    if remaining >= 16:
        footer4 = data[-16:]
        expected_f4 = bytes.fromhex("f85a8c6adef5d97eece90ce3758f290b")
        if footer4 == expected_f4:
            print(f"  Footer4 magic: OK")
        else:
            print(f"  Footer4 magic: MISMATCH (got {footer4.hex()})")
            errors += 1

    # Find version field: it's at (footer_start + 16 + padding + 4)
    # padding aligns (footer_start + 16) to 16-byte boundary
    pos_after_f1 = footer_start + 16
    pad = (16 - (pos_after_f1 % 16)) % 16
    version_offset = pos_after_f1 + pad + 4  # skip Footer2 (4 zeros)
    if version_offset + 4 <= len(data):
        fv = struct.unpack("<I", data[version_offset:version_offset+4])[0]
        print(f"  Footer version field: {fv} (at offset {version_offset})")
        if fv != version:
            print(f"  ERROR: footer version {fv} != header version {version}")
            errors += 1

    # Verify file size is multiple of 16
    if len(data) % 16 != 0:
        print(f"  WARN: file size {len(data)} is not a multiple of 16")

    print(f"\nTotal nodes: {node_count}, errors: {errors}")
    return errors == 0

if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "_test_data/test_out.fbx"
    ok = parse_fbx(path)
    print(f"\n{'PASS' if ok else 'FAIL'}")
    sys.exit(0 if ok else 1)
