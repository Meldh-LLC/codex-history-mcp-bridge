"""Check the files selected for the source package; never read private Codex history.

This is a narrow packaging guardrail, not a complete credential/security scanner.
Diagnostics identify file/category, never the matched credential value.
"""
from __future__ import annotations
import argparse
import ast
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {
    'README.md', 'PROMPTS.md', 'SECURITY.md', 'PRIVACY.md', 'CONTRIBUTING.md', 'THIRD_PARTY.md',
    'LICENSE', 'CHANGELOG.md', 'PROTOCOL_NOTES.md',
    'TESTING.md', 'UPGRADING.md', 'requirements.txt',
    '.gitignore', '.gitattributes', '.editorconfig', 'codex_bridge.py',
    'rollout_fallback.py', 'session_resolver.py', 'verify_resolution.py', 'project_discovery.py', 'verify_discovery.py', 'server.py', 'doctor.py', 'audit_rollout.py',
    'verify_pagination.py', 'setup.ps1', 'configure-tunnel.ps1',
    'start-tunnel.ps1', 'load-runtime-key.ps1', 'run-doctor.ps1', 'run-server.cmd',
}
NESTED_FILES = {
    '.github/ISSUE_TEMPLATE/bug_report.md', '.github/ISSUE_TEMPLATE/config.yml',
    '.github/pull_request_template.md', '.github/release-notes/v0.3.0.md',
    '.github/workflows/ci.yml', '.github/workflows/release.yml',
    'docs/ARCHITECTURE.md', 'docs/COVERAGE.md',
    'docs/DISCOVERY.md', 'docs/OPERATIONS.md', 'docs/REPORT_FORMAT.md',
    'docs/RESOLUTION.md', 'docs/SETUP_WINDOWS.md', 'docs/SOURCES.md',
    'tests/mock_app_server.py', 'tests/mock_discovery_app_server.py',
    'tests/mock_resolver_app_server.py', 'tests/test_discovery.py',
    'tests/test_more_cases.py', 'tests/test_pagination.py',
    'tests/test_projection_025.py', 'tests/test_release_packaging.py',
    'tests/test_resolution.py', 'tests/test_rollout_fallback.py',
    'tests/test_transport.py', 'tools/build_release.py', 'tools/release_check.py',
}
PATTERNS = {
    'credential-like value': re.compile(r'\b(?:sk-|gh[pousr]_|github_pat_)[A-Za-z0-9_-]{24,}'),
    'private-key block': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'concrete tunnel id': re.compile(r'\btunnel_[0-9a-f]{24,}\b'),
    'concrete Windows user path': re.compile(r'[A-Za-z]:[\\/]+Users[\\/]+[A-Za-z0-9_.-]+[\\/]'),
    'concrete Unix user path': re.compile(r'/(?:Users|home)/[A-Za-z0-9_.-]+/'),
}
SESSION_UUID = re.compile(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b', re.I)
MIT_TERMS = """Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


def valid_mit_license(text: str) -> bool:
    """Require standard MIT terms and a concrete 2026 copyright holder."""
    normalized = text.replace('\r\n', '\n').rstrip() + '\n'
    first, separator, body = normalized.partition('\n\n')
    if not separator or not re.fullmatch(r'Copyright \(c\) 2026 [^\[\]\r\n]+', first):
        return False
    return body == MIT_TERMS

def source_files(root: Path = ROOT) -> list[Path]:
    """Return the explicit source-package allowlist without walking other data."""
    files: list[Path] = []
    for name in sorted(ROOT_FILES):
        p = root / name
        if p.is_symlink():
            raise ValueError(f'Symlink is not releasable: {name}')
        if p.is_file():
            files.append(p)
    for name in sorted(NESTED_FILES):
        p = root / name
        for parent in (root / Path(*Path(name).parts[:i]) for i in range(1, len(Path(name).parts))):
            if parent.is_symlink():
                raise ValueError(f'Symlink is not releasable: {parent.relative_to(root).as_posix()}')
        if p.is_symlink():
            raise ValueError(f'Symlink is not releasable: {name}')
        if p.is_file():
            files.append(p)
    return sorted(files, key=lambda p: p.relative_to(root).as_posix())


def package_bytes(path: Path) -> bytes:
    """Return platform-independent UTF-8 bytes for a selected source file."""
    text = path.read_text(encoding='utf-8')
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    if path.suffix.lower() == '.cmd':
        text = text.replace('\n', '\r\n')
    return text.encode('utf-8')

def version(root: Path = ROOT) -> str:
    tree = ast.parse((root / 'codex_bridge.py').read_text(encoding='utf-8'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'BRIDGE_VERSION' for t in node.targets):
            result = ast.literal_eval(node.value)
            if isinstance(result, str) and re.fullmatch(r'\d+\.\d+\.\d+(?:-rc\.\d+)?', result):
                return result
    raise ValueError('Missing or invalid bridge version')

def check(root: Path = ROOT) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    selected = source_files(root)
    required = {'README.md', 'PROMPTS.md', 'server.py', 'codex_bridge.py', 'rollout_fallback.py',
                'session_resolver.py', 'verify_resolution.py', 'project_discovery.py', 'verify_discovery.py', 'requirements.txt', 'SECURITY.md', 'TESTING.md', 'tools/build_release.py', '.github/workflows/ci.yml'}
    present = {p.relative_to(root).as_posix() for p in selected}
    for name in sorted(required - present):
        errors.append(f'Missing: {name}')
    for p in selected:
        name = p.relative_to(root).as_posix()
        if p.stat().st_size > 1024 * 1024:
            errors.append(f'Unexpectedly large source file: {name}')
            continue
        try:
            text = p.read_text(encoding='utf-8')
        except UnicodeError:
            errors.append(f'Non-UTF-8 source: {name}')
            continue
        for label, pattern in PATTERNS.items():
            if pattern.search(text):
                errors.append(f'{label}: {name}')
        if not name.startswith('tests/') and SESSION_UUID.search(text):
            errors.append(f'real-looking session/thread UUID outside synthetic tests: {name}')
        if p.suffix == '.py':
            try:
                ast.parse(text, filename=name)
            except SyntaxError:
                errors.append(f'Python syntax: {name}')
        if p.suffix == '.md':
            for target in re.findall(r'\]\(([^\s)]+)\)', text):
                if '://' in target or target.startswith(('#', 'mailto:')):
                    continue
                target = target.split('#', 1)[0]
                if target and not (p.parent / target).exists():
                    errors.append(f'Broken local link: {name} -> {target}')
    license_path = root / 'LICENSE'
    if not license_path.is_file():
        errors.append('Missing: LICENSE')
    elif not valid_mit_license(license_path.read_text(encoding='utf-8')):
        errors.append('LICENSE is not the standard MIT text with a concrete 2026 copyright holder.')
    warnings.append('Checks cover selected package files only; they do not certify every possible secret or runtime security property.')
    return errors, warnings

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        errors, warnings = check()
        print(json.dumps({'result': 'FAIL' if errors else 'PASS',
            'version': version(), 'selected_files': len(source_files()),
            'errors': errors, 'warnings': warnings}, indent=2))
        return 1 if errors else 0
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({'result':'FAIL', 'error_type':type(exc).__name__, 'error':str(exc)}), file=sys.stderr)
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
