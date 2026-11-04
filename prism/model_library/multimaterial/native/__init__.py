"""Native-side building blocks owned by AI 2.

``bindings`` holds the source-object <-> render-object mapping rules. It imports no
renderer so it can be unit tested in the ordinary test run, and so the same rules
serve the VTK preview process and any future F3D port.

Identity (node path / mesh key / primitive key / source instance identity) is kept
apart from matching evidence (geometry and content fingerprints); see
``MATCH_RULES_VERSION`` for the rule set an evidence file was produced with.
"""
from .bindings import (MATCH_RULES_VERSION, AssignmentTransfer, BindingMap,
                       CarryResult, MappingContractError, MatchEvidence,
                       MatchFailure, NativeObject, NativeSlot, Unmatched,
                       build_slots, carry_over, fingerprint_content,
                       fingerprint_geometry, fingerprint_transform, reconcile)

__all__ = [
    'MATCH_RULES_VERSION', 'AssignmentTransfer', 'BindingMap', 'CarryResult',
    'MappingContractError', 'MatchEvidence', 'MatchFailure', 'NativeObject',
    'NativeSlot', 'Unmatched', 'build_slots', 'carry_over', 'fingerprint_content',
    'fingerprint_geometry', 'fingerprint_transform', 'reconcile',
]
