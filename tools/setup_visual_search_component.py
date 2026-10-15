"""Install the optional local search runtime, without adding ML to Prism's UI."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import venv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index-url', default='https://pypi.org/simple')
    parser.add_argument('--skip-dependencies', action='store_true')
    parser.add_argument('--download-model', action='store_true')
    args = parser.parse_args()
    directory = Path(os.environ.get('LOCALAPPDATA', Path.home()))/'Prism/components/visual-search'
    directory.mkdir(parents=True, exist_ok=True)
    python = directory/'runtime/Scripts/python.exe'
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(directory/'runtime')
    if not args.skip_dependencies:
        subprocess.run([str(python), '-m', 'pip', 'install', '--index-url', args.index_url,
            'open_clip_torch==2.26.1', 'faiss-cpu==1.12.0', 'torch==2.5.1',
            'torchvision==0.20.1', 'numpy==1.26.4', 'timm==1.0.12', 'huggingface-hub==0.26.5',
            'opencv-python-headless==4.10.0.84'], check=True)
    freeze = subprocess.check_output([str(python), '-m', 'pip', 'freeze'], text=True)
    (directory/'requirements-installed.txt').write_text(freeze, encoding='utf-8')
    (directory/'component.json').write_text(json.dumps(dict(
        encoder='https://github.com/mlfoundations/open_clip', encoder_version='2.26.1',
        index='https://github.com/facebookresearch/faiss', index_version='1.12.0',
        model='ViT-B-32', pretrained='openai', protocol_version=1), indent=2), encoding='utf-8')
    print('Local visual search component prepared:', directory)
    if args.download_model:
        import sys
        subprocess.run([sys.executable, str(Path(__file__).with_name('download_clip_model.py'))], check=True)


if __name__ == '__main__':
    main()
