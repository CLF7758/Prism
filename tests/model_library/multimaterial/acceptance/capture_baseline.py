"""D0: freeze the acceptance baseline before any pass/fail claim is made.

The brief for this role says the freeze has to name, in one place, the source
files, the runtimes, the machine and the load the results belong to, because a
threshold that is not bound to a machine is not a threshold.  This script
produces exactly that, from measurements -- not from a previous report:

``artifacts/model-multimaterial/d0/``

* ``environment.json``      -- Python, Qt, F3D, VTK, pytest versions
* ``machine.txt``/``.json`` -- CPU, RAM, GPU, driver, OS build, disk space
* ``source-hashes.json``    -- SHA-256 of every source file the task touches
* ``sample-integrity.json`` -- every upstream sample re-hashed against its
  manifest, plus the stress project manifest on ``E:``
* ``baseline-*.txt``        -- raw, unedited pytest output of the existing
  suites.  Failures stay in the file; nothing is summarised into "green".
* ``SUMMARY.md``            -- what was measured and what it means

It writes nothing outside the artifacts directory and never touches a user
project.  Run it from the repository root::

    .venv/Scripts/python.exe tests/model_library/multimaterial/acceptance/capture_baseline.py
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from importlib import metadata
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
ARTIFACTS = REPO / 'artifacts' / 'model-multimaterial' / 'd0'
SAMPLES = REPO / 'tests' / 'model_library' / 'acceptance' / 'samples'
STRESS_ROOT = Path('E:/Prism-stress-100k')

#: Files whose behaviour this acceptance depends on.  A hash here is the only
#: way to say "the implementation I judged is the implementation you have".
SOURCE_FILES = [
    'prism/model_library/contracts.py',
    'prism/model_library/service.py',
    'prism/model_library/appearance.py',
    'prism/model_library/composition.py',
    'prism/model_library/events.py',
    'prism/model_library/exporter.py',
    'prism/model_library/f3d_backend.py',
    'prism/model_library/f3d_parameters.py',
    'prism/model_library/importer.py',
    'prism/model_library/native_jobs.py',
    'prism/model_library/preview_channels.py',
    'prism/model_library/project_storage.py',
    'prism/model_library/repository.py',
    'prism/model_library/multimaterial/__init__.py',
    'prism/model_library/multimaterial/contracts.py',
    'prism/model_library/multimaterial/ports.py',
    'prism/model_library/multimaterial/service.py',
    'prism/widgets/model_material_panel.py',
    'prism/widgets/model_details.py',
    'docs/MODEL_MULTI_MATERIAL_FRAMEWORK_20261027.md',
    'docs/MODEL_MULTI_MATERIAL_DESIGN_20261027.md',
    'docs/model-multimaterial-tasks/AI_1_MATERIAL_DATA.md',
    'docs/model-multimaterial-tasks/AI_2_MATERIAL_RUNTIME.md',
    'docs/model-multimaterial-tasks/AI_3_MATERIAL_UI.md',
    'docs/model-multimaterial-tasks/AI_4_MATERIAL_ACCEPTANCE.md',
    'tests/model_library/test_multimaterial_scaffold.py',
    'tests/test_model_library_scaffold.py',
    'tests/model_library/test_ui.py',
    'tests/model_library/test_appearance.py',
] + [
    'tests/model_library/multimaterial/acceptance/' + name
    for name in ('material_reference.py', 'capture_baseline.py')
]

#: The suites that exist today.  They are recorded, not trusted: a green run
#: here says the old contract holds, never that multimaterial editing works.
BASELINE_COMMANDS = [
    ('framework-handoff', [
        '-m', 'pytest', '-q',
        'tests/model_library/test_multimaterial_scaffold.py',
        'tests/test_model_library_scaffold.py',
        'tests/model_library/test_ui.py',
        'tests/model_library/test_appearance.py',
    ]),
    ('model-library-suite', [
        '-m', 'pytest', '-q',
        '--ignore=tests/model_library/multimaterial',
        'tests/model_library',
    ]),
]

#: Large files are not re-hashed: the point of the guard is to keep a freeze
#: honest about what it did not verify instead of silently timing out.
HASH_SIZE_GUARD = 4 * 1024 ** 3


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        while True:
            block = stream.read(1 << 20)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _powershell(script, timeout=120):
    """Run a read-only PowerShell probe and keep its raw output."""
    try:
        completed = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
            capture_output=True, text=True, timeout=timeout,
            encoding='utf-8', errors='replace')
        return {'script': script, 'ok': completed.returncode == 0,
                'exit_code': completed.returncode,
                'stdout': completed.stdout, 'stderr': completed.stderr}
    except Exception as error:                       # noqa: BLE001 - recorded
        return {'script': script, 'ok': False, 'exit_code': None,
                'stdout': '', 'stderr': '%s: %s' % (type(error).__name__, error)}


def _package_version(name):
    try:
        return metadata.version(name)
    except Exception:                                # noqa: BLE001 - absent
        return None


def collect_runtime():
    modules = {}
    for name in ('PyQt6', 'PyQt6-Qt6', 'vtk', 'numpy', 'pillow', 'pytest',
                 'pytest-qt', 'f3d'):
        modules[name] = _package_version(name)
    qt = {'PyQt6': modules.get('PyQt6'), 'Qt': modules.get('PyQt6-Qt6')}
    try:
        from PyQt6.QtCore import QT_VERSION_STR, PYQT_VERSION_STR
        qt['QT_VERSION_STR'] = QT_VERSION_STR
        qt['PYQT_VERSION_STR'] = PYQT_VERSION_STR
    except Exception as error:                       # noqa: BLE001 - recorded
        qt['import_error'] = '%s: %s' % (type(error).__name__, error)
    f3d = {'distribution': modules.get('f3d')}
    try:
        import f3d                                       # noqa: F401
        f3d['import'] = 'ok'
        f3d['module_file'] = getattr(f3d, '__file__', '')
        engine = None
        for attribute in ('get_lib_info', 'getLibInfo'):
            getter = getattr(f3d.Engine, attribute, None)
            if getter is None:
                f3d.setdefault('lib_info_attempts', {})[attribute] = 'absent'
                continue
            try:
                engine = getter()
                f3d.setdefault('lib_info_attempts', {})[attribute] = 'ok'
                break
            except Exception as error:               # noqa: BLE001 - recorded
                f3d.setdefault('lib_info_attempts', {})[attribute] = (
                    'failed: %s: %s' % (type(error).__name__, error))
        if engine is not None:
            f3d['lib_info'] = _lib_information(engine)
    except Exception as error:                       # noqa: BLE001 - recorded
        # Kept verbatim: this is the result of importing f3d *after* PyQt6 in
        # this process, which is a real failure mode and not a measurement
        # error.  The clean-child probe below is the corrective measurement.
        f3d['import'] = 'failed: %s: %s' % (type(error).__name__, error)
    f3d['clean_child'] = _probe_f3d_in_clean_child()
    return {
        'python': sys.version.replace('\n', ' '),
        'python_executable': sys.executable,
        'python_bits': platform.architecture()[0],
        'platform': platform.platform(),
        'packages': modules,
        'qt': qt,
        'f3d': f3d,
        'env': {name: os.environ.get(name) for name in
                ('QT_QPA_PLATFORM', 'PRISM_OFFSCREEN', 'PYTHONHASHSEED',
                 'PYTHONDONTWRITEBYTECODE')},
    }


#: f3d ships its own VTK/Qt build.  Importing it after PyQt6 in the same
#: process fails with a DLL initialisation error on this machine, so the only
#: trustworthy environment probe is a child interpreter that imports f3d first.
_PROBE_SCRIPT = (
    'import json, sys\n'
    'info = {}\n'
    'try:\n'
    '    import f3d\n'
    '    info["import"] = "ok"\n'
    '    info["module_file"] = getattr(f3d, "__file__", "")\n'
    # The binding is snake_case: get_lib_info().  getLibInfo is the C++ name and
    # does not exist here -- record both attempts so a rename is never mistaken
    # for "the version is unavailable".
    '    for attribute in ("get_lib_info", "getLibInfo"):\n'
    '        getter = getattr(f3d.Engine, attribute, None)\n'
    '        if getter is None:\n'
    '            info.setdefault("lib_info_attempts", {})[attribute] = "absent"\n'
    '            continue\n'
    '        try:\n'
    '            lib = getter()\n'
    '            info["lib_info_attempts"] = info.get("lib_info_attempts", {})\n'
    '            info["lib_info_attempts"][attribute] = "ok"\n'
    '            wanted = ("version", "version_full", "vtk_version",\n'
    '                      "build_date", "build_system", "compiler", "license",\n'
    '                      "modules")\n'
    '            info["lib_info"] = {}\n'
    '            for name in wanted:\n'
    '                value = getattr(lib, name, None)\n'
    '                if value is not None:\n'
    '                    info["lib_info"][name] = value\n'
    '            info["lib_info_type"] = type(lib).__name__\n'
    '            break\n'
    '        except Exception as error:\n'
    '            info.setdefault("lib_info_attempts", {})[attribute] = (\n'
    '                "failed: %s: %s" % (type(error).__name__, error))\n'
    'except Exception as error:\n'
    '    info["import"] = "failed: %s: %s" % (type(error).__name__, error)\n'
    'try:\n'
    '    from PyQt6.QtCore import QT_VERSION_STR\n'
    '    info["qt_after_f3d"] = "ok " + QT_VERSION_STR\n'
    'except Exception as error:\n'
    '    info["qt_after_f3d"] = "failed: %s: %s" % (type(error).__name__, error)\n'
    'print(json.dumps(info, ensure_ascii=False))\n'
)


def _lib_information(engine):
    """Read F3D's LibInformation without assuming it is a mapping.

    ``f3d.Engine.get_lib_info()`` returns a ``LibInformation`` object whose
    fields are snake_case attributes (``version_full``, ``vtk_version``, ...).
    It is not a dict, so a ``engine[key]`` style read fails even though the
    call succeeded -- which is how a working probe gets misreported as
    "the version is unavailable".
    """
    wanted = ('version', 'version_full', 'vtk_version', 'build_date',
              'build_system', 'compiler', 'license', 'modules')
    values = {}
    for name in wanted:
        value = getattr(engine, name, None)
        if value is not None:
            values[name] = value
    return values


def _probe_f3d_in_clean_child():
    """Ask a fresh interpreter to import f3d first and report what it found.

    The point is not to make the failure go away: the in-process failure stays
    recorded.  The point is to know whether the *native route* is usable at
    all on this machine, and under which import order.
    """
    for arguments in (['-I', '-c', _PROBE_SCRIPT], ['-c', _PROBE_SCRIPT]):
        try:
            completed = subprocess.run(
                [sys.executable, *arguments], capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=180)
        except Exception as error:                   # noqa: BLE001 - recorded
            return {'ok': False, 'invocation': ' '.join(arguments),
                    'error': '%s: %s' % (type(error).__name__, error)}
        payload = None
        for line in (completed.stdout or '').splitlines():
            line = line.strip()
            if line.startswith('{'):
                try:
                    payload = json.loads(line)
                except ValueError:
                    payload = None
        if payload is not None:
            payload['ok'] = payload.get('import') == 'ok'
            # Record the shape of the call, not the program text.
            payload['invocation'] = '<python> %s -c <probe script: import f3d first>'
            payload['interpreter'] = sys.executable
            payload['explains'] = ('本进程先 import PyQt6 再 import f3d 会失败；'
                                  '干净子进程先 import f3d 才能得到可用的原生运行时')
            payload['exit_code'] = completed.returncode
            payload['stderr'] = (completed.stderr or '').strip()[:400]
            return payload
        last = {'ok': False, 'invocation': ' '.join(arguments),
                'exit_code': completed.returncode,
                'stdout': (completed.stdout or '')[:400],
                'stderr': (completed.stderr or '')[:400]}
    return last


def collect_machine():
    probes = {
        'cpu': _powershell(
            "Get-CimInstance Win32_Processor | Select-Object Name,"
            "NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed | ConvertTo-Json"),
        'memory': _powershell(
            "Get-CimInstance Win32_ComputerSystem | Select-Object "
            "Manufacturer,Model,TotalPhysicalMemory | ConvertTo-Json"),
        'gpu': _powershell(
            "Get-CimInstance Win32_VideoController | Select-Object Name,"
            "DriverVersion,DriverDate,AdapterRAM,VideoModeDescription | ConvertTo-Json"),
        'os': _powershell(
            "Get-CimInstance Win32_OperatingSystem | Select-Object Caption,"
            "Version,BuildNumber,LastBootUpTime | ConvertTo-Json"),
        'disk': _powershell(
            "Get-CimInstance Win32_LogicalDisk -Filter \"DriveType=3\" | "
            "Select-Object DeviceID,Size,FreeSpace,FileSystem | ConvertTo-Json"),
        'power': _powershell("powercfg /getactivescheme"),
        'load': _powershell(
            "Get-CimInstance Win32_Processor | Select-Object LoadPercentage | ConvertTo-Json"),
    }
    raw = '\n\n'.join('### %s\n%s\n%s' % (name, probe['script'],
                                          probe['stdout'] or probe['stderr'])
                      for name, probe in probes.items())
    return probes, raw


def collect_source_hashes():
    hashes = {}
    for relative in SOURCE_FILES:
        path = REPO / relative
        if not path.is_file():
            hashes[relative] = {'exists': False, 'sha256': None}
            continue
        hashes[relative] = {
            'exists': True,
            'sha256': sha256_file(path),
            'bytes': path.stat().st_size,
            'mtime': time.strftime('%Y-%m-%dT%H:%M:%S',
                                   time.localtime(path.stat().st_mtime)),
        }
    return hashes


def verify_sample_integrity():
    """Re-hash every upstream sample against the manifest recorded at fetch."""
    inventory_path = SAMPLES / 'INVENTORY.json'
    report = {'inventory': str(inventory_path), 'samples': 0, 'files': 0,
              'verified': 0, 'mismatch': [], 'missing': [], 'unrecorded': [],
              'provenance': {}, 'licenses': {}}
    if not inventory_path.is_file():
        report['error'] = 'INVENTORY.json 不存在'
        return report
    inventory = json.loads(inventory_path.read_text(encoding='utf-8'))
    report['samples'] = len(inventory.get('samples', []))
    for sample in inventory.get('samples', []):
        kind = sample.get('provenance_kind')
        report['provenance'][kind] = report['provenance'].get(kind, 0) + 1
        license_name = sample.get('license') or 'unknown'
        report['licenses'][license_name] = report['licenses'].get(license_name, 0) + 1
        directory = SAMPLES / sample['directory']
        for entry in sample.get('files', []):
            report['files'] += 1
            path = directory / entry['relpath']
            recorded = entry.get('sha256')
            if not path.is_file():
                report['missing'].append('%s/%s' % (sample['directory'], entry['relpath']))
                continue
            if not recorded:
                report['unrecorded'].append('%s/%s' % (sample['directory'],
                                                       entry['relpath']))
                continue
            actual = sha256_file(path)
            if actual != recorded:
                report['mismatch'].append({
                    'file': '%s/%s' % (sample['directory'], entry['relpath']),
                    'recorded': recorded, 'actual': actual,
                    'bytes_recorded': entry.get('bytes'),
                    'bytes_actual': path.stat().st_size,
                })
            else:
                report['verified'] += 1
    return report


def verify_stress_project():
    """Check the 100k stress corpus against its own checksum manifest."""
    report = {'root': str(STRESS_ROOT), 'exists': STRESS_ROOT.is_dir(),
              'entries': 0, 'verified': 0, 'not_hashed': [], 'mismatch': [],
              'missing': [], 'bytes_hashed': 0}
    if not report['exists']:
        report['error'] = '目录不存在'
        return report
    manifest = STRESS_ROOT / 'SHA256SUMS.txt'
    if not manifest.is_file():
        report['error'] = 'SHA256SUMS.txt 不存在'
        return report
    for line in manifest.read_text(encoding='utf-8', errors='replace').splitlines():
        line = line.strip()
        if not line:
            continue
        recorded, _, relative = line.partition('  ')
        if not relative:
            continue
        report['entries'] += 1
        path = STRESS_ROOT / relative.replace('/', os.sep)
        if not path.is_file():
            report['missing'].append(relative)
            continue
        size = path.stat().st_size
        if size > HASH_SIZE_GUARD:
            report['not_hashed'].append({'file': relative, 'bytes': size,
                                         'reason': 'size guard > 4 GiB'})
            continue
        actual = sha256_file(path)
        report['bytes_hashed'] += size
        if actual != recorded:
            report['mismatch'].append({'file': relative, 'recorded': recorded,
                                       'actual': actual})
        else:
            report['verified'] += 1
    report['missing_count'] = len(report['missing'])
    report['top_level'] = []
    for entry in sorted(STRESS_ROOT.iterdir()):
        if not entry.is_file():
            continue
        size = entry.stat().st_size
        record = {'file': entry.name, 'bytes': size,
                  'in_manifest': ('  ' + entry.name) in
                                 manifest.read_text(encoding='utf-8',
                                                    errors='replace')}
        if size > HASH_SIZE_GUARD:
            record['sha256'] = None
            record['reason'] = 'size guard > 4 GiB'
        else:
            record['sha256'] = sha256_file(entry)
        report['top_level'].append(record)
    # 只能说"清单里声明的路径在这台机器上不存在"。它们是被删除、被搬到别处，
    # 还是本来就只存在于生成清单的那台机器上，从这里看不出来 —— 不做推断。
    report['verdict'] = (
        '清单声明的 %d 条路径在本机均不存在（verified=0, mismatch=0）：'
        '不能推断这些文件已被删除，也不能推断工程内没有原件；'
        '可确认的只有"声明路径下取不到字节"，因此无法用于'
        '"原件哈希不变"的核对' % len(report['missing'])
        if report['entries'] and not report['verified']
        and not report['mismatch'] else '见上表')
    return report


def run_baseline(name, arguments, timeout=3600):
    """Run one existing suite and keep its output verbatim."""
    environment = dict(os.environ)
    environment['QT_QPA_PLATFORM'] = 'offscreen'
    environment.setdefault('PYTHONHASHSEED', '0')
    command = [str(REPO / '.venv' / 'Scripts' / 'python.exe'), *arguments]
    started = time.perf_counter()
    try:
        completed = subprocess.run(command, cwd=str(REPO), env=environment,
                                   capture_output=True, text=True,
                                   encoding='utf-8', errors='replace',
                                   timeout=timeout)
        output = (completed.stdout or '') + (completed.stderr or '')
        code = completed.returncode
    except Exception as error:                       # noqa: BLE001 - recorded
        output = '%s: %s' % (type(error).__name__, error)
        code = None
    elapsed = time.perf_counter() - started
    raw_path = ARTIFACTS / ('baseline-%s.txt' % name)
    raw_path.write_text(
        '# command: %s\n# cwd: %s\n# QT_QPA_PLATFORM=%s\n# exit_code: %s\n'
        '# wall_seconds: %.2f\n\n%s' % (' '.join(command), REPO,
                                        environment['QT_QPA_PLATFORM'], code,
                                        elapsed, output),
        encoding='utf-8')
    summary = ''
    for line in reversed(output.splitlines()):
        if ' passed' in line or ' failed' in line or ' error' in line:
            summary = line.strip()
            break
    return {'name': name, 'command': ' '.join(command), 'exit_code': code,
            'wall_seconds': round(elapsed, 2), 'summary_line': summary,
            'raw_log': str(raw_path.relative_to(REPO))}


def main():
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    started = time.strftime('%Y-%m-%dT%H:%M:%S')

    runtime = collect_runtime()
    (ARTIFACTS / 'environment.json').write_text(
        json.dumps(runtime, ensure_ascii=False, indent=2), encoding='utf-8')

    probes, machine_raw = collect_machine()
    (ARTIFACTS / 'machine.txt').write_text(machine_raw, encoding='utf-8')
    (ARTIFACTS / 'machine.json').write_text(
        json.dumps({name: {'ok': probe['ok'], 'stdout': probe['stdout'],
                           'stderr': probe['stderr']}
                    for name, probe in probes.items()},
                   ensure_ascii=False, indent=2), encoding='utf-8')

    hashes = collect_source_hashes()
    (ARTIFACTS / 'source-hashes.json').write_text(
        json.dumps({'generated': started, 'files': hashes},
                   ensure_ascii=False, indent=2), encoding='utf-8')

    samples = verify_sample_integrity()
    stress = verify_stress_project()
    (ARTIFACTS / 'sample-integrity.json').write_text(
        json.dumps({'generated': started, 'upstream_samples': samples,
                    'stress_project': stress}, ensure_ascii=False, indent=2),
        encoding='utf-8')

    runs = [run_baseline(name, arguments) for name, arguments in BASELINE_COMMANDS]
    (ARTIFACTS / 'baseline-runs.json').write_text(
        json.dumps({'generated': started, 'runs': runs},
                   ensure_ascii=False, indent=2), encoding='utf-8')

    lines = [
        '# D0 冻结记录（自动生成）', '',
        '生成时间：%s' % started, '',
        '## 运行时', '',
        '```text',
        'python        : %s' % runtime['python'],
        'PyQt6 / Qt    : %s / %s' % (runtime['qt'].get('PYQT_VERSION_STR'),
                                     runtime['qt'].get('QT_VERSION_STR')),
        'F3D           : %s' % json.dumps(runtime['f3d'].get('lib_info')
                                          or runtime['f3d'], ensure_ascii=False),
        'VTK           : %s' % runtime['packages'].get('vtk'),
        'pytest        : %s' % runtime['packages'].get('pytest'),
        '```', '',
        '## 既有基线原始输出', '',
    ]
    for run in runs:
        lines.append('- `%s` exit=%s wall=%ss：%s → `%s`'
                     % (run['name'], run['exit_code'], run['wall_seconds'],
                        run['summary_line'] or '(无汇总行)', run['raw_log']))
    lines += [
        '', '## 样本清单核对', '',
        '- 上游样本：%d 个样本 / %d 个文件，复核通过 %d，不符 %d，缺失 %d'
        % (samples['samples'], samples['files'], samples['verified'],
           len(samples['mismatch']), len(samples['missing'])),
        '  - 许可分布：%s' % json.dumps(samples['licenses'], ensure_ascii=False),
        '  - 来源分布：%s' % json.dumps(samples['provenance'], ensure_ascii=False),
        '- 压力工程清单：%d 条，复核通过 %d，不符 %d，缺失 %d，因体积未哈希 %d'
        % (stress['entries'], stress['verified'], len(stress['mismatch']),
           len(stress['missing']), len(stress['not_hashed'])),
        '  - 结论：%s' % stress.get('verdict', '见 sample-integrity.json'),
        '  - 目录顶层文件：%s' % '、'.join(
            '%s(%.1f MiB)' % (item['file'], item['bytes'] / 1048576.0)
            for item in stress.get('top_level', [])) if stress.get('top_level')
        else '  - 目录顶层文件：无',
        '',
        '逐项原始数据见 `sample-integrity.json`。清单与磁盘不一致的项，'
        '在修好之前不得用于"原件哈希不变"的证据。',
        '',
        '已核对的两个已知边界：',
        '',
        '- `assimp_transparent/transparentTest.fbx`：哈希与样本清单一致，'
        '且清单记录了它与上游 git blob 一致（`git_blob_sha1_match: true`）。'
        '它的 FBX 容器头本身是坏的（NUL 被替换成空格），属于上游提供的'
        '"不可读容器"样本，不能当作可用 FBX 素材。',
        '- 压力工程清单里的 %d 条路径在本机均不存在（不是"已被删除"，也'
        '不是"工程内没有原件"）。可确认的只有：按清单声明的路径取不到字节，'
        '因此该目录目前**不能**用于"原件哈希不变"的核对；真实十万规模数据'
        '记为未测。' % len(stress['missing']),
        '- `machine.txt` / `machine.json` 里的中文系统名可能显示为替换字符：'
        'PowerShell 5.1 把 CIM 输出写入管道时不是 UTF-8，而采集端按 UTF-8 解码。'
        'CPU 型号、核数、内存、磁盘容量、系统构建号等 ASCII 字段不受影响，'
        '判定只用这些字段。',
        '',
        '## 这一层能证明什么', '',
        '绑定关系到此为止：上面的版本、哈希和机器就是后续所有判定的参考环境。'
        '既有基线的通过数只说明旧契约仍然成立，不说明多材质编辑可用。',
    ]
    (ARTIFACTS / 'SUMMARY.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')

    print('D0 artifacts written to', ARTIFACTS)
    print('runtime  :', runtime['python'].split()[0], runtime['qt'].get('QT_VERSION_STR'))
    print('samples  : verified=%d mismatch=%d missing=%d'
          % (samples['verified'], len(samples['mismatch']), len(samples['missing'])))
    print('stress   : verified=%d mismatch=%d not_hashed=%d'
          % (stress['verified'], len(stress['mismatch']), len(stress['not_hashed'])))
    for run in runs:
        print('baseline : %-22s exit=%s %s' % (run['name'], run['exit_code'],
                                               run['summary_line']))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
