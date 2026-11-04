"""Automatic cover fill follows the same task owner as real cover rendering."""
from concurrent.futures import Future
from types import SimpleNamespace

from prism.model_library.contracts import ModelCapabilities
from prism.model_library.service import ModelLibraryService
from prism.model_library.multimaterial.application import ProjectMaterialService


def test_fill_does_not_duplicate_pending_or_retry_unchanged_failure():
    owner = ModelLibraryService.__new__(ModelLibraryService)
    owner._caps=ModelCapabilities(preview=True)
    owner._closed=False;owner._readonly=False;owner._futures={}
    material = ProjectMaterialService.__new__(ProjectMaterialService)
    material._covers={'m':Future()};material._failed_covers={};material._cover_pending=set()
    material.owner=owner;material.target_for=lambda record:record
    current=SimpleNamespace(revision=1)
    owner.get=lambda model_id:current
    owner.multimaterial=material
    requested=[];owner.render_cover=requested.append
    cards=[SimpleNamespace(model_id='m',thumbnail_data_url='',thumbnail_state='missing')]
    owner.ensure_covers(cards);owner.ensure_covers(cards)
    assert requested==[]
    material._covers['m'].set_result(None)
    material._failed_covers['m']=current
    owner.ensure_covers(cards)
    assert requested==[]
    current=SimpleNamespace(revision=2)
    owner.ensure_covers(cards)
    assert requested==['m']
    assert 'm' not in material._failed_covers
