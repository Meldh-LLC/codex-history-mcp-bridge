# Protocol sources and design decisions

Implementation was checked against these primary source files in `openai/codex`, pinned at commit `3c6f32ca8293e24879859501793f31422d2012be` during the earlier reader build. This is a reference snapshot, not a claim that every historical local rollout uses that schema.

- Collaboration lifecycle/status and inter-agent types: https://github.com/openai/codex/blob/3c6f32ca8293e24879859501793f31422d2012be/codex-rs/protocol/src/protocol.rs (especially the collaboration definitions around lines 4270–4500).
- Persisted rollout families and typed compaction replacement history: https://github.com/openai/codex/blob/3c6f32ca8293e24879859501793f31422d2012be/codex-rs/history/src/rollout_payload.rs
- Native versus legacy event persistence and item-completed representations: https://github.com/openai/codex/blob/3c6f32ca8293e24879859501793f31422d2012be/codex-rs/rollout/src/policy.rs
- Python filesystem metadata portability: https://docs.python.org/3.13/library/os.html#os.stat_result

The source defines `replacement_history` as typed response items with optional metadata. The bridge examines those items with its public-text filter, not as new recorded actions. It does not expose private checkpoint metadata, untyped context summaries, or encrypted content; it does not claim to replay an active model context. A checkpoint without a recognized typed replacement is an explicit projection gap.

The collaboration protocol can carry an agent's prompt and completed/error text inside a status value. These are not harmless empty control flags: prompts/status payloads require the existing tool-output opt-in. Sender/receiver identifiers and state tags can be inspected separately. Other agent sessions are not implicitly opened.

Image operations may carry textual status/failure/path metadata alongside image result bytes. The textual metadata is projected and binary result payloads are omitted. A stored saved path is not proof that the file exists now, and a completion marker is not proof of repository/test success.

Control variants are named explicitly. Familiar prefixes are not used to declare unknown events complete. Known control events with unexpected fields remain warning-qualified; unknown inner content and checkpoint items are counted as well as unknown top-level types.

The bridge recognizes three exact variants rather than relying on name prefixes: `task_started.collaboration_mode_kind=default`, `item_completed.ContextCompaction`, and `item_completed.Extension` with `kind=image_gen.generation` and `status=completed`. It classifies the first two as known lifecycle metadata. It classifies only that exact extension kind as image generation, returns allowlisted status/path/failure metadata, and omits the `result` image payload. Any different extension kind remains unsupported.

Windows `ctime` meanings/reporting are not a portable content-change identity. The observed handle/path discrepancy is handled by using size, modification time, device and inode/file ID consistently. These checks reject ordinary mutation/replacement but are not a cryptographic snapshot against metadata forgery by a process with equivalent filesystem access.

No private rollout is bundled. Tests use synthetic fixtures for counter, pagination, and unsupported-record behavior.

## Short local continuation handles

The MCP model must echo `next_page_token` into the next tool call. To avoid transporting long serialized continuation state, the bridge keeps that state in a bounded in-memory LRU cache and returns a 45-character `cbr4_` SHA-256-derived handle. The handle is validated again against thread ID, privacy flags, offsets, snapshot identity and coverage-counter invariants after lookup. It is not persisted and intentionally expires when the bridge process restarts.

## 0.3.0 resolver design

The resolver uses only `thread/list(useStateDbOnly=true)` and `thread/read(includeTurns=false)` before calling the reader. Reference: https://developers.openai.com/codex/app-server/ (reviewed 2026-09-17). State inventory is authoritative within the chosen filters, not a universal archive catalog. Missing candidates never trigger unrequested index repair. Metadata verification is distinct from history-read success.

## 0.3.0 native item compatibility

The current App Server `ThreadItem` union includes non-message variants such as `hookPrompt`, `collabAgentToolCall`, `subAgentActivity`, `sleep`, and `imageGeneration`. Version 0.3.0 recognizes those variants in the privacy projection while keeping hook/collaboration prompt bodies and generated image result bytes excluded by default. The reader and cross-session search share one recognized-type registry; future unknown variants keep search coverage partial and are reported by type.

Reviewed source: https://github.com/openai/codex/blob/17baabd01b3131a8d07327185a00cb2a771ff1a7/codex-rs/app-server-protocol/schema/typescript/v2/ThreadItem.ts (2026-09-17).

## 0.3.0 search lifecycle

`cbh1_` visible-history search state now has its own bounded cache rather than inheriting the ordinary-reference lifetime. One random opaque handle remains stable through a sequential chain. A successful generation-checked commit refreshes a 72-hour idle deadline; lookup, validation failure and failed commit do not. A separate 30-day absolute deadline, process memory loss, eviction or bridge-version mismatch requires a clean page-1 restart. The response reports non-secret age/TTL values under `search_handle_lifecycle`; the token and internal continuation state are not included there. This does not change the underlying reader, fallback tokens, resolver references or search completeness rules.
