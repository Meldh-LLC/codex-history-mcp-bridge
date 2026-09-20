# Restart, diagnostics, and recovery

## After closing PowerShell or rebooting

Open PowerShell in the **installed** bridge folder. When the key is not already in that process environment:

```powershell
.\load-runtime-key.ps1
```

Paste the saved runtime key into the hidden prompt. Do not paste it into ChatGPT. Then:

```powershell
.\start-tunnel.ps1
```

Leave that process running. The source folder, `.venv`, CLI, saved tunnel profile, and authorized ChatGPT connection must still be available. A key may need replacement if revoked, unavailable, or no longer authorized; a reboot alone does not revoke it.

The original direct launch remains valid:

```powershell
.\tunnel-client.exe run --profile codex-history
```

The helpers do not register a Windows startup task, store a long-lived secret, or change account permissions. Avoid a permanently inherited user-wide credential unless that broader exposure is an explicit choice. Use a trusted secret manager for persistent operations.

## Restart an already-running tunnel

Press Ctrl+C in its window, wait for exit, then launch again in the same shell. Do not close the shell unnecessarily if its key exists only there. Do not leave two stdio instances using the same tunnel ID.

Restarting loses `cbr4_` recovery handles. Start each affected session again without `page_token`; do not reuse old handles or merge abandoned partial coverage. Stop the old process before starting a new one.

A restart with unchanged tool definitions normally does not require re-creating the ChatGPT connection. After changed tools/descriptions, use the plugin's Refresh action and a fresh chat according to [OpenAI's guide](https://developers.openai.com/plugins/deploy/connect-chatgpt).

## Optional readiness check

With the profile's default local health port, run from a second shell:

```powershell
(Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8080/readyz).StatusCode
```

`200` is a runtime readiness check, not evidence of complete session reading. Use the profile's actual port if changed. A successful transcript verifier and a successful ChatGPT traversal test different boundaries.

## Diagnose without disclosing a transcript

```powershell
.\run-doctor.ps1
```

For a particular session:

```powershell
.\.venv\Scripts\python.exe .\audit_rollout.py --thread-id "SESSION_ID"
```

```powershell
.\.venv\Scripts\python.exe .\verify_pagination.py --thread-id "SESSION_ID" --max-pages 3 --max-turns 100 --max-chars 120000
```

Use a real ID from discovery, not the placeholder. Audit inventories known/unknown structures; it does not test rendered text retrieval. The verifier checks continuity only for the pages it actually reads. For a deliberate full traversal, increase `--max-pages` (up to 1000 is an example, not a promise that every session fits).

Diagnostics print session IDs, counts, and possibly paths/type names. Review and redact them before sharing publicly even when payload text is absent. Do not attach an actual rollout, `auth.json`, runtime key, tunnel profile, or an unreviewed log to an issue.

## Common failures

| Symptom | Next check |
|---|---|
| Codex not found | `codex --version`; stale `CODEX_EXECUTABLE`; Windows vs WSL environment. |
| Windows path separators disappear | Use the shipped `configure-tunnel.ps1`, not a hand-quoted command copied through multiple parsers. |
| History line exceeds native stream limit | Automatic local recovery may handle it; increasing `max_chars` does not change the native stream buffer. |
| Recovery handle expired/restarted | Begin a new chain. Check for multiple tunnel instances or a restarted child process. |
| Unknown records or projection gaps | Run the payload-free inventory; report type/field labels rather than discarding the warning. |
| Source file changed | Let the source session become idle and restart. Do not disable consistency checks blindly. |
| No continuation but an error result | An error is not EOF; preserve the diagnostic. |
| Healthy local test, failing ChatGPT call | Verify one running bridge process, loaded version, plugin refresh, and exact tool diagnostic. |

Do not claim a setup is repaired merely because a version or readiness check passes. Verify the specific behavior that failed.

## 0.3.0 search and scope lifetime

`cbh1_` searches, `cbp1_` scopes, `cbg1_` scope-catalog pages and the existing session/page references are process-local. Search state is limited to 16 active chains and 4 MiB per state. The current implementation defaults are a 72-hour sliding idle lifetime plus a separate 30-day absolute cap; these provisional durations govern bookmarks, not search size or page count. Only a valid successful continuation refreshes the idle clock. The same opaque search token remains active through the chain, and responses expose only non-secret lifecycle ages/TTLs. Other references retain 12-hour idle expiry; scope-catalog state remains limited to 16 entries and scopes to 64. An idle-expired, absolute-expired, evicted, restarted or version-invalidated search must restart at page 1. Never combine the abandoned chain with its replacement. State size limits fail rather than truncate. Avoid keeping many overlapping global scans open.

After schema changes refresh the plugin; after a routine process restart recreate affected bookmarks/scopes. Read `verify_discovery.py --help` for local diagnostics. Default search includes archives; `--active-only` excludes them deliberately. Local verification does not require a new runtime key or tunnel profile.
