"""Locate the exact property that desynchronises the FBX stream."""
import struct
from pathlib import Path

source = Path('.tmp/f3d_probe/box.fbx')
data = source.read_bytes()
version = struct.unpack('<I', data[23:27])[0]
wide = version >= 7500
NULL = 25 if wide else 13
WIDTH = {'f': 4, 'd': 8, 'l': 8, 'i': 4, 'b': 1}


def read_node(offset, path=''):
    start = offset
    end, count, length = struct.unpack('<III', data[offset:offset + 12])
    offset += 12
    name_len = data[offset]
    offset += 1
    name = data[offset:offset + name_len].decode('ascii', 'replace')
    offset += name_len
    here = path + '/' + name
    for index in range(count):
        kind = data[offset:offset + 1].decode('ascii', 'replace')
        prop_start = offset
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
            number = struct.unpack('<I', data[offset:offset + 4])[0]
            offset += 4
            print('  array prop %r count=%d at %d bytes=%s'
                  % (kind, number, prop_start,
                     data[offset:offset + min(number * WIDTH[kind], 24)].hex(' ')))
            offset += number * WIDTH[kind]
        elif kind == 'S':
            size = struct.unpack('<I', data[offset:offset + 4])[0]
            offset += 4 + size
        elif kind == 'R':
            size = struct.unpack('<I', data[offset:offset + 4])[0]
            offset += 4 + size
        else:
            print('BAD prop type %r at %d in %s' % (kind, prop_start, here))
            print('bytes:', data[prop_start - 40:prop_start + 40].hex(' '))
            raise SystemExit(1)
    children = []
    while True:
        marker = data[offset:offset + NULL]
        if len(marker) < NULL:
            raise SystemExit('eof')
        if marker == b'\x00' * NULL:
            offset += NULL
            break
        offset = read_node(offset, here)
    return offset


try:
    read_node(27)
except SystemExit:
    pass
