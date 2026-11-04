"""Fixtures for the independent multimaterial acceptance suite.

The suite has three kinds of material:

* an **expectation set** (``artifacts/model-multimaterial/d1``) built from raw
  file bytes by :mod:`material_reference`, never from the implementation;
* **checks** that compare the product against that set;
* **status records** for the gates that cannot be judged yet, so that "not
  tested" is a written fact with a reason instead of an empty space.

Nothing here writes into the user's project, settings or clipboard: the
journey harness works in a temporary project only.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ACCEPTANCE_DIR = Path(__file__).resolve().parent
REPO = ACCEPTANCE_DIR.parents[3]
ARTIFACTS_DIR = REPO / 'artifacts' / 'model-multimaterial'
D0_DIR = ARTIFACTS_DIR / 'd0'
D1_DIR = ARTIFACTS_DIR / 'd1'
D2_DIR = ARTIFACTS_DIR / 'd2'
D5_DIR = ARTIFACTS_DIR / 'd5'
D6_DIR = ARTIFACTS_DIR / 'd6'
FIXTURE_ROOT = D1_DIR / 'fixtures'
SAMPLES_DIR = REPO / 'tests' / 'model_library' / 'acceptance' / 'samples'

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


@pytest.fixture(scope='session')
def repo_root():
    return REPO


@pytest.fixture(scope='session')
def expectations():
    """Build the D1 expectation set once and hand back (payload, problems).

    Building inside the test session rather than trusting a checked-in JSON is
    deliberate: a stale expectation file is worse than none, because it looks
    like evidence.
    """
    from . import build_expectations
    payload, problems = build_expectations.build()
    return payload, problems


@pytest.fixture(scope='session')
def expectation_payload(expectations):
    return expectations[0]


@pytest.fixture(scope='session')
def reference_reader():
    from . import material_reference
    return material_reference


@pytest.fixture(scope='session')
def write_report():
    """Write a machine-readable result next to the report, not only in a chat."""

    def writer(directory, name, payload):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / name
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                          encoding='utf-8')
        return target

    return writer


@pytest.fixture()
def offscreen_qt(monkeypatch):
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    return os.environ['QT_QPA_PLATFORM']
