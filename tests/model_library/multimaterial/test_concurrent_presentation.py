"""Cover and edit preparation share immutable cache files on Windows."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time

import pytest

from prism.model_library.multimaterial.presentation import _publish_cache_file, material_artifact
from prism.model_library.multimaterial.contracts import MaterialCatalog, MaterialState, MaterialTarget


def test_concurrent_artifact_preparation_reads_complete_json(tmp_path):
    target=MaterialTarget('project',1,'model',1,1,1)
    catalog=MaterialCatalog(1,1,'digest','test',(),())
    state=MaterialState(target,(),())
    item={'path':tmp_path/'source.fbx','catalog':catalog,'paths':{}}
    start=threading.Barrier(12)
    def prepare(_):
        start.wait(timeout=10)
        path=material_artifact(item,state,tmp_path,'assimp.dll',{})
        return path,json.loads(path.read_text(encoding='utf-8'))
    with ThreadPoolExecutor(max_workers=12) as pool:
        results=list(pool.map(prepare,range(12)))
    assert len({p for p,_ in results})==1
    assert all(v['target']['model_id']=='model' for _,v in results)
    assert not list(tmp_path.glob('*.tmp'))


def test_failed_cache_write_does_not_publish_partial_or_leave_staging(tmp_path):
    destination=tmp_path/'derived.png'
    def broken(staged):
        staged.write_bytes(b'partial')
        raise OSError('interrupted encoder')
    with pytest.raises(OSError):_publish_cache_file(destination,broken)
    assert not destination.exists()
    assert not list(tmp_path.iterdir())


def test_concurrent_publish_never_overwrites_a_reader_or_exposes_partial_bytes(tmp_path):
    destination=tmp_path/'render.json'
    started=threading.Event()
    def slow(staged):
        staged.write_bytes(b'{')
        started.set()
        time.sleep(.05)
        staged.write_bytes(b'{"complete":true}')
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(_publish_cache_file,destination,slow)
        assert started.wait(timeout=5)
        assert not destination.exists()
        second=pool.submit(_publish_cache_file,destination,lambda p:p.write_bytes(b'wrong'))
        first.result(timeout=5);second.result(timeout=5)
    with destination.open('rb') as held_reader:
        _publish_cache_file(destination,lambda p:p.write_bytes(b'wrong'))
        assert held_reader.read()==b'{"complete":true}'
    assert not list(tmp_path.glob('*.tmp'))
