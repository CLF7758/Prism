"""D2: capture the baseline *before* judging the new feature.

Three things are recorded, because a number without them is not a measurement:

* the **source snapshot** -- a hash per file, so "which build was measured"
  has one answer even when the tree is not a git checkout;
* the **environment** -- OS build, CPU, RAM, GPU/driver, Python and Qt
  versions, the disk the samples live on, the session kind and DPI;
* the **raw output** of every existing-regression command, so a failure that
  already existed is neither blamed on the model library nor quietly dropped.

    .venv/Scripts/python.exe tests/model_library/acceptance/capture_baseline.py

Everything is written under ``artifacts/model-library-acceptance/``. Nothing
in this script touches the user's project, settings, clipboard or capture
service: each command runs in its own temporary directory.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
ARTIFACTS = os.path.join(REPO, 'artifacts', 'model-library-acceptance')
PYTHON = os.path.join(REPO, '.venv', 'Scripts', 'python.exe')

#: The existing regression entry points the model library must not break.
FRAMEWORK_COMMANDS = [
    ('framework-scaffold-and-cards',
     ['-m', 'pytest', '-q', 'tests/test_model_library_scaffold.py',
      'tests/test_material_cards.py']),
    ('framework-navigation',
     ['-m', 'pytest', '-q', 'tests/test_navigation_commit.py',
      'tests/test_material_navigation_async.py']),
    ('framework-save-and-storage',
     ['-m', 'pytest', '-q', 'tests/test_storage_entrypoint.py',
      'tests/test_managed_storage_wiring.py', 'tests/test_save_lifecycle.py']),
    ('framework-board-snapshot',
     ['-m', 'pytest', '-q', 'tests/test_board_save_snapshot.py']),
    ('framework-partial-project',
     ['-m', 'pytest', '-q', 'tests/test_partial_project.py']),
]

#: Source files whose exact bytes define the measured build.
SOURCE_ROOTS = ['prism']
SOURCE_SUFFIXES = ('.py', '.js', '.json', '.html', '.css')


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def source_snapshot():
    """One hash per source file, plus one hash over the whole set."""
    entries = []
    for root in SOURCE_ROOTS:
        base = os.path.join(REPO, root)
        for directory, subdirectories, names in os.walk(base):
            subdirectories[:] = sorted(d for d in subdirectories
                                       if d not in ('__pycache__', 'vendor'))
            for name in sorted(names):
                if not name.endswith(SOURCE_SUFFIXES):
                    continue
                path = os.path.join(directory, name)
                entries.append({'path': os.path.relpath(path, REPO).replace('\\', '/'),
                                'bytes': os.path.getsize(path),
                                'sha256': sha256_file(path)})
    combined = hashlib.sha256()
    for entry in entries:
        combined.update(entry['path'].encode('utf-8'))
        combined.update(entry['sha256'].encode('ascii'))
    return {'file_count': len(entries), 'combined_sha256': combined.hexdigest(),
            'files': entries}


def _powershell(script):
    """Run one PowerShell snippet; return its text or None."""
    for executable in ('powershell', 'pwsh'):
        if shutil.which(executable) is None:
            continue
        try:
            finished = subprocess.run(
                [executable, '-NoProfile', '-NonInteractive', '-Command', script],
                capture_output=True, text=True, timeout=120)
        except Exception:
            continue
        if finished.returncode == 0:
            return finished.stdout.strip()
    return None


def environment():
    info = {
        'captured_utc': utc_now(),
        'os': platform.platform(),
        'os_version': platform.version(),
        'machine': platform.machine(),
        'processor': os.environ.get('PROCESSOR_IDENTIFIER') or platform.processor(),
        'python': sys.version.replace('\n', ' '),
        'python_executable': sys.executable,
        'pid': os.getpid(),
        'cwd': os.getcwd(),
        'cpu_count': os.cpu_count(),
        'dpi_scale': os.environ.get('QT_SCALE_FACTOR') or os.environ.get('QT_SCREEN_SCALE_FACTORS'),
        'session': os.environ.get('SESSIONNAME'),
    }
    try:
        import PyQt6.QtCore as QtCore
        info['qt'] = QtCore.QT_VERSION_STR
        info['pyqt'] = QtCore.PYQT_VERSION_STR
    except Exception as error:
        info['qt'] = 'unavailable: %s' % error
    try:
        import pytest
        info['pytest'] = pytest.__version__
    except Exception:
        info['pytest'] = 'unknown'
    info['cpu'] = _powershell(
        '(Get-CimInstance Win32_Processor | '
        'Select-Object -First 1 -Property Name,NumberOfCores,NumberOfLogicalProcessors,'
        'MaxClockSpeed | Format-List | Out-String).Trim()')
    info['memory'] = _powershell(
        '[string]::Format("{0:N0} MiB", '
        '(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1MB)')
    info['gpu'] = _powershell(
        '(Get-CimInstance Win32_VideoController | '
        'Select-Object -Property Name,DriverVersion,AdapterRAM,CurrentHorizontalResolution,'
        'CurrentVerticalResolution,CurrentRefreshRate | Format-List | Out-String).Trim()')
    info['nvidia_smi'] = None
    if shutil.which('nvidia-smi'):
        try:
            finished = subprocess.run(
                ['nvidia-smi', '--query-gpu=name,driver_version,memory.total',
                 '--format=csv,noheader'], capture_output=True, text=True, timeout=60)
            info['nvidia_smi'] = finished.stdout.strip()
        except Exception as error:
            info['nvidia_smi'] = 'failed: %s' % error
    info['power_plan'] = _powershell('powercfg /getactivescheme')
    return info


def disk_state():
    """Where the samples and the cache live, and how fast that disk is."""
    state = {'volumes': []}
    for label, path in (('repo', REPO), ('samples', os.path.join(HERE, 'samples')),
                        ('cache', os.path.join(REPO, 'cache'))):
        if not os.path.exists(path):
            state['volumes'].append({'label': label, 'path': path, 'state': 'missing'})
            continue
        usage = shutil.disk_usage(path)
        drive = os.path.splitdrive(os.path.abspath(path))[0] or '?'
        state['volumes'].append({
            'label': label, 'path': path, 'drive': drive,
            'total_bytes': usage.total, 'free_bytes': usage.free,
            'used_bytes': usage.used,
        })
    state['drive_type'] = _powershell(
        'Get-PhysicalDisk | Select-Object DeviceId,MediaType,BusType,Size '
        '| Format-Table -AutoSize | Out-String')
    state['cache_entries'] = (sum(len(names) for _b, _d, names in os.walk(os.path.join(REPO, 'cache')))
                              if os.path.isdir(os.path.join(REPO, 'cache')) else 0)
    return state


def run(name, arguments, timeout=1800):
    """Run one command in its own directory and keep the raw output."""
    started = time.perf_counter()
    environment_variables = dict(os.environ)
    environment_variables['PYTHONHASHSEED'] = '0'
    # Every command runs in its own pytest temporary directory; a scratch
    # directory here would only collect empty folders.
    try:
        finished = subprocess.run([PYTHON] + arguments, cwd=REPO,
                                  capture_output=True, text=True, timeout=timeout,
                                  env=environment_variables)
        output = finished.stdout + finished.stderr
        code = finished.returncode
    except subprocess.TimeoutExpired as expired:
        output = (expired.stdout or '') + '\n[TIMEOUT after %ss]' % timeout
        code = 'timeout'
    duration = time.perf_counter() - started
    log_path = os.path.join(ARTIFACTS, 'logs', 'baseline-%s.txt' % name)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, 'w', encoding='utf-8') as stream:
        stream.write('# command: %s %s\n' % (PYTHON, ' '.join(arguments)))
        stream.write('# cwd: %s\n\n' % REPO)
        stream.write(output)
    return {'name': name, 'command': [PYTHON] + arguments, 'exit': code,
            'duration_s': round(duration, 3), 'log': os.path.relpath(log_path, REPO),
            'last_line': output.strip().splitlines()[-1] if output.strip() else ''}


def main():
    os.makedirs(ARTIFACTS, exist_ok=True)
    payload = {
        'schema': 'model_library_baseline/1',
        'captured_utc': utc_now(),
        'note': ('This is a baseline, not an acceptance result. A command listed '
                 'as failing here was failing before the model library was judged, '
                 'and stays attributed to its own work line.'),
        'environment': environment(),
        'disk': disk_state(),
        'source': source_snapshot(),
        'commands': [],
    }
    for name, arguments in FRAMEWORK_COMMANDS:
        print('running', name, flush=True)
        payload['commands'].append(run(name, arguments))
    target = os.path.join(ARTIFACTS, 'baseline.json')
    with open(target, 'w', encoding='utf-8') as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
    print('written:', target)
    for command in payload['commands']:
        print('%-32s exit=%-8s %6.1fs  %s' % (command['name'], command['exit'],
                                              command['duration_s'],
                                              command['last_line'][:90]))
    return 0


if __name__ == '__main__':
    sys.exit(main())
