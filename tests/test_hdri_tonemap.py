"""Tone mapping and channel extraction for scene-linear (EXR/HDR) sources.

These are the pure functions behind the HDRI exposure/channel controls, so
they are tested directly rather than through a decoded image.
"""
import pytest

np = pytest.importorskip('numpy')

from prism.fileio.image import (DISPLAY_CHANNELS, MAX_DISPLAY_EDGE,  # noqa: E402
                                TONEMAP_OPERATORS, extract_channel,
                                tonemap_rgb)


# ── Tone mapping ─────────────────────────────────────────────────────


def test_tonemap_preserves_shape():
    rgb = np.zeros((5, 7, 3), dtype=np.float32)
    assert tonemap_rgb(rgb).shape == (5, 7, 3)


def test_black_stays_black():
    rgb = np.zeros((2, 2, 3), dtype=np.float32)
    assert tonemap_rgb(rgb).max() == pytest.approx(0.0)


def test_all_operators_stay_in_range():
    rgb = np.full((2, 2, 3), 1000.0, dtype=np.float32)
    for operator in TONEMAP_OPERATORS:
        out = tonemap_rgb(rgb, operator=operator)
        assert out.min() >= 0.0
        assert out.max() <= 1.0


def test_exposure_brightens_the_image():
    rgb = np.full((2, 2, 3), 0.1, dtype=np.float32)
    base = tonemap_rgb(rgb, exposure=0.0, gamma=1.0, operator='linear')
    lifted = tonemap_rgb(rgb, exposure=1.0, gamma=1.0, operator='linear')
    assert lifted.mean() > base.mean()


def test_negative_exposure_darkens_the_image():
    rgb = np.full((2, 2, 3), 0.5, dtype=np.float32)
    base = tonemap_rgb(rgb, exposure=0.0, gamma=1.0, operator='linear')
    darker = tonemap_rgb(rgb, exposure=-2.0, gamma=1.0, operator='linear')
    assert darker.mean() < base.mean()


def test_linear_clips_above_one():
    rgb = np.full((2, 2, 3), 2.0, dtype=np.float32)
    out = tonemap_rgb(rgb, gamma=1.0, operator='linear')
    assert out.max() == pytest.approx(1.0)


def test_reinhard_compresses_rather_than_clips():
    rgb = np.full((2, 2, 3), 100.0, dtype=np.float32)
    out = tonemap_rgb(rgb, gamma=1.0, operator='reinhard')
    assert out.max() < 1.0
    assert out.max() > 0.5


def test_aces_is_bounded_and_monotonic_at_the_top():
    low = tonemap_rgb(np.full((1, 1, 3), 0.5, np.float32),
                      gamma=1.0, operator='aces')
    high = tonemap_rgb(np.full((1, 1, 3), 5.0, np.float32),
                       gamma=1.0, operator='aces')
    assert high.mean() > low.mean()
    assert high.max() <= 1.0


def test_unknown_operator_falls_back_to_reinhard():
    rgb = np.full((2, 2, 3), 4.0, dtype=np.float32)
    known = tonemap_rgb(rgb, gamma=1.0, operator='reinhard')
    unknown = tonemap_rgb(rgb, gamma=1.0, operator='not-a-real-operator')
    assert np.allclose(known, unknown)


def test_negative_samples_are_clamped_not_wrapped():
    rgb = np.full((2, 2, 3), -5.0, dtype=np.float32)
    assert tonemap_rgb(rgb).min() == pytest.approx(0.0)


def test_gamma_brightens_midtones():
    rgb = np.full((2, 2, 3), 0.25, dtype=np.float32)
    untouched = tonemap_rgb(rgb, gamma=1.0, operator='linear')
    corrected = tonemap_rgb(rgb, gamma=2.2, operator='linear')
    assert corrected.mean() > untouched.mean()


def test_absurd_gamma_does_not_explode():
    rgb = np.full((2, 2, 3), 0.5, dtype=np.float32)
    out = tonemap_rgb(rgb, gamma=0.0, operator='linear')
    assert np.isfinite(out).all()


# ── Channel extraction ───────────────────────────────────────────────


@pytest.mark.parametrize('channel', DISPLAY_CHANNELS)
def test_extract_channel_always_yields_rgb(channel):
    rgba = np.random.rand(4, 4, 4).astype(np.float32)
    assert extract_channel(rgba, channel).shape == (4, 4, 3)


def test_extract_red_isolates_the_red_channel():
    rgba = np.zeros((2, 2, 4), dtype=np.float32)
    rgba[:, :, 0] = 1.0
    rgba[:, :, 1] = 0.25
    red = extract_channel(rgba, 'r')
    full = extract_channel(rgba, 'rgb')
    # A single channel is displayed as greyscale, so every component
    # carries the red value...
    assert red[:, :, 0].min() == pytest.approx(1.0)
    assert red[:, :, 1].min() == pytest.approx(1.0)
    # ...whereas the RGB view keeps the individual channels.
    assert full[:, :, 1].min() == pytest.approx(0.25)
    assert not np.allclose(red, full)


def test_extract_alpha_reads_the_fourth_channel():
    rgba = np.zeros((2, 2, 4), dtype=np.float32)
    rgba[:, :, 3] = 0.5
    alpha = extract_channel(rgba, 'alpha')
    assert alpha[:, :, 0].min() == pytest.approx(0.5)


def test_extract_alpha_without_alpha_falls_back_to_rgb():
    rgb = np.zeros((2, 2, 3), dtype=np.float32)
    assert extract_channel(rgb, 'alpha').shape == (2, 2, 3)


def test_luminance_uses_rec709_weights():
    rgba = np.zeros((1, 1, 4), dtype=np.float32)
    rgba[0, 0, 1] = 1.0                       # pure green
    out = extract_channel(rgba, 'luminance')
    assert out[0, 0, 0] == pytest.approx(0.7152, abs=1e-4)


def test_unknown_channel_returns_rgb():
    rgba = np.random.rand(2, 2, 4).astype(np.float32)
    out = extract_channel(rgba, 'not-a-channel')
    assert np.allclose(out, rgba[:, :, :3])


# ── Downsampling constant ────────────────────────────────────────────


def test_display_edge_limit_is_sane():
    assert MAX_DISPLAY_EDGE >= 1024
    assert MAX_DISPLAY_EDGE <= 16384


# ── Source detection and the loader contract ─────────────────────────


def test_is_hdr_path_recognises_scene_linear_formats():
    from prism.fileio.image import is_hdr_path

    assert is_hdr_path('studio.exr')
    assert is_hdr_path('SKY.HDR')
    assert not is_hdr_path('photo.png')
    assert not is_hdr_path(None)
    assert not is_hdr_path(1234)


def test_exif_rotated_image_accepts_hdr_options(qapp, tmp_path):
    """The panel re-decodes through this signature; it must stay accepted."""
    from PyQt6 import QtCore, QtGui

    from prism.fileio.image import exif_rotated_image

    path = tmp_path / 'plain.png'
    image = QtGui.QImage(4, 4, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('red'))
    assert image.save(str(path))

    loaded = exif_rotated_image(str(path), {'exposure': 1.0, 'channel': 'r'})
    assert not loaded.isNull()
    assert loaded.size() == QtCore.QSize(4, 4)


def test_load_image_accepts_hdr_options(qapp, tmp_path):
    from PyQt6 import QtGui

    from prism.fileio.image import load_image

    path = tmp_path / 'plain.png'
    image = QtGui.QImage(4, 4, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('blue'))
    assert image.save(str(path))

    loaded, name = load_image(str(path), {'exposure': -1.0})
    assert not loaded.isNull()
    assert name == str(path)


def test_hdr_loader_does_not_forward_display_only_options(qapp, monkeypatch):
    from PyQt6 import QtGui
    from prism.fileio import image as image_io

    received = []
    # Strict signature mirrors the actual decoder, not a permissive mock.
    def decode(path, exposure=0.0, gamma=2.2, operator='reinhard', channel='rgb'):
        received.append(channel)
        result = QtGui.QImage(4, 4, QtGui.QImage.Format.Format_RGB32)
        result.fill(QtGui.QColor('red'))
        return result

    monkeypatch.setattr(image_io, '_load_with_openimageio', decode)
    for channel in ['rgb', 'r', 'g', 'b', 'alpha', 'luminance']:
        result, _ = image_io.load_image('scene.hdr', {
            'channel': channel, 'false_color': True, 'exposure': 0.0})
        assert not result.isNull()
    assert received == ['rgb', 'r', 'g', 'b', 'alpha', 'luminance']
