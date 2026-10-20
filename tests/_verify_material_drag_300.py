"""Actual 300-material source handoff preparation; no Unreal process is mocked."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import time
from prism.external_drag import file_mime
from prism.fileio.sql import SQLiteIO
from prism.material_transfer import prepare_texture_transfer, _asset_key


def test_300_material_texture_handoff(main_window, qtbot):
    root = Path(__file__).resolve().parents[1]/'artifacts/material-tests/stress-300'
    scene = main_window.view.scene
    SQLiteIO(str(root/'PolyHaven-300.prism'),scene,readonly=True).read()
    main_window.view.category_panel._install_resource_service()
    service = main_window.view.category_panel.material_service
    records = service.list_records()
    assert len(records) == 300
    before = scene.attachments.loaded_count()
    def prepare():
        start = time.perf_counter()
        result = prepare_texture_transfer(records,scene.attachments,root/'drag-sources')
        duration = (time.perf_counter()-start)*1000
        expected = {file['sha256'] for record in records for file in record['files']}
        actual = {hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in result['paths']}
        assert actual == expected
        assert len({_asset_key(Path(path).name) for path in result['paths']}) == len(result['paths'])
        return result,duration
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(prepare)
        qtbot.waitUntil(future.done,timeout=120000)
        result,duration = future.result()
    mime = file_mime(result['paths'])
    assert len(mime.urls()) == len(result['paths'])
    assert all(url.isLocalFile() for url in mime.urls()) and not mime.hasImage()
    assert scene.attachments.loaded_count() == before
    report = dict(materials=300,original_texture_files=len(result['paths']),all_original_hashes_match=True,
                  prepare_ms=duration,cache=str(root/'drag-sources'),
                  persistent_source_paths=True,native_file_urls=True,unreal_receiver_tested=False)
    (root/'texture-drag-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
