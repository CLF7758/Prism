"""Single scene owner with bounded obsolete queue metadata and latest intent."""
from concurrent.futures import Future
import heapq
import itertools
import threading


class MaterialScheduler:
    def __init__(self):
        self._condition = threading.Condition(threading.RLock())
        self._sequence = itertools.count()
        self._heap = []
        self._tasks = {}
        self._pending = {}
        self._closed = False
        self._active = 0
        self._submitted = self._replaced = self._completed = 0
        self._thread = threading.Thread(target=self._run, name='material-project', daemon=True)
        self._thread.start()

    def submit(self, fn, *args, priority=20, replace_key=None):
        with self._condition:
            if self._closed:
                raise RuntimeError('Material scheduler is closed')
            future = Future()
            if replace_key is not None:
                old = self._pending.pop(replace_key, None)
                if old is not None:
                    task = self._tasks.pop(old, None)
                    if task is not None:
                        task[0].cancel()
                        self._replaced += 1
            sequence = next(self._sequence)
            if replace_key is not None:
                self._pending[replace_key] = sequence
            self._tasks[sequence] = (future, replace_key, fn, args)
            heapq.heappush(self._heap, (priority, sequence))
            # Obsolete heap entries retain no callbacks, snapshots or textures.
            # Compact even their small metadata so bursts cannot leave a tail.
            if len(self._heap) > 2 * len(self._tasks) + 32:
                self._heap = [entry for entry in self._heap if entry[1] in self._tasks]
                heapq.heapify(self._heap)
            self._submitted += 1
            self._condition.notify()
            return future

    def stats(self):
        with self._condition:
            return dict(submitted=self._submitted, replaced=self._replaced,
                        completed=self._completed, pending=len(self._tasks),
                        active=self._active, heap_entries=len(self._heap))

    def _run(self):
        while True:
            with self._condition:
                while not self._heap and not self._closed:
                    self._condition.wait()
                if not self._heap:
                    return
                _, sequence = heapq.heappop(self._heap)
                task = self._tasks.pop(sequence, None)
                if task is None:
                    continue
                future, key, fn, args = task
                if key is not None and self._pending.get(key) == sequence:
                    self._pending.pop(key, None)
                if not future.set_running_or_notify_cancel():
                    continue
                self._active += 1
            try:
                future.set_result(fn(*args))
            except BaseException as error:
                future.set_exception(error)
            finally:
                with self._condition:
                    self._active -= 1
                    self._completed += 1

    def shutdown(self, wait=True, cancel_futures=False):
        with self._condition:
            self._closed = True
            if cancel_futures:
                tasks = tuple(self._tasks.values())
                self._tasks.clear()
                self._pending.clear()
                self._heap.clear()
                for future, *_ in tasks:
                    future.cancel()
            self._condition.notify_all()
        if wait:
            self._thread.join()
