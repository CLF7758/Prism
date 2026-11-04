"""最近使用账本：左栏「最近使用」口径的唯一来源。

**为什么存在用户目录，而不是工程文件里**：工程文件是用户自己的资产，
应用不该因为多记一次使用就把用户的工程改写掉（工程还可能被 Git 或同步
盘盯着）。「最近使用」是应用自己的状态，而且跨工程才有意义，所以单独
落在 `%APPDATA%\\Prism\\recent-usage.json`。

**为什么不引入 Qt**：这个模块会被后台线程读写，界面那一侧只负责把动作
转成调用。保持零 Qt 依赖，任何线程都能用，测试也能无 GUI 跑。

磁盘上的形状::

    {"version": 1,
     "projects": {"<project_id>": {"<section>": {"<resource_id>": <entry>}}}}
    <entry> = {"name": "...", "ts": 1730000000.0}

`section` 是资源类别（'canvas' / 'mindmap' / 'document' / 'material' /
'model' / 'asset' …），`project_id` 是工程 id，两者都由调用方给 —— 这个
模块不认识 Prism 的资源模型，只负责按 id 记账。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import math
import copy
from time import monotonic
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def timestamp(value):
    """Normalize ledger seconds and saved asset ISO dates to UTC seconds."""
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            value = (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
        except ValueError:
            pass
    try:
        stamp = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return stamp if math.isfinite(stamp) and stamp > 0 else 0.0

#: 磁盘格式版本。结构变了就 +1；旧版本读不出来时按空账本处理。
VERSION = 1

#: 单个工程最多留多少条（跨分区合计）。
PROJECT_LIMIT = 100

#: 账本文件名。
FILENAME = 'recent-usage.json'

#: 文件名所在的子目录。
DIRECTORY_NAME = 'Prism'


def default_path() -> Path:
    """账本默认落盘位置。

    `%APPDATA%` 拿不到时退到 `~/.prism` —— 仍然是用户级目录，账本不该
    出现在工程目录里。
    """
    base = os.environ.get('APPDATA')
    if base:
        return Path(base) / DIRECTORY_NAME / FILENAME
    return Path.home() / '.prism' / FILENAME


class RecentUsage:
    """最近使用账本。

    内存读改写由同一把锁保护。异步磁盘检查在锁外执行，并在发布时核对
    修订，避免旧文件读取覆盖刚发生的本地更新。
    """

    def __init__(self, path=None, *, background_writes=False):
        self._lock = threading.RLock()
        self._path = Path(path) if path is not None else default_path()
        self._state = None
        # 磁盘快照 (mtime_ns, size)：用来发现别人改过这份账本。
        self._stamp = None
        # 已见过的最大时间戳：保证新条目严格比旧条目新。
        self._latest = 0.0
        self._background_writes = background_writes
        self._executor = None
        self._pending = None
        self._writing = False
        self._future = None
        self._refresh_future = None
        self._next_refresh = 0.
        self._revision = 0

    @property
    def path(self) -> Path:
        """账本文件路径。"""
        return self._path

    # -- 磁盘读写 ----------------------------------------------------

    @staticmethod
    def _empty_state():
        return {'version': VERSION, 'projects': {}}

    @staticmethod
    def _normalize(data):
        """把磁盘上的形状整理成内部形状。

        不认识的字段直接丢掉：这份文件是应用自己的状态，坏了一点不该
        让整个应用起不来。
        """
        state = RecentUsage._empty_state()
        if not isinstance(data, dict):
            return state
        projects = data.get('projects')
        if not isinstance(projects, dict):
            return state
        for project_id, sections in projects.items():
            if not isinstance(sections, dict):
                continue
            for section, entries in sections.items():
                if not isinstance(entries, dict):
                    continue
                for resource_id, entry in entries.items():
                    if not isinstance(entry, dict):
                        continue
                    try:
                        ts = float(entry.get('ts', 0.0))
                    except (TypeError, ValueError):
                        continue
                    name = entry.get('name')
                    state['projects'].setdefault(project_id, {}) \
                        .setdefault(section, {})[resource_id] = {
                            'name': name if isinstance(name, str) else '',
                            'ts': ts,
                        }
        for project in state['projects'].values():
            RecentUsage._trim(project)
        return state

    def _read_disk(self):
        """读盘。

        文件不存在或 JSON 坏了都当空账本：这份文件坏了最多让用户丢一次
        「最近使用」列表，不值得让调用方崩溃。
        """
        try:
            raw = self._path.read_text(encoding='utf-8')
        except (OSError, ValueError):
            return self._empty_state()
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning('最近使用账本损坏，按空账本处理: %s', self._path)
            return self._empty_state()
        return self._normalize(data)

    def _snapshot(self):
        """磁盘当前的 (mtime_ns, size)。"""
        try:
            stat = self._path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    @staticmethod
    def _max_ts(state):
        latest = 0.0
        for sections in state.get('projects', {}).values():
            for entries in sections.values():
                for entry in entries.values():
                    ts = entry.get('ts', 0.0)
                    if ts > latest:
                        latest = ts
        return latest

    def _state_locked(self):
        """取当前账本；磁盘被改过就重新读。

        同一进程里可能存在第二个实例（测试会这么干），账本也可能被别的
        程序改过，所以拿磁盘快照比对；没变就不重新解析 JSON。
        """
        if self._writing:
            return self._state
        if self._background_writes and self._state is not None:
            self._refresh_locked()
            return self._state
        stamp = self._snapshot()
        if self._state is None or stamp != self._stamp:
            self._state = self._read_disk()
            self._stamp = stamp
            self._latest = self._max_ts(self._state)
        return self._state

    @property
    def refresh_future(self):
        with self._lock:
            return self._refresh_future

    def refresh(self):
        """Explicit async external-file check; local reads remain immediate."""
        with self._lock:
            return self._refresh_locked(force=True)

    def _refresh_locked(self, *, force=False):
        if self._refresh_future is not None and not self._refresh_future.done():
            return self._refresh_future
        if not force and monotonic() < self._next_refresh:
            return self._refresh_future
        self._next_refresh = monotonic() + 1.
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='PrismRecentUsage')
        try:
            self._refresh_future = self._executor.submit(self._refresh_disk)
        except RuntimeError:
            pass  # Shutdown still permits reads of the last in-memory state.
        return self._refresh_future

    def _refresh_disk(self):
        with self._lock:
            if self._writing:
                return False
            previous, revision = self._stamp, self._revision
            loaded = self._state is not None
        stamp = self._snapshot()
        if loaded and stamp == previous:
            return False
        state = self._read_disk()
        with self._lock:
            if self._writing or self._revision != revision:
                return False  # A local edit superseded this disk read.
            self._state, self._stamp = state, stamp
            self._latest = self._max_ts(state)
            self._revision += 1
        return True

    def _write_disk(self, state):
        """原子写：先写临时文件，再 `os.replace` 覆盖。

        直接覆盖的话，写到一半被读走或进程崩了，文件就只剩半截 JSON，
        而半截 JSON 只能当空账本处理 —— 用户的最近列表就没了。
        """
        temporary = self._path.with_name(self._path.name + '.tmp')
        payload = json.dumps(state, ensure_ascii=False, indent=2)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(temporary, 'w', encoding='utf-8') as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
        except OSError:
            # 账本是辅助状态，写不进去不该拖垮应用。
            logger.exception('无法写入最近使用账本: %s', self._path)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        return True

    def _commit(self, state):
        """把整理好的账本落盘，并让内存副本跟上磁盘。"""
        self._state = state
        self._revision += 1
        if self._background_writes:
            # Publish in memory immediately; one writer keeps only the newest
            # pending snapshot. Disk flushes must not block selection or reads.
            self._pending = copy.deepcopy(state)
            if not self._writing:
                if self._executor is None:
                    self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='PrismRecentUsage')
                self._writing = True
                self._future = self._executor.submit(self._drain)
            return True
        written = self._write_disk(state)
        self._stamp = self._snapshot()
        return written

    def _drain(self):
        while True:
            with self._lock:
                state, self._pending = self._pending, None
                if state is None:
                    self._writing = False
                    return
            self._write_disk(state)
            stamp = self._snapshot()
            with self._lock:
                self._stamp = stamp

    def flush(self):
        """Wait for this ledger's queued writes, for shutdown and explicit export."""
        with self._lock:
            future = self._future
            refresh = self._refresh_future
        if refresh is not None:
            refresh.result()
        if future is not None:
            future.result()

    # -- 裁剪 --------------------------------------------------------

    @staticmethod
    def _trim(project):
        """跨所有资源类型保留最近的 100 项，不按天数到期。"""
        total = sum(len(entries) for entries in project.values())
        if total <= PROJECT_LIMIT:
            return
        ordered = []
        for section, entries in project.items():
            for resource_id, entry in entries.items():
                ordered.append((entry['ts'], section, resource_id, entry))
        ordered.sort(key=lambda item: item[0], reverse=True)
        trimmed = {}
        for _ts, section, resource_id, entry in ordered[:PROJECT_LIMIT]:
            trimmed.setdefault(section, {})[resource_id] = entry
        project.clear()
        project.update(trimmed)

    # -- 公开 API ----------------------------------------------------

    def record(self, project_id, section, resource_id, name=''):
        """记一次使用。

        同一个资源再记一次只更新时间戳，不新增条目：「最近使用」要的是
        排序而不是访问次数，而再次使用本来就该把它顶到更前面。`name`
        有值才更新 —— 空名字不该把已经记下的名字抹掉。
        """
        with self._lock:
            state = self._state_locked()
            projects = state['projects']
            project = projects.setdefault(project_id, {})
            entries = project.setdefault(section, {})
            now = time.time()
            if now <= self._latest:
                # Windows 上 time.time() 的分辨率有限，两次记录可能落在
                # 同一时刻；时间戳必须严格递增，否则「新的在前」排不出来。
                now = self._latest + 1e-6
            self._latest = now
            entry = entries.get(resource_id)
            if entry is None:
                entry = {'name': '', 'ts': now}
                entries[resource_id] = entry
            else:
                entry['ts'] = now
            if name:
                entry['name'] = name
            self._trim(project)
            self._commit(state)
            return {'section': section, 'resource_id': resource_id,
                    'name': entry['name'], 'ts': entry['ts']}

    def recent(self, project_id, limit=PROJECT_LIMIT, sections=None):
        """某工程最近用过的条目，新的在前。

        `sections` 给了就只看这些分区 —— 左栏可能按分区分段显示。
        """
        with self._lock:
            state = self._state_locked()
            project = state['projects'].get(project_id) or {}
            wanted = None if sections is None else set(sections)
            items = []
            for section, entries in project.items():
                if wanted is not None and section not in wanted:
                    continue
                for resource_id, entry in entries.items():
                    items.append({'section': section,
                                  'resource_id': resource_id,
                                  'name': entry['name'],
                                  'ts': entry['ts']})
            items.sort(key=lambda item: item['ts'], reverse=True)
            if limit is None:
                return items
            if limit <= 0:
                return []
            return items[:limit]

    def forget(self, project_id, resource_id):
        """忘掉一个资源（多半是它被删掉了）。

        跨分区找：调用方未必知道资源落在哪个分区。
        """
        with self._lock:
            state = self._state_locked()
            project = state['projects'].get(project_id)
            if not project:
                return False
            removed = False
            for section in list(project):
                if resource_id in project[section]:
                    del project[section][resource_id]
                    removed = True
                if not project[section]:
                    del project[section]
            if not removed:
                return False
            if not project:
                del state['projects'][project_id]
            self._commit(state)
            return True

    def clear(self, project_id):
        """清空某工程的整份账本。"""
        with self._lock:
            state = self._state_locked()
            if project_id not in state['projects']:
                return False
            del state['projects'][project_id]
            self._commit(state)
            return True


_LEDGER = None
_LEDGER_LOCK = threading.Lock()


def ledger() -> RecentUsage:
    """模块级单例。

    界面到处都要用最近使用，同一进程里存在两份账本就会各自持有半截
    状态，所以这里只给一份。
    """
    global _LEDGER
    with _LEDGER_LOCK:
        if _LEDGER is None:
            _LEDGER = RecentUsage(background_writes=True)
        return _LEDGER
