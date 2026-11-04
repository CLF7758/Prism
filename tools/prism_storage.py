"""Explicit experimental maintenance entry. No GUI or automatic conversion."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('preflight', 'convert', 'export-legacy'))
    parser.add_argument('source')
    parser.add_argument('destination', nargs='?')
    parser.add_argument('--experimental', action='store_true')
    args = parser.parse_args()
    from prism.storage.contracts import StorageError
    from prism.storage.object_repository import ObjectError
    try:
        if args.operation == 'preflight':
            from prism.storage.migration_plan import preflight_source
            from dataclasses import asdict
            result = asdict(preflight_source(args.source))
        else:
            if args.destination is None:
                parser.error('destination is required')
            if args.operation == 'convert':
                from prism.storage.migration import convert_legacy
                result = convert_legacy(args.source, args.destination, experimental=args.experimental,
                    progress=lambda value: print(json.dumps(value, ensure_ascii=False), flush=True))
            else:
                from prism.storage.legacy_export import export_legacy
                result = {'exported': str(export_legacy(args.source, args.destination,
                                                        experimental=args.experimental))}
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0
    except (StorageError, ObjectError, OSError, ValueError) as error:
        print(json.dumps({'error': str(error), 'kind': getattr(getattr(error, 'kind', None),
                                                            'value', type(error).__name__)},
                         ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
