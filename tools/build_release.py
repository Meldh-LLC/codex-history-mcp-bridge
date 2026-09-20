"""Build a deterministic source ZIP from the explicit package allowlist."""
from __future__ import annotations
import argparse
import hashlib
from pathlib import Path
import sys
import zipfile
from release_check import ROOT, check, package_bytes, source_files, version


def build(root: Path, output: Path) -> tuple[Path, Path]:
    errors, warnings = check(root)
    if errors:
        raise ValueError('; '.join(errors))
    for warning in warnings:
        print('NOTE:', warning, file=sys.stderr)
    output.mkdir(parents=True, exist_ok=True)
    current = version(root)
    archive = output / f'codex_history_mcp_bridge_{current}.zip'
    entries = {p.relative_to(root).as_posix(): package_bytes(p) for p in source_files(root)}
    manifest = ''.join(f'{hashlib.sha256(data).hexdigest()}  {name}\n' for name, data in sorted(entries.items()))
    entries['MANIFEST.sha256'] = manifest.encode('utf-8')
    # Stored entries avoid zlib-version differences while remaining portable.
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_STORED) as z:
        for name, data in sorted(entries.items()):
            info = zipfile.ZipInfo('codex-history-bridge/' + name, date_time=(2026, 9, 16, 0, 0, 0))
            info.create_system = 3
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o100644 << 16
            z.writestr(info, data)
    checksum = archive.with_name(archive.name + '.sha256')
    checksum.write_text(f'{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n', encoding='utf-8')
    return archive, checksum


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    try:
        archive, checksum = build(ROOT, args.out)
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(archive)
    print(checksum)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
