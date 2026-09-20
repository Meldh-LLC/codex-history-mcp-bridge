# Contributing

This is an early, Windows-first community bridge. Keep changes small and separate reader semantics, security filtering, transport, and documentation work. A green synthetic test suite does not establish compatibility with every live installation or account configuration.

## Before submitting a change

```powershell
python -m unittest discover -s tests -v
```

```powershell
python .\tools\release_check.py
```

The core suite uses the Python standard library and synthetic fixtures. MCP wrapper tests use a stub; CI also installs the declared SDK and separately checks real server import/registration. No live Codex installation, tunnel, or account key belongs in CI. Never use `pull_request_target` to execute untrusted code with secrets.

For a new record variant, add a minimal **synthetic** fixture with the actual structural names and fake values. Test both privacy defaults and opt-ins, long-field continuation, EOF counters, and unknown-neighbor rejection. Do not fix coverage by blindly marking a prefix or unknown payload as understood.

Preserve these invariants: no mutation/control tools; no raw unknown-object dump; no reasoning or binary payload escape; opt-in tool bodies and diffs; explicit source-restart accounting; unfinished content always has valid continuation or an explicit error; do not manufacture semantic completeness from EOF.

## Issue reports

Use the bug-report template. Share the bridge/Python/Codex/tunnel-client versions, OS, failing operation, safe error, and whether the issue occurs locally or only through ChatGPT. An inventory can help; inspect it for personal data before posting. Do not attach real rollouts, session IDs, usernames/paths, credentials, private tool output, or unreviewed logs.

Security-sensitive reports should follow [SECURITY.md](SECURITY.md). There is no guaranteed support schedule or SLA. Cross-platform reports should say exactly which test ran rather than marking an entire OS supported.

Contributions are accepted under the repository's [MIT License](LICENSE).
