"""Source-package checks use source text and temporary synthetic directories only."""
from __future__ import annotations
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_check_for_test', ROOT/'tools'/'release_check.py')
assert spec and spec.loader
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)


class ReleasePackagingTests(unittest.TestCase):
    def test_version(self):
        self.assertEqual(checks.version(ROOT), '0.3.0')

    def test_release_selection_excludes_working_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p/'README.md').write_text('example')
            (p/'auth.json').write_text('secret fixture')
            (p/'.venv').mkdir()
            (p/'.venv'/'local.txt').write_text('local')
            (p/'private-work').mkdir()
            (p/'private-work'/'plan.md').write_text('private planning')
            self.assertEqual([f.name for f in checks.source_files(p)], ['README.md'])

    def test_release_selection_excludes_internal_process_files(self):
        internal = [
            'LICENSE_DECISION.md', 'TEST_RESULTS.txt', 'docs/ACCEPTANCE.md',
            'docs/RELEASE_STATUS.md', 'docs/RELEASING.md',
            'docs/reader-baseline.json',
        ]
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            for name in internal:
                path = p/name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('internal process material')
            self.assertEqual(checks.source_files(p), [])

    def test_release_selection_does_not_sweep_unlisted_nested_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p/'docs').mkdir()
            (p/'docs'/'private-report.txt').write_text('must stay out')
            self.assertEqual(checks.source_files(p), [])

    def test_package_bytes_are_platform_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            markdown = p/'README.md'
            command = p/'run-server.cmd'
            markdown.write_bytes(b'first\r\nsecond\r\n')
            command.write_bytes(b'first\nsecond\n')
            self.assertEqual(checks.package_bytes(markdown), b'first\nsecond\n')
            self.assertEqual(checks.package_bytes(command), b'first\r\nsecond\r\n')

    def test_archive_avoids_version_dependent_deflate_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, str(ROOT/'tools'/'build_release.py'), '--out', tmp],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            archive = Path(tmp)/'codex_history_mcp_bridge_0.3.0.zip'
            with zipfile.ZipFile(archive) as package:
                self.assertTrue(package.infolist())
                self.assertTrue(all(item.compress_type == zipfile.ZIP_STORED for item in package.infolist()))
                self.assertTrue(all(item.create_system == 3 for item in package.infolist()))

    def test_packaging_checks_pass(self):
        errors, _ = checks.check(ROOT)
        self.assertEqual(errors, [])

    def test_license_validator_rejects_placeholder_or_altered_terms(self):
        valid = 'Copyright (c) 2026 Example Holder\n\n' + checks.MIT_TERMS
        self.assertTrue(checks.valid_mit_license(valid))
        self.assertFalse(checks.valid_mit_license(valid.replace('Example Holder', '[OWNER]')))
        self.assertFalse(checks.valid_mit_license(valid.replace('Permission is hereby granted', 'Permission may be granted')))

    def test_primary_prompt_is_identical_in_readme_and_prompts(self):
        text=(ROOT/'PROMPTS.md').read_text()
        start=text.index('Use Codex History Bridge to reconstruct')
        end=text.index('\n```',start)
        self.assertIn(text[start:end], (ROOT/'README.md').read_text())

    def test_connection_prompt_uses_registered_status_tool_name(self):
        prompt = (ROOT/'PROMPTS.md').read_text()
        server = (ROOT/'server.py').read_text()
        self.assertIn('codex_bridge_status', prompt)
        self.assertIn('async def codex_bridge_status(', server)
        self.assertNotIn('codex_history_status', prompt)

    def test_ci_has_no_secret_tokens_or_privileged_pr_trigger(self):
        text=(ROOT/'.github/workflows/ci.yml').read_text()
        self.assertNotIn('pull_request_target',text)
        self.assertNotIn('${{ secrets.',text)
        self.assertIn('contents: read',text)

    def test_key_loader_does_not_contain_literal_key_or_persistence(self):
        text=(ROOT/'load-runtime-key.ps1').read_text()
        self.assertIn('-AsSecureString',text)
        self.assertIn('ZeroFreeBSTR',text)
        self.assertNotIn('SetEnvironmentVariable',text)
        self.assertNotIn('Set-Content',text)

    def test_readme_claims_no_universal_semantic_completeness(self):
        text=(ROOT/'README.md').read_text()
        self.assertIn('complete projection is not complete knowledge',text)
        self.assertIn('requested history is sent to ChatGPT',text)

if __name__=='__main__':
    unittest.main()
