# Verified session selection — 0.3.0

## Flow

`project name -> confirmed exact directory -> state-only inventory -> metadata validation -> session_ref -> paginated read -> document`

A name search samples up to 5,000 inventory rows and 100 pages across the selected archive filters, with a per-call work budget. It always returns directory choices, even if only one matches. Exact paths are lexical comparisons: Windows extended prefixes and slash/case differences are normalized; POSIX case is preserved. No folder aliases, underscore substitutions or symlink resolution are guessed.

The exact-directory catalog uses `thread/list` with `useStateDbOnly=true`. Each candidate is checked with `thread/read(includeTurns=false)`. Thread ID, directory and reported stable identity fields must agree. Unknown/missing history modes, mismatched IDs, changing candidate metadata and failed metadata reads become rejected entries. A single candidate failure does not discard others. An empty inventory is scoped to these filters; it is not proof that no history exists anywhere.

`validation_status=metadata_verified` and `selectable=true` mean identity was resolved, not that transcript retrieval was tested. `history_readability=not_probed` is deliberate. No history body is fetched by resolution.

## Scope and identity

Defaults exclude archives and background sources. A parent-thread ID, tagged subagent source, non-user thread source, or unrecognized source classification is excluded unless background inclusion was explicitly selected. A user fork is kept distinct from a spawned child. Threads are keyed by their canonical thread ID; a shared session-tree ID never merges child threads into a parent. Duplicate titles remain separate choices; duplicate exact IDs are flagged rather than counted twice. Active/archive collisions are explicitly reported.

The resolver does not read SQLite directly, scan rollouts, repair inventory, resume threads, or substitute a filename for identity. Local recovery enforces permitted-root, exact-thread ownership, and filename checks. It does not support external paths, compressed-only storage, or unmapped legacy-only candidates.

Advanced legacy listing remains separate for compatibility, and the MCP server forces state-database-only discovery for it as well. No tool requests App Server scan-and-repair behavior. A legacy result is a lead, not a resolver-issued reference, and is never silently promoted into a verified catalog.

## References

- `cbs1_`: verified session selection.
- `cbq1_`: exact-directory catalog continuation; binds directory/home/filter options.
- `cbc1_`: session read continuation; binds selected reference, home, privacy flags, and the underlying reader token.

Each is a 45-character random process-local handle. Each of the three caches has at most 4,096 entries and 12-hour idle expiry. Caches store identity, bounded candidate metadata and cursors/counters, not returned transcript pages. These are retrieval bookmarks, not authorization. Keep the bridge process running; restart/expiry/eviction requires resolving again and starting the affected read without a token.

First reads revalidate identity. If metadata changed or no longer resolves, a bounded state-inventory refresh returns verified candidates with the same display name for user confirmation. It NEVER silently redirects, even if there is only one. Refresh results can be incomplete; follow the supplied catalog continuation. Unknown/corrupt handles have no reliable identity to recover: resolve the project again.

The wrapper verifies the returned thread identity and preserves source-restart and coverage fields. Metadata can still change concurrently; the reader's page fingerprint and file-snapshot guards remain the protection for content continuation.

## Consumer behavior

Resolve the full chosen catalog, then complete sessions independently. A consumer can still stop because of model/tool/context limits; it must preserve a partial report, not claim EOF. If a source restart is returned, start coverage for that session again. Do not treat checkpoint snapshots, duplicate event representations or linked agent IDs as newly executed work.

References hide IDs from control inputs, not from evidence. Read results may contain canonical thread/item IDs for citation in the report appendix. The user should choose by name/date/directory, not type UUIDs.

## Source contract

OpenAI's App Server documentation defines metadata-only reads, exact-directory/source/archive filters, state-only listing and opaque pagination cursors: https://developers.openai.com/codex/app-server/ (reviewed 2026-09-17). This is not a guarantee that every future App Server version preserves the schema or lists every valid local historical representation.

## Conceptual scopes

The exact-directory resolver is intentionally exact. `codex_search_visible_history` and `codex_create_project_scope`/`codex_catalog_project_scope` cover broader conceptual projects; see [Discovery](DISCOVERY.md). Name/path matching does not establish one-folder conceptual scope. A same-folder second session is cataloged independently without requiring its title to repeat the project name.
