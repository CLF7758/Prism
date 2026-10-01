"""Dependency-free perceptual image similarity helpers."""

from PyQt6 import QtCore, QtGui


def difference_hash(image_or_pixmap, hash_size=8):
    """Return a perceptual difference hash as an integer."""
    if isinstance(image_or_pixmap, QtGui.QPixmap):
        image = image_or_pixmap.toImage()
    else:
        image = QtGui.QImage(image_or_pixmap)
    if image.isNull():
        return None
    gray = image.convertToFormat(QtGui.QImage.Format.Format_Grayscale8)
    gray = gray.scaled(
        hash_size + 1, hash_size,
        QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
        QtCore.Qt.TransformationMode.SmoothTransformation)
    result = 0
    bit = 0
    for y in range(hash_size):
        for x in range(hash_size):
            if gray.pixelColor(x, y).red() > gray.pixelColor(x + 1, y).red():
                result |= 1 << bit
            bit += 1
    return result


def hamming_distance(left, right):
    if left is None or right is None:
        return 64
    return bin(left ^ right).count('1')


def average_color(image_or_pixmap):
    """Return a small RGB colour signature used to reject dHash collisions."""
    if isinstance(image_or_pixmap, QtGui.QPixmap):
        image = image_or_pixmap.toImage()
    else:
        image = QtGui.QImage(image_or_pixmap)
    if image.isNull():
        return None
    image = image.convertToFormat(QtGui.QImage.Format.Format_RGB32).scaled(
        8, 8, QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
        QtCore.Qt.TransformationMode.SmoothTransformation)
    channels = [0, 0, 0]
    for y in range(8):
        for x in range(8):
            color = image.pixelColor(x, y)
            channels[0] += color.red()
            channels[1] += color.green()
            channels[2] += color.blue()
    return tuple(value // 64 for value in channels)


def color_distance(left, right):
    if left is None or right is None:
        return 765
    return sum(abs(a - b) for a, b in zip(left, right))


def similar_items(reference, items, max_distance=10):
    """Return ``(item, distance)`` pairs ordered by visual similarity."""
    reference_hash = difference_hash(reference.pixmap())
    matches = []
    for item in items:
        pixmap = getattr(item, 'pixmap', lambda: None)()
        if pixmap is None or pixmap.isNull():
            continue
        distance = hamming_distance(reference_hash, difference_hash(pixmap))
        if distance <= max_distance:
            matches.append((item, distance))
    return sorted(matches, key=lambda pair: pair[1])


def duplicate_groups(items, max_distance=4):
    """Return connected groups of visually duplicate image items.

    A group contains at least two items.  Connected components are used
    instead of comparing everything to one selected image, so a scan from
    the Images menu finds duplicates across the whole board.  The function
    never changes item visibility or selection.
    """
    hashed = []
    for item in items:
        pixmap = getattr(item, 'pixmap', lambda: None)()
        if pixmap is None or pixmap.isNull():
            continue
        value = difference_hash(pixmap)
        if value is not None:
            aspect = pixmap.width() / max(pixmap.height(), 1)
            hashed.append((item, value, average_color(pixmap), aspect))

    parent = list(range(len(hashed)))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left, right):
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for left in range(len(hashed)):
        for right in range(left + 1, len(hashed)):
            if (hamming_distance(hashed[left][1], hashed[right][1]) <= max_distance
                    and color_distance(hashed[left][2], hashed[right][2]) <= 45
                    and abs(hashed[left][3] - hashed[right][3]) <=
                    max(hashed[left][3], hashed[right][3]) * 0.02):
                union(left, right)

    groups = {}
    for index, (item, _value, _color, _aspect) in enumerate(hashed):
        groups.setdefault(find(index), []).append(item)
    result = [group for group in groups.values() if len(group) > 1]
    return sorted(result, key=lambda group: (-len(group),
                                             _item_name(group[0]).casefold()))


def _item_name(item):
    return (getattr(item, '_title', '') or
            getattr(item, 'filename', '') or
            'image')
