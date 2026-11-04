"""Screenshot quality fingerprints for the scale-acceptance baseline.

The acceptance run has to *disprove* three failure modes, not just "look
fine":

* a solid-colour block where an image should be (a blank or failed tile);
* a missing image inside the viewport (a card that never painted);
* a downscaled stand-in passed off as the real thing at close range.

A screenshot alone cannot answer those. This module turns every screenshot
into numbers a reader can check: how much of the frame is a single colour,
how bright it is, how much it varies, how many distinct colours it holds,
and -- per card region -- whether that card actually carries an image.
"""
from __future__ import annotations

import numpy as np

#: A frame whose non-uniform share is below this is treated as a solid block.
PURE_BLOCK_RATIO = 0.01
#: A frame whose luminance standard deviation is below this is treated as flat.
FLAT_STDDEV = 3.0
#: A card region is considered "empty" when it is flat and near-uniform.
EMPTY_REGION_STDDEV = 2.0
EMPTY_REGION_UNIFORM = 0.02


def qimage_to_array(image):
    """A QImage as an ``(h, w, 3)`` uint8 RGB array.

    The array owns its pixels: a view into a QImage's buffer would dangle
    as soon as the temporary QImage is collected.
    """
    from PyQt6 import QtGui
    if image is None or image.isNull():
        return None
    converted = image.convertToFormat(QtGui.QImage.Format.Format_RGB888)
    width, height = converted.width(), converted.height()
    if not width or not height:
        return None
    pointer = converted.bits()
    pointer.setsize(converted.sizeInBytes())
    buffer = np.frombuffer(pointer, dtype=np.uint8)
    stride = converted.bytesPerLine()
    usable = min(buffer.size, stride * height)
    buffer = buffer[:usable]
    if usable < stride * height:  # pragma: no cover - defensive
        return None
    return np.array(buffer.reshape(height, stride)[:, :width * 3].reshape(height, width, 3))


def fingerprint_array(rgb):
    """Colour statistics of one RGB array.

    ``unique_colors`` is counted on a 5-bit-per-channel quantisation so a
    1580x1000 frame does not need a 1.5-million-entry set; the quantisation
    is reported so the number can be read for what it is.
    """
    if rgb is None or rgb.size == 0:
        return dict(error='empty image')
    pixels = rgb.reshape(-1, 3).astype(np.int16)
    luma = (0.2126 * pixels[:, 0] + 0.7152 * pixels[:, 1] + 0.0722 * pixels[:, 2])
    quantized = (rgb.reshape(-1, 3) >> 3).astype(np.int32)
    packed = (quantized[:, 0] << 10) | (quantized[:, 1] << 5) | quantized[:, 2]
    values, counts = np.unique(packed, return_counts=True)
    total = int(packed.size)
    modal = int(counts.max())
    unique_colors = int(values.size)
    non_uniform_ratio = 1.0 - (modal / total)
    stddev = float(luma.std())
    mean = float(luma.mean())
    verdict = 'ok'
    if non_uniform_ratio < PURE_BLOCK_RATIO:
        verdict = 'solid-block'
    elif stddev < FLAT_STDDEV:
        verdict = 'flat'
    return dict(width=int(rgb.shape[1]),
                height=int(rgb.shape[0]),
                pixels=total,
                mean_luma=round(mean, 3),
                stddev_luma=round(stddev, 3),
                unique_colors=unique_colors,
                unique_color_quantisation='5 bits per channel (32768 buckets)',
                modal_color_ratio=round(modal / total, 6),
                non_uniform_ratio=round(non_uniform_ratio, 6),
                verdict=verdict)


def fingerprint_image(image):
    """Colour statistics of one QImage."""
    return fingerprint_array(qimage_to_array(image))


def fingerprint_file(path):
    """Colour statistics of a PNG on disk (no Qt needed)."""
    from PIL import Image
    with Image.open(path) as handle:
        rgb = np.asarray(handle.convert('RGB'), dtype=np.uint8)
    return fingerprint_array(rgb)


def region_fingerprint(rgb, rect):
    """Colour statistics of one rectangle of an RGB array.

    ``rect`` is ``(x, y, width, height)`` in pixels of ``rgb``.
    """
    if rgb is None:
        return dict(error='empty image')
    x, y, width, height = (int(value) for value in rect)
    image_height, image_width = rgb.shape[0], rgb.shape[1]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(image_width, x + width), min(image_height, y + height)
    if x1 <= x0 or y1 <= y0:
        return dict(error='rect outside image', rect=[x, y, width, height])
    crop = rgb[y0:y1, x0:x1]
    stats = fingerprint_array(crop)
    stats['rect'] = [x, y, width, height]
    stats['empty'] = bool(stats.get('stddev_luma', 0) is not None
                          and stats['stddev_luma'] < EMPTY_REGION_STDDEV
                          and stats.get('non_uniform_ratio', 1) < EMPTY_REGION_UNIFORM)
    return stats


__all__ = ['EMPTY_REGION_STDDEV', 'EMPTY_REGION_UNIFORM', 'FLAT_STDDEV',
           'PURE_BLOCK_RATIO', 'fingerprint_array', 'fingerprint_file',
           'fingerprint_image', 'qimage_to_array', 'region_fingerprint']
