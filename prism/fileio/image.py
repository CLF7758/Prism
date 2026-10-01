# This file is part of Prism.
#
# Prism is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Prism is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Prism.  If not, see <https://www.gnu.org/licenses/>.

import logging
import math
import os.path
import tempfile
from urllib.error import URLError
from urllib import parse, request

from PyQt6 import QtGui

import exif
from lxml import etree
import plum


logger = logging.getLogger(__name__)


#: Tone mapping operators offered for scene-linear (EXR/HDR) sources.
TONEMAP_OPERATORS = ('linear', 'reinhard', 'aces')
#: Channels a high dynamic range source can be displayed as.
DISPLAY_CHANNELS = ('rgb', 'r', 'g', 'b', 'alpha', 'luminance')
#: Longest edge a decoded image keeps for display. A canvas never needs more,
#: and an 8K HDRI would otherwise cost hundreds of megabytes per item.
MAX_DISPLAY_EDGE = 4096

#: Extensions treated as scene-linear, so exposure and channel controls
#: have linear data to work on.
HDR_EXTENSIONS = ('.exr', '.hdr')


def is_hdr_path(path):
    """Whether the path points at a scene-linear image format."""
    if not isinstance(path, str):
        return False
    return os.path.splitext(path)[1].lower() in HDR_EXTENSIONS


def tonemap_rgb(rgb, exposure=0.0, gamma=2.2, operator='reinhard'):
    """Map scene-linear RGB into displayable ``[0, 1]`` values.

    :param rgb: float array shaped ``(h, w, 3)`` or ``(h, w, 4)``
    :param exposure: exposure adjustment in stops
    :param gamma: display gamma; 1.0 leaves the tone curve untouched
    :param operator: one of :data:`TONEMAP_OPERATORS`
    """
    import numpy as np

    rgb = np.asarray(rgb, dtype=np.float32)
    # Real captures - and OpenEXR's own edge-case test images - can carry
    # NaN and Inf samples. Cleaning them up before the arithmetic keeps the
    # warnings out of the console and the result well defined.
    rgb = np.nan_to_num(rgb, nan=0.0, posinf=1.0, neginf=0.0)
    scaled = np.maximum(rgb * (2.0 ** float(exposure)), 0.0)
    if operator == 'linear':
        mapped = np.clip(scaled, 0.0, 1.0)
    elif operator == 'aces':
        mapped = _aces_curve(scaled)
    else:
        # 'reinhard', and any unknown value, keeps highlights usable.
        mapped = scaled / (1.0 + scaled)
    gamma = max(float(gamma), 0.1)
    if gamma != 1.0:
        mapped = np.power(mapped, 1.0 / gamma)
    return np.clip(mapped, 0.0, 1.0)


def _aces_curve(values):
    """Narkowicz's ACES filmic approximation."""
    import numpy as np

    a, b, c, d, e = 2.51, 0.03, 2.43, 0.59, 0.14
    return np.clip(
        (values * (a * values + b)) / (values * (c * values + d) + e),
        0.0, 1.0)


def extract_channel(rgba, channel='rgb'):
    """Reduce an ``(h, w, 3|4)`` float array to the channel to display.

    Single channel results are repeated to RGB so the rest of the pipeline
    keeps working on three channel images.
    """
    import numpy as np

    rgba = np.asarray(rgba)
    if channel == 'r':
        source = rgba[:, :, 0:1]
    elif channel == 'g':
        source = rgba[:, :, 1:2]
    elif channel == 'b':
        source = rgba[:, :, 2:3]
    elif channel == 'alpha' and rgba.shape[2] >= 4:
        source = rgba[:, :, 3:4]
    elif channel == 'luminance':
        rgb = rgba[:, :, :3]
        source = (0.2126 * rgb[:, :, 0:1]
                  + 0.7152 * rgb[:, :, 1:2]
                  + 0.0722 * rgb[:, :, 2:3])
    else:
        source = rgba[:, :, :3]
    if source.shape[2] == 1:
        return np.repeat(source, 3, axis=2)
    return source


#: False-colour bands, as (upper luminance bound, RGB).  Same shape as a
#: camera's exposure-zone overlay: the picture stops being a picture and
#: becomes a map of where the tones actually sit.  The last band catches
#: everything above it, because that is the clipped one.
FALSE_COLOR_BANDS = (
    (0.02, (0, 0, 90)),          # 死黑：已经压没了
    (0.10, (0, 60, 170)),        # 接近死黑
    (0.25, (0, 140, 155)),       # 暗部
    (0.60, (45, 145, 65)),       # 中间调
    (0.85, (185, 185, 65)),      # 亮部
    (0.95, (225, 145, 45)),      # 接近过曝
    (1.01, (235, 45, 45)),       # 过曝：已经洗白了
)


def false_color_preview(image, exposure=0.0):
    """Map luminance onto a false-colour ramp.

    A histogram says *how many* pixels are clipped; this says *where*.
    That is the job of the zebra / exposure-zone overlay on a camera, and
    it is the one thing the exposure and channel controls cannot show.

    Degrades to returning the image unchanged when numpy is missing, so an
    absent optional dependency means "no overlay" rather than an error.
    """
    try:
        import numpy as np
    except ImportError:
        return image

    converted = image.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
    width, height = converted.width(), converted.height()
    if not width or not height:
        return QtGui.QImage()
    buffer = converted.bits().asstring(converted.bytesPerLine() * height)
    rgba = np.frombuffer(buffer, dtype=np.uint8).reshape(
        height, converted.bytesPerLine())[:, :width * 4].reshape(
            height, width, 4).copy()

    # Rec.601 luma on the exposure-adjusted values, so the overlay tracks
    # the exposure slider the same way the picture does.
    rgb = np.clip(rgba[:, :, :3].astype(np.float32) *
                  (2.0 ** float(exposure)), 0, 255) / 255.0
    luma = (rgb[:, :, 0] * 0.299 + rgb[:, :, 1] * 0.587
            + rgb[:, :, 2] * 0.114)

    # One digitize over the whole array rather than one mask per band:
    # seven full-image comparisons on a 24-megapixel image is the
    # difference between instant and noticeable.
    bounds = np.array([upper for upper, _ in FALSE_COLOR_BANDS[:-1]],
                      dtype=np.float32)
    ramp = np.array([colour for _, colour in FALSE_COLOR_BANDS],
                    dtype=np.uint8)
    rgba[:, :, :3] = ramp[np.digitize(luma, bounds)]
    rgba[:, :, 3] = 255

    result = QtGui.QImage(rgba.data, width, height, rgba.strides[0],
                          QtGui.QImage.Format.Format_RGBA8888)
    return result.copy()


def adjust_display_image(image, exposure=0.0, channel='rgb'):
    """Return an 8-bit non-destructive preview for an ordinary QImage."""
    import numpy as np

    converted = image.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
    width, height = converted.width(), converted.height()
    if not width or not height:
        return QtGui.QImage()
    buffer = converted.bits().asstring(converted.bytesPerLine() * height)
    rgba = np.frombuffer(buffer, dtype=np.uint8).reshape(
        height, converted.bytesPerLine())[:, :width * 4].reshape(
            height, width, 4).copy()
    if channel == 'luminance' and not exposure:
        # The plain grayscale toggle lands here for every pixel of the
        # image.  Integer weights keep the whole pass inside uint16 arrays
        # rather than the float32 temporaries (plus the extra 3x repeat)
        # the generic path allocates, which made a 24-megapixel toggle take
        # well over a second.  Same luminance to within one 8-bit step.
        weights = np.array([54, 183, 19], dtype=np.uint16)
        gray = (rgba[:, :, :3].astype(np.uint16) * weights).sum(axis=2)
        rgba[:, :, :3] = (gray >> 8).astype(np.uint8)[:, :, None]
        result = QtGui.QImage(rgba.data, width, height, rgba.strides[0],
                              QtGui.QImage.Format.Format_RGBA8888)
        return result.copy()
    rgb = np.clip(rgba[:, :, :3].astype(np.float32) *
                  (2.0 ** float(exposure)), 0, 255)
    normalized = np.concatenate((rgb / 255.0,
                                 rgba[:, :, 3:4] / 255.0), axis=2)
    shown = extract_channel(normalized, channel)
    rgba[:, :, :3] = np.clip(shown * 255.0, 0, 255).astype(np.uint8)
    if channel == 'alpha':
        rgba[:, :, 3] = 255
    result = QtGui.QImage(rgba.data, width, height, rgba.strides[0],
                          QtGui.QImage.Format.Format_RGBA8888)
    return result.copy()


def _load_with_openimageio(path, exposure=0.0, gamma=2.2,
                           operator='reinhard', channel='rgb'):
    """Load formats Qt cannot decode (PSD/EXR/HDR) when OIIO is installed."""
    if not path:
        return QtGui.QImage()
    try:
        import OpenImageIO as oiio
        import numpy as np
    except ImportError:
        return QtGui.QImage()
    source = oiio.ImageInput.open(path)
    if source is None:
        logger.debug('OpenImageIO decode failed: %s', oiio.geterror())
        return QtGui.QImage()
    try:
        spec = source.spec()
        pixels = source.read_image(format=oiio.FLOAT)
    finally:
        source.close()
    if pixels is None:
        return QtGui.QImage()
    pixels = np.asarray(pixels, dtype=np.float32)
    if pixels.ndim == 1:
        pixels = pixels.reshape(spec.height, spec.width, spec.nchannels)
    if pixels.shape[2] < 3:
        gray = pixels[:, :, :1]
        alpha = pixels[:, :, 1:2] if pixels.shape[2] == 2 else None
        pixels = np.repeat(gray, 3, axis=2)
        if alpha is not None:
            pixels = np.concatenate((pixels, alpha), axis=2)
    rgb = pixels[:, :, :3]
    is_hdr = is_hdr_path(path)
    height, width = pixels.shape[0], pixels.shape[1]
    if pixels.shape[2] >= 4:
        alpha01 = np.clip(pixels[:, :, 3:4], 0.0, 1.0)
    else:
        alpha01 = np.ones((height, width, 1), dtype=np.float32)

    if is_hdr:
        # EXR/HDR are normally scene-linear: apply the exposure and the
        # selected tone curve instead of clipping the highlights away.
        rgb01 = tonemap_rgb(rgb, exposure, gamma, operator)
        if channel != 'rgb':
            # Channel isolation only makes sense for scene-linear sources,
            # and only after tone mapping so the values stay in [0, 1].
            rgb01 = extract_channel(
                np.concatenate((rgb01, alpha01), axis=2), channel)
    else:
        rgb01 = rgb

    rgb01 = np.nan_to_num(rgb01, nan=0.0, posinf=1.0, neginf=0.0)
    rgb = np.clip(rgb01 * 255.0, 0, 255).astype(np.uint8)
    alpha = np.clip(alpha01 * 255.0, 0, 255).astype(np.uint8)
    rgba = np.ascontiguousarray(np.concatenate((rgb, alpha), axis=2))

    # Downsample before handing the buffer to Qt: nothing on the canvas
    # needs more than MAX_DISPLAY_EDGE pixels, and keeping the full 8K
    # buffer per item costs hundreds of megabytes.
    longest = max(width, height)
    if longest > MAX_DISPLAY_EDGE:
        step = int(math.ceil(longest / float(MAX_DISPLAY_EDGE)))
        rgba = np.ascontiguousarray(rgba[::step, ::step])
        height, width = rgba.shape[0], rgba.shape[1]

    image = QtGui.QImage(
        rgba.data, width, height, rgba.strides[0],
        QtGui.QImage.Format.Format_RGBA8888)
    return image.copy()


def exif_rotated_image(path=None, hdr_options=None):
    """Returns a QImage that is transformed according to the source's
    orientation EXIF data.

    ``hdr_options`` (exposure, gamma, operator, channel) is forwarded to the
    OpenImageIO loader used for scene-linear formats.
    """
    # Display-only options (e.g. false_color) are applied by the inspector,
    # not by the decoder. Keep this boundary safe for all loader callers.
    options = {key: value for key, value in (hdr_options or {}).items()
               if key in ('exposure', 'gamma', 'operator', 'channel')}
    if is_hdr_path(path):
        # Scene-linear sources go through OpenImageIO first, so the exposure
        # and channel controls have linear data to work on. If OIIO is
        # missing or fails, the Qt path below still gets a chance.
        image = _load_with_openimageio(path, **options)
        if not image.isNull():
            return image

    img = QtGui.QImage(path)
    if img.isNull():
        return _load_with_openimageio(path, **options)

    with open(path, 'rb') as f:
        try:
            exifimg = exif.Image(f)
        except (plum.exceptions.UnpackError, NotImplementedError):
            logger.exception(f'Exif parser failed on image: {path}')
            return img

    try:
        if 'orientation' in exifimg.list_all():
            orientation = exifimg.orientation
        else:
            return img
    except (NotImplementedError, ValueError):
        logger.exception(f'Exif failed reading orientation of image: {path}')
        return img

    transform = QtGui.QTransform()

    if orientation == exif.Orientation.TOP_RIGHT:
        return img.mirrored(horizontal=True, vertical=False)
    if orientation == exif.Orientation.BOTTOM_RIGHT:
        transform.rotate(180)
        return img.transformed(transform)
    if orientation == exif.Orientation.BOTTOM_LEFT:
        return img.mirrored(horizontal=False, vertical=True)
    if orientation == exif.Orientation.LEFT_TOP:
        transform.rotate(90)
        return img.transformed(transform).mirrored(
            horizontal=True, vertical=False)
    if orientation == exif.Orientation.RIGHT_TOP:
        transform.rotate(90)
        return img.transformed(transform)
    if orientation == exif.Orientation.RIGHT_BOTTOM:
        transform.rotate(270)
        return img.transformed(transform).mirrored(
            horizontal=True, vertical=False)
    if orientation == exif.Orientation.LEFT_BOTTOM:
        transform.rotate(270)
        return img.transformed(transform)

    return img


def load_image(path, hdr_options=None):
    if isinstance(path, str):
        path = os.path.normpath(path)
        return (exif_rotated_image(path, hdr_options), path)
    if path.isLocalFile():
        path = os.path.normpath(path.toLocalFile())
        return (exif_rotated_image(path, hdr_options), path)

    url = bytes(path.toEncoded()).decode()
    domain = '.'.join(parse.urlparse(url).netloc.split(".")[-2:])
    img = exif_rotated_image(None, hdr_options)
    if domain == 'pinterest.com':
        try:
            with request.urlopen(url, timeout=20) as response:
                page_data = response.read(5 * 1024 * 1024 + 1)
            if len(page_data) > 5 * 1024 * 1024:
                raise ValueError('Pinterest page exceeds 5 MiB')
            root = etree.HTML(page_data)
            url = root.xpath("//img")[0].get('src')
        except Exception as e:
            logger.debug(f'Pinterest image download failed: {e}')
    try:
        with request.urlopen(url, timeout=20) as response:
            imgdata = response.read(50 * 1024 * 1024 + 1)
        if len(imgdata) > 50 * 1024 * 1024:
            logger.warning('Image download exceeds 50 MiB: %s', url)
            return (img, url)
    except (URLError, OSError) as e:
        logger.debug('Downloading image failed: %s', e)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            fname = os.path.join(tmp, 'img')
            with open(fname, 'wb') as f:
                f.write(imgdata)
                logger.debug(f'Temporarily saved in: {fname}')
            img = exif_rotated_image(fname, hdr_options)
    return (img, url)
