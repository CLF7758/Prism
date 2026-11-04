"""Walk a real binary FBX and report where the record stream desynchronises."""
import struct
import sys
from pathlib import Path

source = Path('.tmp/f3d_probe/box.fbx')
data = source.read_bytes()
version = struct.unpack('<I', data[23:27])[0]
wide = version >= 7500
NULL = 25 if wide else 13


def read_node(offset, depth=0):
    start = offset
    head = data[offset:offset + 12]
    end, count, length = struct.unpack('<III', head)
    offset += 12
    name_len = data[offset]
    offset += 1
    name = data[offset:offset + name_len].decode('ascii', 'replace')
    offset += name_len
    for _ in range(count):
        kind = data[offset:offset + 1].decode('ascii', 'replace')
        offset += 1
        if kind == 'Y':
            offset += 2
        elif kind == 'C':
            offset += 1
        elif kind in 'IF':
            offset += 4
        elif kind in 'DL':
            offset += 8
        elif kind in 'fdlib':
            fmt = {'f': 4, 'd': 8, 'l': 8, 'i': 4, 'b': 1}[kind]
            number = struct.unpack('<I', data[offset:offset + 4])[0]
            offset += 4 + number * fmt
        elif kind == 'S':
            size = struct.unpack('<I', data[offset:offset + 4])[0]
            offset += 4 + size
        elif kind == 'R':
            size = struct.unpack('<I', data[offset:offset + 4])[0]
            offset += 4 + size
        else:
            return None, offset, ('bad prop type %r' % kind)
    children = []
    while True:
        marker = data[offset:offset + NULL]
        if len(marker) < NULL:
            return None, offset, 'eof in children'
        if marker == b'\x00' * NULL:
            offset += NULL
            break
        child, offset, error = read_node(offset, depth + 1)
        if child is None:
            return None, offset, error
        children.append(child)
    return (name, end, start, children), offset, None


offset = 27
for index in range(40):
    node, offset, error = read_node(offset)
    if node is None:
        print('STOP at', offset, error)
        print('bytes around', data[max(0, offset - 24):offset + 24].hex(' '))
        break
    name, end, start, children = node
    print('%-24s start=%-6d end=%-6d after=%-6d children=%d'
          % (name[:24], start, end, offset, len(children)))
    if end and end != offset:
        print('   !! declared end %d but stream is at %d' % (end, offset))
