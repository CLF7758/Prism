"""Standalone OpenCLIP/FAISS adapter. No Qt, network API or custom ML model."""
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import traceback
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from prism.source_store import ArchiveReadSession

MODEL = 'ViT-B-32'
PRETRAINED = 'openai'
FINGERPRINT = 'openclip-2.26.1-vit-b32-openai-rgb-exif-v1'
MAX_ITEMS = 100000


def emit(event, **values):
    sys.stdout.write(json.dumps(dict(event=event, **values), ensure_ascii=False)+'\n')
    sys.stdout.flush()


def source_identity(source):
    """Conservative versioning also invalidates aliases after archive saves."""
    path = source.get('path') or (source.get('archive') or [None])[0]
    if not path:
        return None
    stat = os.stat(path)
    parts = (os.path.normcase(os.path.abspath(path)), stat.st_size, stat.st_mtime_ns,
             stat.st_ctime_ns, source.get('archive') if not source.get('path') else None)
    return hashlib.sha256(repr(parts).encode('utf-8')).hexdigest()


def source_bytes(source, reader=None):
    if source.get('path'):
        return Path(source['path']).read_bytes()
    archive = source.get('archive')
    if archive:
        path, identifier = archive[:2]
        if reader is not None:
            content = reader.read((path, identifier))
        else:
            with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True) as connection:
                row = connection.execute('SELECT data FROM sqlar WHERE item_id=?', (identifier,)).fetchone()
            content = row[0] if row else None
        if content:
            return content
        if len(archive) > 2:
            return Path(archive[2]).read_bytes()
    raise OSError('原图不可用')


class Engine:
    def __init__(self, directory):
        self.directory = Path(directory)/FINGERPRINT
        self.directory.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.directory/'vectors.sqlite', timeout=30)
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('CREATE TABLE IF NOT EXISTS vectors (digest TEXT PRIMARY KEY, vector BLOB NOT NULL, dimension INTEGER NOT NULL, touched REAL NOT NULL)')
        self.connection.execute('CREATE TABLE IF NOT EXISTS aliases (identity TEXT PRIMARY KEY, digest TEXT NOT NULL)')
        self.connection.execute('CREATE TABLE IF NOT EXISTS appearance_v2 (digest TEXT PRIMARY KEY, histogram BLOB NOT NULL, spatial BLOB NOT NULL)')
        self.connection.execute('DROP TABLE IF EXISTS appearance_v1')
        self.model = None

    def appearance(self, source):
        import numpy as np
        from prism.components.visual_appearance import describe
        identity = source_identity(source)
        row = self.connection.execute('SELECT digest FROM aliases WHERE identity=?', (identity,)).fetchone() if identity else None
        digest = row[0] if row else None
        cached = self.connection.execute('SELECT histogram,spatial FROM appearance_v2 WHERE digest=?', (digest,)).fetchone() if digest else None
        if cached and len(cached[0]) == 512*4 and len(cached[1]) == 192*4:
            vectors = tuple(np.frombuffer(part, dtype=np.float32).copy() for part in cached)
            if all(np.isfinite(vector).all() for vector in vectors):
                return vectors
        content = source_bytes(source, getattr(self, 'reader', None))
        digest = hashlib.sha256(content).hexdigest()
        features = describe(content)
        self.connection.execute('INSERT OR REPLACE INTO appearance_v2 VALUES (?,?,?)',
            (digest, features[0].tobytes(), features[1].tobytes()))
        return features

    def load(self):
        if self.model is not None:
            return
        emit('progress', phase='model_load', message='正在加载本地模型；首次使用需要下载开源权重')
        import torch
        import open_clip
        import faiss
        self.torch, self.faiss = torch, faiss
        torch.set_num_threads(min(4, os.cpu_count() or 1))
        faiss.omp_set_num_threads(2)
        self.device = 'cuda' if torch.cuda.is_available() else 'cpu'
        self.model, _, self.preprocess = open_clip.create_model_and_transforms(
            MODEL, pretrained=PRETRAINED, device=self.device,
            cache_dir=str(self.directory.parent/'models'))
        self.model.eval()
        emit('progress', phase='model_ready', device=self.device, message='本地模型已就绪')

    def feature(self, source):
        import numpy as np
        def cached_vector(row):
            if row is None or row[1] != 512 or len(row[0]) != 512*4:
                return None
            vector = np.frombuffer(row[0], dtype=np.float32).copy()
            return vector if np.isfinite(vector).all() and .99 <= np.linalg.norm(vector) <= 1.01 else None
        identity = source_identity(source)
        row = self.connection.execute('SELECT v.vector,v.dimension FROM aliases a JOIN vectors v '
            'ON a.digest=v.digest WHERE a.identity=?', (identity,)).fetchone() if identity else None
        vector = cached_vector(row)
        if vector is not None:
            return vector, True
        content = source_bytes(source, getattr(self, 'reader', None))
        digest = hashlib.sha256(content).hexdigest()
        row = self.connection.execute('SELECT vector,dimension FROM vectors WHERE digest=?', (digest,)).fetchone()
        vector = cached_vector(row)
        cached = vector is not None
        if vector is None:
            from PIL import Image, ImageOps
            with Image.open(io.BytesIO(content)) as image:
                image = ImageOps.exif_transpose(image)
                if image.mode in ('RGBA', 'LA') or 'transparency' in image.info:
                    foreground = image.convert('RGBA')
                    image = Image.new('RGBA', foreground.size, (128, 128, 128, 255))
                    image.alpha_composite(foreground)
                tensor = self.preprocess(image.convert('RGB')).unsqueeze(0).to(self.device)
            with self.torch.inference_mode():
                feature = self.model.encode_image(tensor)
                feature /= feature.norm(dim=-1, keepdim=True).clamp(min=1e-12)
            vector = feature.float().cpu().numpy()[0].astype(np.float32)
            self.connection.execute('INSERT OR REPLACE INTO vectors VALUES (?,?,?,?)',
                (digest, vector.tobytes(), int(vector.size), time.time()))
        if identity:
            self.connection.execute('INSERT OR REPLACE INTO aliases VALUES (?,?)', (identity, digest))
        return vector, cached

    def search(self, request):
        import numpy as np
        self.load()
        data = json.loads(Path(request['manifest']).read_text(encoding='utf-8'))
        assets = data['assets']
        if len(assets) > MAX_ITEMS:
            raise ValueError('单次检索最多支持 100000 张图片，请缩小范围')
        vectors, keys, failed, cached = [], [], 0, 0
        last_progress = 0
        for position, asset in enumerate(assets):
            try:
                vector, hit = self.feature(asset)
                vectors.append(vector)
                keys.append(asset['key'])
                cached += int(hit)
            except (OSError, ValueError, sqlite3.Error):
                failed += 1
            if (position+1) % 8 == 0:
                self.connection.commit()
            if time.monotonic()-last_progress >= .2:
                emit('progress', request_id=request['request_id'], phase='index',
                     done=position+1, total=len(assets), cached=cached, failed=failed)
                last_progress = time.monotonic()
        self.connection.commit()
        query, _ = self.feature(data['query'])
        self.connection.commit()
        results = []
        if vectors:
            matrix = np.stack(vectors).astype(np.float32)
            index = self.faiss.IndexFlatIP(matrix.shape[1])
            index.add(matrix)
            scores, indices = index.search(query.reshape(1, -1), min(600, len(keys)))
            from prism.components.visual_appearance import compare
            query_appearance = self.appearance(data['query'])
            sources = {asset['key']: asset for asset in assets}
            for count, (score, position) in enumerate(zip(scores[0], indices[0]), 1):
                if position < 0:
                    continue
                key = keys[int(position)]
                try:
                    details = compare(query_appearance, self.appearance(sources[key]), float(score))
                except (OSError, ValueError, sqlite3.Error):
                    continue
                results.append(dict(key=key, **details))
                if count % 8 == 0:
                    self.connection.commit()
                if time.monotonic()-last_progress >= .2:
                    emit('progress', request_id=request['request_id'], phase='appearance', done=count,
                         total=len(indices[0]), message='正在比较主色调和明暗分布')
                    last_progress = time.monotonic()
            results.sort(key=lambda row: row['score'], reverse=True)
            results = results[:200]
        # Cache bounds are independent of user project size or save frequency.
        self.connection.execute('DELETE FROM vectors WHERE digest NOT IN '
            '(SELECT digest FROM vectors ORDER BY touched DESC LIMIT ?)', (MAX_ITEMS,))
        self.connection.execute('DELETE FROM aliases WHERE digest NOT IN (SELECT digest FROM vectors)')
        self.connection.execute('DELETE FROM appearance_v2 WHERE digest NOT IN (SELECT digest FROM vectors)')
        self.connection.execute('DELETE FROM aliases WHERE rowid NOT IN '
            '(SELECT rowid FROM aliases ORDER BY rowid DESC LIMIT ?)', (MAX_ITEMS*2,))
        self.connection.commit()
        emit('results', request_id=request['request_id'], results=results,
             indexed=len(keys), total=len(assets), failed=failed, cached=cached,
             device=self.device, model=FINGERPRINT, ranking='clip+opencv-lab-v2')


def main():
    directory = sys.argv[1]
    engine = Engine(directory)
    emit('ready', protocol_version=1)
    for line in sys.stdin:
        request = None
        try:
            if len(line) > 65536:
                raise ValueError('Request too large')
            request = json.loads(line)
            if request.get('command') == 'shutdown':
                break
            engine.reader = ArchiveReadSession()
            try:
                engine.search(request)
            finally:
                engine.reader.close()
        except Exception as error:
            traceback.print_exc(file=sys.stderr)
            emit('failed', request_id=request.get('request_id') if isinstance(request, dict) else None, error=str(error))
    engine.connection.close()


if __name__ == '__main__':
    main()
