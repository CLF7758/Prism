"""Resume the upstream OpenAI CLIP weights, verifying its published SHA256."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import os
from pathlib import Path
import re
import subprocess

DIGEST = '40d365715913c9da98579312b702a82c18be219cc2a73407c4526f58eba950af'
SIZE = 353976522
URL = f'https://openaipublic.azureedge.net/clip/models/{DIGEST}/ViT-B-32.pt'
CHUNK = 4*1024*1024


def main():
    directory = Path(os.environ.get('LOCALAPPDATA', Path.home()))/'Prism/components/visual-search/index/models'
    directory.mkdir(parents=True, exist_ok=True)
    target = directory/'ViT-B-32.pt'
    if target.is_file() and target.stat().st_size == SIZE and hashlib.file_digest(target.open('rb'), 'sha256').hexdigest() == DIGEST:
        print('CLIP model already verified', flush=True)
        return
    pieces = directory/'download-parts'
    pieces.mkdir(exist_ok=True)
    # A partial SDK/curl download can seed complete prefix chunks; the final
    # SHA check remains mandatory before the model becomes usable.
    partial = directory/'ViT-B-32.pt.partial'
    if partial.is_file():
        with partial.open('rb') as stream:
            for number in range(min(partial.stat().st_size//CHUNK, SIZE//CHUNK)):
                part = pieces/f'{number:04}.part'
                data = stream.read(CHUNK)
                if not part.exists():
                    part.write_bytes(data)

    def download(number):
        start, end = number*CHUNK, min(SIZE, (number+1)*CHUNK)-1
        part = pieces/f'{number:04}.part'
        if part.is_file() and part.stat().st_size == end-start+1:
            return number
        temporary, headers = pieces/f'{number:04}.tmp', pieces/f'{number:04}.headers'
        for _ in range(3):
            result = subprocess.run(['curl.exe', '--fail', '--location', '--silent', '--show-error',
                '--connect-timeout', '15', '--max-time', '120', '--range', f'{start}-{end}',
                '-D', str(headers), '-o', str(temporary), URL], capture_output=True)
            actual = headers.read_text(errors='replace') if headers.exists() else ''
            if result.returncode == 0 and temporary.is_file() and temporary.stat().st_size == end-start+1 and re.search(
                    rf'content-range:\s*bytes {start}-{end}/{SIZE}', actual, re.I):
                temporary.replace(part)
                return number
        raise OSError(f'CLIP weight chunk {number} failed; rerun to resume')

    count = (SIZE+CHUNK-1)//CHUNK
    with ThreadPoolExecutor(max_workers=4) as pool:
        for done, future in enumerate(as_completed([pool.submit(download, n) for n in range(count)]), 1):
            future.result()
            print(f'CLIP weights {done}/{count} chunks', flush=True)
    assembled = directory/'ViT-B-32.pt.verified'
    digest = hashlib.sha256()
    with assembled.open('wb') as output:
        for number in range(count):
            with (pieces/f'{number:04}.part').open('rb') as stream:
                while data := stream.read(1024*1024):
                    output.write(data)
                    digest.update(data)
    if digest.hexdigest() != DIGEST:
        raise OSError('CLIP checksum mismatch; refusing to install downloaded model')
    assembled.replace(target)
    # Only delete files in this specific, verified download staging directory.
    for part in pieces.iterdir():
        if part.is_file() and part.suffix in ('.part', '.headers', '.tmp'):
            part.unlink()
    partial.unlink(missing_ok=True)
    print('CLIP model SHA256 verified: '+DIGEST, flush=True)


if __name__ == '__main__':
    main()
