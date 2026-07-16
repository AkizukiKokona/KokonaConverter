"""Comprehensive FBX 7400 binary validator.

Parses every node recursively, validates all offsets, and reports any structural
issues that could cause strict parsers (like UE5.8 Interchange) to reject the file.
"""
import struct
import sys
import zlib


def parse_properties(data, pos, num_props, prop_list_len):
    """Parse properties starting at pos. Returns (props_list, next_pos)."""
    props = []
    start = pos
    for i in range(num_props):
        if pos >= len(data):
            return props, pos, f"EOF reading property {i}"
        tc = chr(data[pos])
        pos += 1
        if tc == 'Y':
            val = struct.unpack('<h', data[pos:pos+2])[0]; pos += 2
            props.append(('Y', val))
        elif tc == 'C':
            val = data[pos]; pos += 1
            props.append(('C', val))
        elif tc == 'I':
            val = struct.unpack('<i', data[pos:pos+4])[0]; pos += 4
            props.append(('I', val))
        elif tc == 'F':
            val = struct.unpack('<f', data[pos:pos+4])[0]; pos += 4
            props.append(('F', val))
        elif tc == 'D':
            val = struct.unpack('<d', data[pos:pos+8])[0]; pos += 8
            props.append(('D', val))
        elif tc == 'L':
            val = struct.unpack('<q', data[pos:pos+8])[0]; pos += 8
            props.append(('L', val))
        elif tc == 'S' or tc == 'R':
            slen = struct.unpack('<I', data[pos:pos+4])[0]; pos += 4
            if pos + slen > len(data):
                return props, pos, f"{tc} length {slen} exceeds file"
            raw = data[pos:pos+slen]; pos += slen
            if tc == 'S':
                props.append(('S', raw.decode('utf-8', errors='replace')))
            else:
                props.append(('R', raw))
        elif tc in ('i', 'l', 'f', 'd', 'b'):
            count = struct.unpack('<I', data[pos:pos+4])[0]; pos += 4
            encoding = struct.unpack('<I', data[pos:pos+4])[0]; pos += 4
            comp_len = struct.unpack('<I', data[pos:pos+4])[0]; pos += 4
            if pos + comp_len > len(data):
                return props, pos, f"array {tc} comp_len {comp_len} exceeds file"
            raw = data[pos:pos+comp_len]; pos += comp_len
            if encoding == 1:
                try:
                    raw = zlib.decompress(raw)
                except Exception as e:
                    return props, pos, f"zlib decompress failed: {e}"
            # Verify decompressed length
            elem_sizes = {'i': 4, 'l': 8, 'f': 4, 'd': 8, 'b': 1}
            expected_len = count * elem_sizes[tc]
            if len(raw) != expected_len:
                return props, pos, f"array {tc} count={count} expected {expected_len} bytes, got {len(raw)}"
            props.append((tc, f"array[{count}]"))
        else:
            return props, pos, f"unknown property type '{tc}' at offset {pos-1}"
    
    # Verify prop_list_len
    actual_len = pos - start
    if actual_len != prop_list_len:
        return props, pos, f"prop_list_len mismatch: declared={prop_list_len} actual={actual_len}"
    
    return props, pos, None


def parse_node(data, pos, depth=0, path=""):
    """Parse a single node and its children. Returns (node_info, next_pos, error)."""
    if pos + 13 > len(data):
        return None, pos, f"EOF at pos {pos}, need 13 bytes for node header"
    
    end_offset = struct.unpack('<I', data[pos:pos+4])[0]
    num_props = struct.unpack('<I', data[pos+4:pos+8])[0]
    prop_list_len = struct.unpack('<I', data[pos+8:pos+12])[0]
    name_len = data[pos+12]
    
    # NULL record
    if end_offset == 0 and num_props == 0 and prop_list_len == 0 and name_len == 0:
        return {'name': '__NULL__', 'pos': pos, 'end': pos + 13}, pos + 13, None
    
    if name_len == 0:
        return None, pos, f"pos {pos}: name_len=0 but not NULL record (end={end_offset} props={num_props})"
    
    if pos + 13 + name_len > len(data):
        return None, pos, f"pos {pos}: name extends beyond file"
    
    name = data[pos+13:pos+13+name_len].decode('utf-8', errors='replace')
    full_path = f"{path}/{name}" if path else name
    
    if end_offset <= pos:
        return None, pos, f"pos {pos}: node '{name}' end_offset={end_offset} <= pos={pos}"
    if end_offset > len(data):
        return None, pos, f"pos {pos}: node '{name}' end_offset={end_offset} > file_size={len(data)}"
    
    # Parse properties
    props_start = pos + 13 + name_len
    props, props_end, prop_error = parse_properties(data, props_start, num_props, prop_list_len)
    if prop_error:
        return None, pos, f"pos {pos}: node '{name}' property error: {prop_error}"
    
    # Parse children
    children = []
    child_pos = props_end
    while child_pos < end_offset:
        child_info, next_pos, child_error = parse_node(data, child_pos, depth+1, full_path)
        if child_error:
            return None, pos, f"pos {pos}: node '{name}' child error at {child_pos}: {child_error}"
        if child_info and child_info['name'] == '__NULL__':
            child_pos = next_pos
            break
        children.append(child_info)
        child_pos = next_pos
    
    if child_pos != end_offset:
        return None, pos, f"pos {pos}: node '{name}' children end at {child_pos} but end_offset={end_offset}"
    
    node_info = {
        'name': name,
        'path': full_path,
        'pos': pos,
        'end': end_offset,
        'num_props': num_props,
        'prop_list_len': prop_list_len,
        'props': props,
        'children': children,
        'depth': depth,
    }
    return node_info, end_offset, None


def validate_fbx(path):
    with open(path, 'rb') as f:
        data = f.read()
    
    # Check magic
    expected_magic = b"Kaydara FBX Binary  \x00\x1a\x00"
    magic = data[:23]
    version = struct.unpack('<I', data[23:27])[0]
    
    print(f"=== {path.split('/')[-1]} ===")
    print(f"  Size: {len(data)} bytes")
    print(f"  Magic: {'OK' if magic == expected_magic else 'MISMATCH!'}")
    print(f"  Magic bytes: {magic.hex()}")
    print(f"  Version: {version}")
    print(f"  Size % 16: {len(data) % 16}")
    
    # Parse all top-level nodes
    pos = 27
    top_nodes = []
    errors = []
    node_count = 0
    
    while pos < len(data):
        node_info, next_pos, error = parse_node(data, pos)
        if error:
            errors.append(f"pos {pos}: {error}")
            break
        if node_info and node_info['name'] == '__NULL__':
            pos = next_pos
            break
        top_nodes.append(node_info)
        pos = next_pos
        node_count += 1
        if node_count > 200:
            errors.append("too many top-level nodes")
            break
    
    print(f"  Top-level nodes: {len(top_nodes)}")
    for n in top_nodes:
        prop_summary = ", ".join(f"{p[0]}:{repr(p[1])[:40]}" for p in n['props'][:3])
        print(f"    {n['name']:25s} props={n['num_props']} children={len(n['children'])} [{prop_summary}]")
    
    # Count total nodes recursively
    def count_nodes(node):
        if node['name'] == '__NULL__':
            return 0
        return 1 + sum(count_nodes(c) for c in node['children'])
    
    total = sum(count_nodes(n) for n in top_nodes)
    print(f"  Total nodes (recursive): {total}")
    
    if errors:
        print(f"  ERRORS ({len(errors)}):")
        for e in errors:
            print(f"    {e}")
    else:
        print(f"  No structural errors found!")
    
    # Verify footer
    if pos < len(data):
        remaining = len(data) - pos
        print(f"  Footer: {remaining} bytes after NULL record")
        # Footer should be: Footer1(16) + padding(0-15) + Footer2(4) + version(4) + Footer3(120) + Footer4(16) = 160 + padding
        footer4 = data[-16:]
        footer4_expected = bytes.fromhex("f85a8c6adef5d97eece90ce3758f290b")
        print(f"  Footer4 magic: {'OK' if footer4 == footer4_expected else 'MISMATCH!'}")
    
    print()
    return len(errors) == 0


if __name__ == '__main__':
    paths = sys.argv[1:] if len(sys.argv) > 1 else ['/workspace/_test_data/test_complex.fbx']
    all_ok = True
    for p in paths:
        ok = validate_fbx(p)
        all_ok = all_ok and ok
    sys.exit(0 if all_ok else 1)
