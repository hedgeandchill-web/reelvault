"""Isolated video conversion entrypoint. Killed with its child FFmpeg processes on timeout."""
import json
import sys
from pathlib import Path
from app import _download, translate_error


def main():
    if len(sys.argv) != 3:
        raise SystemExit(2)
    url, folder_raw = sys.argv[1:]
    folder = Path(folder_raw)
    try:
        path = _download(url, folder)
        print(json.dumps({'ok': True, 'path': str(path)}), flush=True)
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': translate_error(exc)}), flush=True)
        raise SystemExit(1)

if __name__ == '__main__':
    main()
