# Upgrade to 0.3.0

Keep the working installation as the rollback copy. No dependency, credential, tunnel profile, or live-history migration is required. The archive has a `codex-history-bridge` subfolder; run commands in the folder containing the Python files.

## 1. Extract and compile

Extract into a separate staging folder. Do not run `setup.ps1` or `configure-tunnel.ps1` for an existing installation.

```powershell
python -m py_compile .\codex_bridge.py .\rollout_fallback.py .\session_resolver.py .\project_discovery.py .\server.py .\verify_discovery.py
```

Resolve any compilation error before continuing.

## 2. Validate locally

Synthetic tests require only the standard library:

```powershell
python -m unittest discover -s tests -v
```

Read-only local search with a known term/positive root (replace these examples):

```powershell
python .\verify_discovery.py search --query "PROJECT_TERM" --expect-root "E:\Projects\KnownRoot" --max-calls 1000
```

The search is in one Python process. It does not print message text or tokens; it does read allowed local history. Expect `PASS_SEARCH_FINISHED` only if the observed inventory and selected message scans finished and the expected match occurred. `PARTIAL_SEARCH`/`FAIL_EXPECTED_MATCH_NOT_FOUND`/`PARTIAL_MAX_CALLS` need review. A returned cursor exhaustion is not enough by itself. Version 0.3.0 keeps one opaque `cbh1_` token unchanged for the active chain, refreshes its 72-hour idle lifetime only after successful continuations, reports non-secret idle/absolute lifecycle diagnostics, and retains a 30-day absolute cap. Unknown native item variants still prevent a complete visible-message scan.

Metadata-only scope catalog (one or several exact roots; repeat `--root`):

```powershell
python .\verify_discovery.py catalog --name "Example project" --root "E:\Projects\PartA" --root "E:\Projects\PartB"
```

Confirm it keeps separate sessions within the same root and handles archives and root failures explicitly. A test using one root is not evidence of multi-root behavior.

## 3. Install the validated files

Stop the old tunnel with Ctrl+C, but keep that PowerShell window open to retain any process-only runtime key. Back up the working source files.

Copy these nine files from the extracted source into the known working bridge folder:

```text
codex_bridge.py
rollout_fallback.py
session_resolver.py
project_discovery.py
server.py
verify_pagination.py
verify_resolution.py
verify_discovery.py
audit_rollout.py
```

`project_discovery.py` is a new REQUIRED runtime module. `verify_discovery.py` is the new local diagnostic. The other verifiers are optional operational helpers but should be kept in sync. Leave `.venv`, the tunnel executable, credentials, PowerShell helpers and tunnel configuration untouched.

From the working folder:

```powershell
.\.venv\Scripts\python.exe -m py_compile .\codex_bridge.py .\rollout_fallback.py .\session_resolver.py .\project_discovery.py .\server.py .\verify_discovery.py
```

```powershell
.\.venv\Scripts\python.exe -c "import codex_bridge,project_discovery; print(codex_bridge.BRIDGE_VERSION); print(project_discovery.__file__)"
```

Expect `0.3.0` and the intended installed path.

## 4. Restart and refresh

Restart the existing tunnel in its original shell:

```powershell
.\tunnel-client.exe run --profile codex-history
```

Refresh/rescan the custom MCP connection. It must expose `codex_search_visible_history`, `codex_create_project_scope`, and `codex_catalog_project_scope`. Use a fresh chat and new references. Do not reuse any pre-0.3.0 search/session/page handles after the process restart.

Use `PROMPTS.md` first for scope discovery or a metadata-only confirmed-root catalog. Read full histories only after the root set is decided. Finish each selected session independently and use document-first output. Search excerpts are not full-session reads and must not stand in for report evidence.

## Rollback

Stop the tunnel, restore the backed-up working runtime files, and restart the same profile. Refresh its tools. Extra unused new files can stay in the folder; the earlier runtime will ignore them. Start new chains after rollback; never combine pages across versions or processes. Nothing needs changing in Codex history or account settings.
