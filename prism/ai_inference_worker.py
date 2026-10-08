"""Private JSON-lines entry point for crash-isolated local inference."""
import base64
import io
import json
import sys


def main():
    if sys.stdin is None:
        sys.stdin = open(0, encoding='utf-8')
    if sys.stdout is None:
        sys.stdout = open(1, 'w', encoding='utf-8')
    import faulthandler
    from prism.config import logfile_name
    try:
        fault_log = open(logfile_name(), 'a', encoding='utf-8')
        faulthandler.enable(file=fault_log, all_threads=True)
    except OSError:
        pass
    from prism.wd14_tagger import get_tagger
    tagger = get_tagger()
    for line in sys.stdin:
        try:
            request = json.loads(line)
            source = (io.BytesIO(base64.b64decode(request['bytes']))
                      if 'bytes' in request else request['path'])
            if not tagger.load_model():
                raise RuntimeError('AI 模型加载失败，请查看 Prism.log。')
            tags = tagger.tag_image(source)
            response = {'tags': [(name, float(confidence))
                                 for name, confidence in tags]}
        except Exception as exc:
            response = {'error': str(exc)}
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
