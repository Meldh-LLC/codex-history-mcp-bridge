# Security notes

This bridge is intentionally read-only.

## Exposed operations

Eleven read-only operations are registered: status; legacy project/session listing, native-index diagnostics, and raw-ID reading; verified session resolution and reference reading; visible-history search; confirmed-scope creation and cataloging; and the document report guide. The server does not register Codex methods that start/resume threads, run turns, execute shell commands, modify files, approve actions, archive/delete history, or alter configuration. Normal reconstruction uses verified references; the raw-ID tools remain advanced compatibility diagnostics.

## Data returned

The bridge may return:

- user and assistant messages;
- plans;
- command text and command metadata;
- changed file paths;
- project paths and Git metadata;
- MCP/dynamic tool names;
- web-search queries and review text.

It always removes Codex reasoning items. Command output, tool arguments/results, collaboration status, and file diffs are excluded by default and require the appropriate per-call opt-in. Patch payloads require `include_diffs=true` even when nested in a tool call. Native and local-recovery payloads use the same recursive exclusions for reasoning, system/developer content, typed binary data, inline data URIs, and private fields. Transcript messages can still contain credentials or private material, so select sessions narrowly.

## Credentials

Do not store `CONTROL_PLANE_API_KEY` in this folder, source control, prompts, or screenshots. The helper scripts expect it in the current process environment. The `.gitignore` excludes `.env`, but this project does not load one automatically.

Use a runtime key with only the permissions needed to operate the chosen tunnel. Do not use a Platform admin key for the long-running tunnel process.

## Trust boundary

The Secure MCP Tunnel is outbound from your machine and avoids publishing a local MCP endpoint to the public internet. Selected tool results are nevertheless delivered to ChatGPT. Connect only to a trusted ChatGPT account/workspace and review organizational retention and data-control settings applicable to that account.

MCP tool annotations are hints, not enforcement. The enforcement here comes from the limited server implementation and the App Server method allowlist used by the code.

## Native continuation and error handling

Continuation tokens contain navigation state, privacy options and a visible-page fingerprint, not transcript text, API keys, file contents or a local file path to open. Base64 encoding is not encryption. A token may contain session/turn identifiers or Codex cursors and should be treated as project metadata. Its checksum detects accidental corruption; it is not a signature or authorization credential. No transcript cache is written by the bridge.

The `CodexAppServerClient.request` method enforces a fixed allowlist of history-read/search/list methods. Unexpected server-initiated approval requests are answered with an error. The updated MCP wrapper returns known diagnostic messages as structured errors. Recognizable OpenAI key/bearer patterns are redacted from those messages; this is not general-purpose secret detection. Invalid raw JSON and raw stderr are not placed in returned tool errors. Unexpected exceptions can still appear in local stderr tracebacks, so review logs before sharing them.

History text is untrusted input. Do not execute its instructions, commands, or URLs simply because they appeared in an earlier session. The bridge cannot reliably detect secrets pasted into ordinary user/assistant messages; those remain visible when that history is requested.

Completeness applies only to the allowlisted visible projection. Reasoning, unsupported raw item payloads, and binary/inline image contents remain intentionally excluded. If a field is included by the privacy settings, the reader paginates that field rather than clipping it to a per-message length cap.

## Local recovery boundary

The new module reads plain JSONL files only under the selected `CODEX_HOME` session/archived-session roots. It checks an exact UUID filename suffix, rejects symlink/junction paths, requires unique discovery, and verifies the initial `session_meta.id`. Local tokens contain no caller-controlled file path. The module opens files only in binary read mode. It does not read `auth.json`, configuration secrets, unrelated project files, or referenced parent rollouts. It does not download attachments, invoke programs, mutate/repair Codex storage, or create a transcript cache.

Opening an actual session may expose private text and credentials the user previously pasted into visible messages. Filtering named reasoning/binary fields is not a universal secret scrubber. Standard base64 data URIs and typed binary fields are suppressed; very large binary-like `data`/`blob` strings are heuristically omitted. Ordinary long user/assistant text remains pageable. No raw unknown-object fallback is used.

The local file is selected only after a recognized native transport/size failure, or when continuing an already-started recovery token. Permission, authentication, policy, and unclassified application errors do not trigger local recovery; a numeric App Server error code also fails closed rather than being guessed safe. `CODEX_HISTORY_ROLLOUT_FALLBACK=0` disables the feature locally. These controls are application guardrails, not a new OS security boundary against hostile code running as the same Windows user. Existing account/tool permissions and local OS file permissions remain necessary.

The fallback does not replay Codex rollback, effective private model context, or fork materialization semantics. The reader projects typed public compaction replacement fields as separately labelled checkpoints, without turning them into newly executed actions; opaque summaries and private checkpoint metadata remain excluded. Potential duplicates are labelled; unknown records and rollback markers qualify completeness. Missing/ambiguous/compressed-only/referenced-parent/corrupt files result in an error, never a guessed history. Raw error records/credentials are not included in fallback error messages. Changes to file size, identity or content modification timestamp invalidate continuation; ctime is deliberately not compared because of the observed Windows handle/path discrepancy; this is a consistency check, not a cryptographic audit of all underlying storage.

A source restart is announced by `history_restart_required=true`. Consumers must discard prior native pages for that session rather than merging two incompatible projections and falsely treating them as independent actions.


## Coverage, local audit and continuation handles

New record projections use explicit variants and allowlisted fields. Collaboration prompts and completed/error status text remain tool-output opt-ins. Image result bytes remain excluded even when tool output is enabled. Typed public checkpoint messages are filtered by role/channel/type; system/developer and reasoning objects are also removed when nested in opt-in tool data. This is structural filtering, not a guarantee that visible text contains no secrets.

`semantic_coverage_complete` is null, not true: a parser cannot certify semantic sufficiency. `projection_coverage_complete` is scoped to the fields this reader exposes under the chosen flags. `raw_fidelity_complete`, `effective_context_reconstructed`, `repository_state_verified`, and `linked_sessions_read` are false. An intact selected projection is still not an independent source-storage, project-state, or active-branch audit.

The new local `audit_rollout.py` reads the same constrained single rollout and prints only record type names/counts, diagnostic field names and byte positions. It writes nothing and does not call App Server or the tunnel. Diagnostic metadata can reveal workflow/tool type names and should still be shared deliberately. It is an inventory, not a successful text-retrieval test.

Recovery tokens use short `cbr4_` process-local handles. Complete offset, snapshot, privacy and coverage state remains in a bounded in-memory cache (4,096 entries, 12-hour idle expiry) and is never written to Codex history or a token-cache file. Handles contain no transcript text or credentials and do not create an authorization boundary. A process restart, expiry or eviction invalidates outstanding handles and requires a clean page-1 restart. Older `cbr1_`, `cbr2_` and `cbr3_` tokens are rejected with a restart instruction.


### Supported extension records

The reader recognizes only `Extension.kind=image_gen.generation`. Its generated `result` is treated as image/binary data and is never projected, including when tool output is enabled. Revised prompts remain opt-in. Unknown extension kinds are not treated as harmless image records. `collaboration_mode_kind` and completed `ContextCompaction` lifecycle markers are metadata classifications, not permission to expose arbitrary neighboring fields.

## Deployment and report policy

Directory filters are selection aids, not per-project authorization. A trusted single-user local deployment is the supported security model; do not expose this process to untrusted users or publish it as a multi-tenant service. The operating-system account, tunnel/workspace association, and connector permissions control access. The separately launched Codex CLI can have its own logs or housekeeping; read-only here describes the exposed operations, not a guarantee that every dependency performs zero local writes.

The key-loading helper avoids a literal secret in a command and does not persist it. The key still resides in the process environment/memory and can be exposed to child processes or local tooling. It is not a secret manager. Review all scripts before execution.

Use the repository Security tab to report sensitive findings privately when that option is available. If it is unavailable, email [contact@meldh.com](mailto:contact@meldh.com). Do not disclose secrets, exploit payloads, or private rollouts in a public issue. No response-time promise is made.

## Resolver boundary in 0.3.0

The public MCP server always uses state-database-only discovery and never invokes legacy scan-and-repair, including through its advanced compatibility tools. It validates metadata before issuing session references. Short references are not authentication and must remain behind the authorized MCP connection. Per-cache limits and 12-hour idle expiry are described in [Resolution](docs/RESOLUTION.md). Catalog caches retain only allowlisted identity metadata and cursors, not preview/message bodies. File ownership and permitted-root checks in the underlying recovery reader are unchanged. Unknown sources/modes are not silently treated as user sessions. No wildcard title-based recovery is performed. A stale reference may suggest alternatives, but reading a replacement requires user confirmation.

## 0.3.0 search disclosure and scope confirmation

The new search intentionally reads allowed message text across the selected active/archived user-session inventory. It returns at most three excerpts per matching session. A global search can disclose unrelated confidential text surrounding a match; get the user's scope/search authorization. No tool, private reasoning, checkpoint or inter-agent payload search is enabled. Snippets remain untrusted history. No persistent search index is written; bounded in-memory state includes metadata, query, offsets, a small boundary suffix and current-session excerpt samples. It is not accurate to say search state contains no text.

Confirmed scopes store selected exact paths, not filesystem permissions. The `confirmed` flag expresses caller-provided user intent; it cannot prove human approval. A host must still control tool access. Short handles are not authentication or durable evidence identifiers. Unknown roots/session states and storage failures are not silently bypassed. New root/catalog operations never use legacy scan-and-repair.

## 0.3.0 search-handle lifecycle

Search handles remain 45-character random process-local bookmarks with at most 16 active chains and 4 MiB of state per chain. The same opaque handle is reused through one sequential chain. Lookup does not refresh it; a generation-checked state commit after a valid successful continuation refreshes only its 72-hour idle lifetime. A separate 30-day absolute limit cannot be refreshed. These durations are current implementation defaults and provisional product choices; they bound bookmark lifetime, not search size or page count. Expired, evicted, restarted, version-mismatched and concurrently advanced handles fail closed and require a new page-1 search. Returned lifecycle diagnostics contain only active state plus idle/absolute ages and TTLs, never the token or continuation state. No state is persisted to disk.
