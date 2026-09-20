# Testing

All histories and App Server responses used by the automated tests are synthetic. The suite does not require user Codex history, an account key, a tunnel connection, or a configured ChatGPT app.

Tool-registration tests use a local stub. The continuous-integration workflow separately installs the declared MCP SDK and checks server import and registration.

## Search lifecycle regressions

The suite covers 105 sequential visible-history continuation calls with simulated delays; stable copy-safe tokens; idle refresh only after a valid commit; invalid-call non-refresh; idle and absolute expiry; process/version invalidation; token-free lifecycle diagnostics; and the 4 MiB state bound.

## Reader, resolver, and privacy coverage

Tests cover current App Server non-message variants; hook/collaboration/generated-image exclusions; unknown future variants blocking false completeness; exact-directory resolution; multi-root scopes; duplicate titles; archive and background filtering; short process-local handles; native and fallback pagination; 16 MiB transport recovery; multi-million-character fields; compaction checkpoints; coverage accounting; source restarts; and literal visible-message search.

The synthetic discovery suite includes a 40-session inventory, an empty native search index despite matching readable history, and local subprocesses running synthetic stdio App Servers. These fixtures do not establish compatibility with every live Codex installation or account configuration.

## Reproduce

From the directory containing the Python files:

```powershell
python -m unittest discover -s tests -v
```

```powershell
python tools/release_check.py
```

The local helpers in `UPGRADING.md` and `docs/DISCOVERY.md` print safe statuses rather than transcript excerpts or tokens. `catalog` is metadata-only. `search` reads allowlisted direct user/assistant text locally but prints no excerpts, tokens, or raw Codex IDs.

## Coverage limits

The tests do not prove semantic completeness, current repository truth, future Codex compatibility, or confidentiality of visible text that already contains a secret. A passing suite also does not establish a fresh-machine installation, macOS tunnel operation, or current account/workspace permissions.
