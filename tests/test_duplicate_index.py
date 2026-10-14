import random
from unittest.mock import Mock
from PyQt6 import QtGui
from prism import fileio
from prism.duplicate_scan import ImageSource, DuplicateScanService


def test_preparing_file_source_does_not_read_original(view, tmp_path, monkeypatch):
    path = tmp_path/'source.png'
    image = QtGui.QImage(64, 64, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('red'))
    assert image.save(str(path))
    item = fileio._load_image_item(str(path))
    view.scene.addItem(item)
    monkeypatch.setattr(item, 'original_bytes', Mock(side_effect=AssertionError('GUI original read')))
    source = ImageSource.from_item(item)
    assert source._data is None
    assert source.snapshot is not None
    assert source.bytes_ == path.read_bytes()
    source.release()
    assert source._data is None


def test_index_preserves_brute_force_connected_groups(qapp):
    rng = random.Random(31415)
    service = DuplicateScanService()
    entries = []
    for n in range(80):
        value = rng.getrandbits(64)
        changed = value
        for bit in rng.sample(range(64), 4):
            changed ^= 1 << bit
        for variant, fingerprint in enumerate((value, changed)):
            source = ImageSource(str(n*2+variant), str(n*2+variant), b'')
            entries.append((source, fingerprint, (100, 110, 120), 1.5))
    parent = list(range(len(entries)))
    def root(i):
        while parent[i] != i:
            i = parent[i]
        return i
    for left in range(len(entries)):
        for right in range(left+1, len(entries)):
            if service._close_enough(entries[left], entries[right]):
                parent[root(right)] = root(left)
    expected = {}
    for n, entry in enumerate(entries):
        expected.setdefault(root(n), set()).add(entry[0].key)
    expected = {frozenset(g) for g in expected.values() if len(g) > 1}
    actual = {frozenset(s.key for s in group) for group in service._group(entries)}
    assert actual == expected


def test_second_scan_of_unchanged_source_skips_original_read(view, tmp_path, monkeypatch):
    path = tmp_path/'unchanged.png'
    image = QtGui.QImage(64, 64, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('red'))
    assert image.save(str(path))
    item = fileio._load_image_item(str(path))
    view.scene.addItem(item)
    service = DuplicateScanService()
    service.scan([ImageSource.from_item(item)])
    source = ImageSource.from_item(item)
    monkeypatch.setattr(ImageSource, 'digest', Mock(side_effect=AssertionError('original reread')))
    service.scan([source])
    assert source._data is None


def test_worker_fingerprint_reads_known_png(qapp):
    from PyQt6 import QtCore
    image = QtGui.QImage(40, 40, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('red'))
    data = QtCore.QByteArray()
    buffer = QtCore.QBuffer(data)
    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, 'PNG')
    source = ImageSource('fallback', 'fallback', bytes(data))
    assert DuplicateScanService()._fingerprint(source) is not None


def test_persistent_fingerprints_survive_service_recreation(view, tmp_path, monkeypatch):
    path = tmp_path/'persistent.png'
    image = QtGui.QImage(64, 64, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('blue'))
    assert image.save(str(path))
    item = fileio._load_image_item(str(path))
    view.scene.addItem(item)
    first = DuplicateScanService()
    first.cache_directory = str(tmp_path/'cache')
    first.scan([ImageSource.from_item(item)])
    second = DuplicateScanService()
    second.cache_directory = first.cache_directory
    monkeypatch.setattr(ImageSource, 'digest', Mock(side_effect=AssertionError('persistent hit must skip original')))
    monkeypatch.setattr(second, '_fingerprint', Mock(side_effect=AssertionError('persistent hit must skip decode')))
    second.scan([ImageSource.from_item(item)])


def test_persistent_fingerprint_cache_prunes_entries(tmp_path):
    from prism.duplicate_cache import FingerprintCache
    store = FingerprintCache(tmp_path, limit=3)
    for n in range(8):
        store.put(str(n), (n, (100, 100, 100), 1.0))
    store.close()
    reopened = FingerprintCache(tmp_path, limit=3)
    try:
        assert reopened.get('0') is None
        assert reopened.get('7') == (7, (100, 100, 100), 1.0)
    finally:
        reopened.close()


def test_large_image_process_matches_original_fingerprint(view, tmp_path):
    from PyQt6 import QtCore
    path = tmp_path/'large.png'
    image = QtGui.QImage(2048, 1024, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('black'))
    painter = QtGui.QPainter(image)
    for n in range(20):
        painter.fillRect(n*100, (n%4)*200, 85, 750, QtGui.QColor((n*47)%255, (n*71)%255, (n*29)%255))
    painter.end()
    assert image.save(str(path))
    item = fileio._load_image_item(str(path))
    view.scene.addItem(item)
    source = ImageSource.from_item(item)
    service = DuplicateScanService()
    service.cache_directory = str(tmp_path/'process-cache')
    expected = service._fingerprint(source)
    source.release()
    service.scan([source])
    from prism.image_pyramid import source_key
    assert service.source_cache.get(source_key(*source.snapshot)) == expected
