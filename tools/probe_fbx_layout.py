"""Dump the node structure of a real binary FBX file."""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

source = Path(sys.argv[1] if len(sys.argv) > 1
              else '.tmp/f3d_probe/box.fbx')
data = source.read_bytes()
print('size', len(data))
print('header', data[:32])
version = struct.unpack('<I', data[23:27])[0]
print('version', version)
wide = version >= 7500
offset = 27
null = 25 if wide else 13
print('null size', null)


def read_node(data, offset, wide, depth=0):
    offset_size = 8 if wide else 4
    head = data[offset:offset + 8 + offset_size * 2]
    if len(head) < 8 + offset_size * 2:
        return None, offset
    if wide:
        end, count, length = struct.unpack('<QQQ', head[:24])
    else:
        end, count, length = struct.unpack('<III', head[:12])
    offset += 8 + offset_size * 2
    name_len = data[offset]
    offset += 1
    name = data[offset:offset + name_len].decode('utf-8', 'replace')
    offset += name_len
    print('  ' * depth, 'node', name, 'props', count, 'end', end)
    props = []
    for _ in range(count):
        kind = data[offset:offset + 1].decode('ascii', 'replace')
        offset += 1
        if kind == 'Y':
            props.append(struct.unpack('<h', data[offset:offset + 2])[0]); offset += 2
        elif kind == 'C':
            props.append(bool(data[offset])); offset += 1
        elif kind == 'I':
            props.append(struct.unpack('<i', data[offset:offset + 4])[0]); offset += 4
        elif kind == 'F':
            props.append(struct.unpack('<f', data[offset:offset + 4])[0]); offset += 4
        elif kind == 'D':
            props.append(struct.unpack('<d', data[offset:offset + 8])[0]); offset += 8
        elif kind == 'L':
            props.append(struct.unpack('<q', data[offset:offset + 8])[0]); offset += 8
        elif kind in 'fdlib':
            fmt = {'f': ('f', 4), 'd': ('d', 8), 'l': ('q', 8), 'i': ('i', 4), 'b': ('?', 1)}[kind]
            number = struct.unpack('<I', data[offset:offset + 4])[0]; offset += 4
            raw = data[offset:offset + number * fmt[1]]; offset += number * fmt[1]
            props.append(list(struct.unpack('<' + fmt[0] * number, raw)) if number else [])
        elif kind == 'S':
            size = struct.unpack('<I', data[offset:offset + 4])[0]; offset += 4
            props.append(data[offset:offset + size].decode('utf-8', 'replace')); offset += size
        elif kind == 'R':
            size = struct.unpack('<I', data[offset:offset + 4])[0]; offset += 4
            props.append(data[offset:offset + size]); offset += size
        else:
            print('  ' * depth, 'UNKNOWN PROP TYPE', repr(kind), 'at', offset - 1)
            print('  ' * depth, 'context', data[max(0, offset - 40):offset + 20])
            raise SystemExit(1)
    children = []
    while True:
        marker = data[offset:offset + null]
        if len(marker) < null:
            return (name, props, children), offset
        if marker == b'\x00' * null:
            offset += null
            break
        child, offset = read_node(data, offset, wide, depth + 1)
        if child is None:
            return (name, props, children), offset
        children.append(child)
    return (name, props, children), offset


offset = 27
for index in range(6):
    node, offset = read_node(data, offset, wide)
    if node is None:
        break
    print('top node', index, node[0], '->', offset)
print('after top level', offset, 'remaining', len(data) - offset)
