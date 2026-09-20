# Privacy model

Codex History Bridge runs on the machine that holds the selected Codex history. It does not create a transcript index or copy complete histories into a package. When ChatGPT calls a bridge tool, the selected result is sent through the configured Secure MCP Tunnel to that ChatGPT workspace.

## What can leave the machine

- catalog metadata such as session names, dates, recorded working directories, and archive status;
- direct user/assistant text returned by a requested read;
- up to three bounded excerpts per matching session during visible-history search;
- tool output and file diffs only when the caller explicitly enables their separate opt-in flags;
- diagnostic counts, type names, paths, and identifiers returned by advanced local tools.

Reasoning, system/developer messages, binary media payloads, and raw unknown objects are excluded by the default projection. These filters do not detect every secret a person may have pasted into ordinary visible text.

## Local state

Continuation, session, search, and scope references are opaque process-local bookmarks. Their bounded state remains in memory and disappears on restart, expiry, or eviction. The bridge writes no transcript cache or search index. Codex App Server and the tunnel client may create their own ordinary logs or operational files.

## Safe use

Use a trusted ChatGPT account or workspace, confirm exact roots before broad reconstruction, and authorize global visible-message search only when its broader disclosure is acceptable. Review diagnostic output before posting it publicly. Never attach real rollouts, `auth.json`, runtime keys, tunnel profiles, raw session IDs, or unreviewed generated reports to an issue.

See [SECURITY.md](SECURITY.md) for the threat boundary, credential handling, fallback rules, and private vulnerability-reporting guidance.
