"""Find where the first FBX node record starts."""
import struct
import sys
from pathlib import Path

source = Path('.tmp/f3d_probe/box.fbx')
data = source.read_bytes()
print('header hex', data[:64].hex(' '))
for offset in range(23, 48):
    end, count, length = struct.unpack('<III', data[offset:offset + 12])
    name_len = data[offset + 12]
    name = data[offset + 13:offset + 13 + name_len]
    printable = all(32 <= byte < 127 for byte in name)
    print(offset, 'end', end, 'count', count, 'len', length,
          'namelen', name_len, 'name', name[:30],
          'PRINTABLE' if printable and name_len else '')
