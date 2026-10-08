"""Run the native ONNX runtime outside the graphics application's process."""
import base64
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time


class InferenceProcess:
    def __init__(self, canceled=lambda: False):
        self.canceled = canceled
        self.responses = queue.Queue()
        environment = dict(os.environ, PYTHONIOENCODING='utf-8')
        root = str(Path(__file__).resolve().parent.parent)
        environment['PYTHONPATH'] = root + os.pathsep + environment.get('PYTHONPATH', '')
        if getattr(sys, 'frozen', False):
            command = [sys.executable, '--ai-inference-worker']
        else:
            command = [sys.executable, '-m', 'prism.ai_inference_worker']
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
            env=environment,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for line in self.process.stdout:
                self.responses.put(json.loads(line))
        except (OSError, ValueError):
            pass
        finally:
            self.responses.put({'error': 'AI 推理进程异常退出，请查看模型和运行库版本。'})

    def load_model(self):
        return True  # The subprocess loads it on the first request.

    def tag_image(self, source):
        if hasattr(source, 'read'):
            request = {'bytes': base64.b64encode(source.read()).decode('ascii')}
        else:
            request = {'path': source}
        try:
            self.process.stdin.write(json.dumps(request) + '\n')
            self.process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise RuntimeError('AI 推理进程已退出。') from exc
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if self.canceled():
                self.close()
                return []
            try:
                response = self.responses.get(timeout=0.2)
            except queue.Empty:
                continue
            if response.get('error'):
                raise RuntimeError(response['error'])
            return response.get('tags', [])
        self.close()
        raise TimeoutError('AI 推理超时（120 秒），任务已停止。')

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.reader.join(timeout=1)
        self.process.stdin.close()
        self.process.stdout.close()
