"""Local processor readiness only; dialogue tool availability is not probed here."""
from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import sys
import tempfile

def check_environment(output_dir: Path, font_path: Path | None, text: str, font_index: int = 0) -> dict:
    errors = []
    dependencies = {}
    for module, label in [('PIL', 'Pillow'), ('fontTools', 'fontTools')]:
        try:
            imported = importlib.import_module(module)
            dependencies[label] = getattr(imported, '__version__', 'available')
        except ImportError:
            dependencies[label] = None
            errors.append(f'Missing dependency: {label}')
    writable = False
    try:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=output_dir) as handle:
            handle.write(b'sticker-doctor')
            handle.flush()
        writable = True
    except OSError as exc:
        errors.append(f'Output directory is not writable: {exc}')
    font_hash = None
    if all(dependencies.values()):
        try:
            from .render import validate_font
            font_hash = validate_font(font_path, text, font_index)
        except (ValueError, OSError) as exc:
            errors.append(str(exc))
    return {'ok': not errors, 'python': sys.version.split()[0], 'pillow': dependencies['Pillow'], 'dependencies': dependencies,
            'output_writable': writable, 'font_hash': font_hash,
            'generation_tool': 'not_checked', 'errors': errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--font', type=Path)
    parser.add_argument('--font-index', type=int, default=0)
    parser.add_argument('--text', default='')
    args = parser.parse_args()
    result = check_environment(args.out, args.font, args.text, args.font_index)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
