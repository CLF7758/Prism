"""Verify a diagnostics ZIP, then reuse the existing offline trace analyzer."""
import argparse
import hashlib
import importlib.util
import json
import re
from pathlib import Path
import sys
import tempfile
import zipfile


def integrity_summary(reports, io, context, manifest, warnings):
    """Separate missing evidence, recorded versions and current disk bytes."""
    disk = context.get('disk-source-manifest.json', {})
    runtime = context.get('runtime.json', {})
    drift = []
    sessions = []
    for report in reports:
        scope = report.get('session_scope', {})
        contract = report.get('recording_contract', {})
        sessions.append(dict(session=report.get('session'),
            start_observed=scope.get('has_session_start'), end_observed=scope.get('has_session_end'),
            completeness=report.get('completeness'),
            recording_contract_version=contract.get('recording_contract_version'),
            schema_version_mismatch=contract.get('schema_version_mismatch')))
        snapshots = list(contract.get('version_snapshots') or [])
        startup = runtime.get('startup_version_snapshot')
        if runtime.get('session') == report.get('session') and isinstance(startup, dict):
            snapshots.append(startup)
        by_file = {}
        unstable_recorded = set()
        for snapshot in snapshots:
            files = snapshot.get('files')
            if not isinstance(files, dict):
                continue
            for name, row in files.items():
                digest = row.get('sha256') if isinstance(row, dict) else None
                if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-fA-F]{32}|[0-9a-fA-F]{64}', digest):
                    continue
                name = str(name).replace('\\', '/')
                if name.startswith('prism/'):
                    name = name[6:]
                if row.get('changed_during_hash') or snapshot.get('version_snapshot_inconsistent'):
                    unstable_recorded.add(name)
                by_file.setdefault(name, set()).add(digest.lower())
        for name, digests in sorted(by_file.items()):
            current = disk.get(name, {})
            digest = current.get('sha256')
            reliable = (isinstance(digest, str) and len(digest) == 64
                        and not current.get('changed_during_read') and name not in unstable_recorded)
            matches = all(digest.lower().startswith(value) for value in digests) if reliable else None
            drift.append(dict(session=report.get('session'), file=name,
                recorded_digests=sorted(digests), current_sha256=digest,
                recorded_snapshot_unstable=name in unstable_recorded,
                recorded_versions_disagree=any(not a.startswith(b) and not b.startswith(a)
                    for a in digests for b in digests),
                current_disk_matches_recorded_prefix=matches,
                state=('unknown_current_disk' if matches is None else
                       'match_at_recorded_precision' if matches else 'recorded_source_differs_from_disk')))
    chains = io.get('rotation_chains', [])
    return dict(sessions=sessions, rotation_chains=chains,
        missing_rotation_segments=[chain for chain in chains
            if chain.get('missing_indices') or chain.get('missing_base')],
        sequence_or_input_gaps=io.get('input_evidence_gaps', []),
        omitted_files=manifest.get('omissions', []), truncated_or_mutating_files=warnings,
        source_comparison=drift,
        source_comparison_state='available_at_recorded_precision' if drift else 'unknown_no_comparable_recorded_versions',
        analyzer_warnings=io.get('warnings', []),
        loaded_source_bytes='unknown; module paths and recorded prefixes do not prove in-memory bytes',
        coverage='retained evidence only; missing end may be an active session, not a crash',
        unavailable_source_comparison='no snapshots or no comparable selected source files is unknown, not unchanged')


def analyze_bundle(source):
    source = Path(source)
    if source.stat().st_size > 64*1024*1024:
        raise ValueError('Diagnostics ZIP exceeds size limit')
    with zipfile.ZipFile(source) as archive, tempfile.TemporaryDirectory(prefix='prism-diagnostics-') as folder:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)) or len(names) > 128:
            raise ValueError('Duplicate or excessive diagnostics entries')
        if sum(info.file_size for info in infos) > 64*1024*1024:
            raise ValueError('Diagnostics package exceeds the uncompressed size limit')
        if any(info.file_size > 16*1024*1024 for info in infos):
            raise ValueError('Diagnostics entry exceeds size limit')
        for name in names:
            parts = name.split('/')
            if ('\\' in name or ':' in name or name.startswith('/')
                    or any(part in ('', '.', '..') for part in parts)):
                raise ValueError('Unsafe diagnostics entry')
        manifest = json.loads(archive.read('manifest.json'))
        if not isinstance(manifest, dict) or manifest.get('schema_version') != 1:
            raise ValueError('Unsupported diagnostics schema')
        rows = manifest.get('files', [])
        if not isinstance(rows, list) or any(not isinstance(row, dict)
                or not isinstance(row.get('path'), str) or not isinstance(row.get('bytes'), int)
                or not isinstance(row.get('sha256'), str) for row in rows):
            raise ValueError('Invalid diagnostics manifest file records')
        evidence_names = [row['path'] for row in rows]
        if len(evidence_names) != len(set(evidence_names)) or set(names) != set(evidence_names) | {'manifest.json'}:
            raise ValueError('Diagnostics manifest does not exactly cover the evidence')
        inputs, warnings, context = [], [], {}
        for row in rows:
            raw = archive.read(row['path'])
            if len(raw) != row['bytes'] or hashlib.sha256(raw).hexdigest() != row['sha256']:
                raise ValueError('Diagnostics evidence hash mismatch: ' + row['path'])
            if row['path'] in ('runtime.json', 'project-stat.json', 'summary.json', 'disk-source-manifest.json'):
                snapshot = json.loads(raw)
                if not isinstance(snapshot, dict):
                    raise ValueError('Invalid diagnostics snapshot: ' + row['path'])
                context[row['path']] = snapshot
            if row.get('truncated') or row.get('changed_during_read'):
                warnings.append({'path': row['path'], 'truncated': row.get('truncated', False),
                                 'changed_during_read': row.get('changed_during_read', False)})
            # No extractall: write only manifest-verified operation logs to a
            # private flat directory, never restore arbitrary archive paths.
            name = Path(row['path']).name
            if re.fullmatch(r'logs/(?:Prism-operations-\d+\.jsonl(?:\.\d+)?|Prism-startup-\d+\.jsonl)', row['path']):
                target = Path(folder)/name
                target.write_bytes(raw)
                inputs.append(str(target))
        spec = importlib.util.spec_from_file_location('prism_diagnostic_trace_analyzer',
            Path(__file__).with_name('analyze_prism_trace.py'))
        analyzer = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = analyzer
        spec.loader.exec_module(analyzer)
        reports, io = analyzer.analyse(inputs) if inputs else ([], {'reason': 'no_operation_logs'})
        return dict(reports=reports, io=io, diagnostic_bundle=dict(
            sha256=hashlib.sha256(source.read_bytes()).hexdigest(), manifest_verified=True,
            analyzer_version='1.1.0',
            omissions=manifest.get('omissions', []), evidence_warnings=warnings,
            context=context,
            integrity_summary=integrity_summary(reports, io, context, manifest, warnings),
            native_evidence='unknown; manifest verification does not provide a native stack',
            runtime_disk_equality='not_proven', operation_log_files=len(inputs)))


def main(argv=None):
    parser = argparse.ArgumentParser(description='校验 Prism 诊断 ZIP，再复用追踪分析器；不启动应用或读取工程原件。')
    parser.add_argument('bundle')
    parser.add_argument('--json', required=True, dest='output')
    args = parser.parse_args(argv)
    if Path(args.bundle).resolve() == Path(args.output).resolve():
        parser.error('拒绝覆盖输入诊断包')
    try:
        report = analyze_bundle(args.bundle)
    except (ValueError, OSError, KeyError, zipfile.BadZipFile) as error:
        parser.exit(2, '诊断包校验失败：' + str(error) + '\n')
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
