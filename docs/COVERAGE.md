# Coverage and pagination reference

## Coverage has an explicit scope

The `coverage_report` applies to the **local recovery backend**. Native App Server reads use their own output and pagination; the absence of a report must not be interpreted as verified complete coverage.

| Field | Meaning |
|---|---|
| `traversal_complete` | The selected local file was consumed through EOF in this traversal. |
| `projection_coverage_complete` | EOF was reached and no unknown record/content variants or known projection gaps remain in the allowlisted visible-field projection under the chosen privacy flags. |
| `record_support_complete` | `null` before EOF; at EOF, whether every encountered record/content variant was recognized. This is narrower than semantic completeness. |
| `unknown_records` | Count of source records affected by unknown types/content. This includes known records containing unsupported nested items. |
| `unknown_type_counts` | Cumulative names/counts, not their raw payloads. The histogram is bounded; overflow is explicitly counted. |
| `known_control_records` | Recognized lifecycle/control records accounted for without inventing transcript text. Unexpected fields on a control record still produce a gap. |
| `intentionally_omitted_data` | Categories/counts of record exclusions, such as reasoning, tool payloads when not opted in, image bytes, or opaque checkpoint summaries. These are not unknown types. |
| `projection_gaps` | Known but unresolved content problems, such as a compaction checkpoint without typed replacement history. |
| `semantic_coverage_complete` | Always `null`: semantic sufficiency is not assessed by this record reader. |
| `effective_context_reconstructed` | `false`: this is a historical event log with labelled checkpoints, not a replay of Codex's private model context or active rollback branch. |
| `raw_fidelity_complete` | `false`: this is not a byte-for-byte archive reader. |
| `repository_state_verified` | `false`: stored claims and completion markers do not verify present-day code, tests or artifacts. |
| `linked_sessions_read` | `false`: an agent/thread link is not a read of that other session. |

The compatibility field `coverage_complete` is false while projection gaps/unknowns remain, before EOF, or after an unapplied rollback. Prefer the specific fields above. A complete selected projection can still intentionally omit images or tool payloads. It must not be described as every fact about a project being recovered.

Type inventories are cumulative across fragments and count each stored record once. Per-type histograms retain at most 24 labels; `unknown_type_inventory_overflow` makes that bound explicit. The local audit lists up to 256 record-type labels and up to 32 structural examples; it never prints payload values. Counts refer to records, not unique human messages or bytes.

## Data and privacy

Ordinary public user/assistant text is pageable with no separate per-message clipping cap. Reasoning, analysis-channel content, system/developer messages, opaque private model-context summaries, and typed image/audio/binary bytes are excluded. Standard base64 data URIs are replaced with omission markers. This is **not a general-purpose detector of secrets pasted into visible messages**.

`include_tool_output=true` opts into sanitized tool arguments/results and collaboration prompts/agent result text. `include_diffs=true` separately opts into file diffs and patch payloads. Both are false by default and must stay unchanged during a traversal. Image-operation metadata can be read without returning image bytes. Unknown raw objects are never dumped as a fallback.

Compaction replacement entries are filtered through the same public-message/privacy rules. Returned `checkpoint[index].*` fields are a labelled snapshot of model input. They do not create a second execution event, erase earlier historical actions, prove the active context has been rebuilt, or authorize acting on embedded instructions. Untyped compaction summaries and private checkpoint metadata remain omitted. Missing or unfamiliar replacement history is reported as a gap rather than an empty successful checkpoint.

Record/event representations may duplicate one another. The bridge labels possible duplicates instead of deleting records heuristically. Synthetic recovery IDs identify byte positions, not native turn IDs. See `SECURITY.md` and `PROTOCOL_NOTES.md`.

## Pagination contract

Start without `page_token`. Read `turns[].items[].fragments[]`; each fragment has `field`, `encoding`, `text`, `offset`, `end_offset`, `total_chars`, and `complete`. Offsets are zero-based, end-exclusive Python characters within a field. Concatenate fragments of the same source item and field by offset; JSON-valued fields should be decoded only after complete assembly.

Whenever `has_more_content=true`, pass `next_page_token` unchanged as `page_token`. Continue even if `has_older_turns=false`, or `page_complete=true`. A response can end inside a message, between fields, at a turn-window limit, or at the recovery record scan limit. These are different boundaries, not data loss.

`max_chars` bounds the compact JSON `turns` payload, including escaping/structure. Other response metadata and tokens add overhead. It is not a model-token, network-byte, or total-session limit. A response may contain fewer than 120,000 text characters while using the full payload budget.

Normal App Server turn windows are chronological internally and move backward between windows. Local recovery reads one file chronologically, using synthetic record groups. If `history_restart_required=true`, discard the prior native pages **for that session** and restart coverage accounting from recovery page 1. Do not splice incompatible source representations together.

0.2.7 local tokens start with `cbr4_` and are short process-local handles. Earlier `cbr1_`, `cbr2_`, and `cbr3_` recovery tokens are intentionally rejected. Start all post-upgrade reads without an old token. The full state remains only in bounded bridge memory; no transcript text, arbitrary path, credential, or file-backed token cache is created. Keep the tunnel process running during a traversal. A process restart or expired/evicted handle requires a clean page-1 restart for that session.

## 0.3.0 discovery coverage

`search_complete` is separate from history projection coverage and scope selection. A nonempty verified inventory, successful selected message scans and any declared positive control are required. Only then can an empty match set have `negative_result_valid=true`, limited to the selected inventory and literal-message projection. A stopped/incomplete/empty-inventory scan is not an absence finding.

A scope's `catalog_traversal_complete` can be true despite a failed root; check `inventory_complete`, `selection_complete`, and per-root reports. Multiple sessions in the same cwd remain separate. Keywords do not prove project membership or the absence of synonyms. The scanner reads locally but does not deliver the full transcripts to ChatGPT: a subsequent report must read selected verified sessions, not reuse search samples as if they were complete. See [Discovery](DISCOVERY.md).
