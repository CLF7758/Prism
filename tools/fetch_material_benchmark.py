"""Fetch 300 distinct CC0 PBR materials for an offline Prism benchmark.

Powered by Poly Haven (https://polyhaven.com). No live API in the product.
Resumable cache, four download workers, exact upstream bytes and checksums.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import time
from urllib.request import Request, urlopen


def fetch(url):
    for attempt in range(4):
        try:
            with urlopen(Request(url, headers={'User-Agent': 'PrismMaterialStressTest/1.0'}), timeout=45) as response:
                return response.read()
        except Exception:
            if attempt == 3:
                raise
            time.sleep(1 + attempt)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--count', type=int, default=300)
    parser.add_argument('--output', type=Path, default=Path('artifacts/material-tests/polyhaven-300'))
    args = parser.parse_args()
    root = args.output
    root.mkdir(parents=True, exist_ok=True)
    catalog = root / 'catalog.json'
    if not catalog.exists():
        catalog.write_bytes(fetch('https://api.polyhaven.com/assets?t=textures'))
    assets = json.loads(catalog.read_bytes())
    # Spread across the whole alphabet/category range rather than 300 copies.
    keys = sorted(assets)
    chosen = [keys[i * len(keys) // args.count] for i in range(args.count)]

    def download(key):
        directory = root / key
        directory.mkdir(exist_ok=True)
        metadata = directory / 'files.json'
        if not metadata.exists():
            metadata.write_bytes(fetch('https://api.polyhaven.com/files/' + key))
        files = json.loads(metadata.read_bytes())
        result = dict(id=key, name=assets[key]['name'], source='https://polyhaven.com/a/' + key,
                      license='CC0', tags=assets[key].get('categories', []), maps={})
        for slot, names in [('basecolor', ('Diffuse', 'diff', 'col', 'Color', 'col1', 'col_01', 'coll1')), ('normal', ('nor_dx', 'nor_gl')), ('roughness', ('Rough', 'rough'))]:
            info = next((files[n]['1k'][fmt] for n in names for fmt in ('jpg', 'png')
                         if files.get(n, {}).get('1k', {}).get(fmt)), None)
            if not info:
                raise RuntimeError(f'{key}: missing {slot} 1k texture')
            path = directory / Path(info['url']).name
            if not path.exists() or hashlib.md5(path.read_bytes()).hexdigest() != info['md5']:
                blob = fetch(info['url'])
                if hashlib.md5(blob).hexdigest() != info['md5']:
                    raise RuntimeError('Checksum mismatch: ' + info['url'])
                path.write_bytes(blob)
            result['maps'][slot] = dict(path=str(path.resolve()), url=info['url'], md5=info['md5'],
                                       convention='OpenGL' if 'nor_gl' in info['url'] else 'DirectX')
        return result

    complete, errors = [], []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(download, key): key for key in chosen}
        for future in as_completed(futures):
            try:
                complete.append(future.result())
            except Exception as error:
                errors.append(dict(id=futures[future], error=str(error)))
            report = dict(provider='Powered by Poly Haven', license='CC0', materials=sorted(complete, key=lambda a: a['id']), errors=errors)
            (root / 'manifest.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(f'{len(complete)} complete, {len(errors)} failed', flush=True)
    if len(complete) != args.count:
        raise SystemExit(f'Expected {args.count} distinct materials; got {len(complete)}: {errors}')


if __name__ == '__main__':
    main()
