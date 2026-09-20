# Document-first project handoff

The bridge returns history and report instructions. The consuming chat creates the file with its available document tools. No document-generation dependency is added to the local MCP server.

## Default deliverable

An editable Word `.docx` with a brief chat answer (normally at most 150 words). A PDF is optional when requested, not a required local installation dependency. Do not claim file creation or supply a download link unless the artifact exists. When file tools are unavailable, say so and supply structured document-ready Markdown.

## Reading order

1. **Overview (one page).** Project, historical cutoff, evidence boundary, current recorded state, principal unresolved issue and next actions. Use a small status table only if it clarifies distinct workstreams.
2. **Findings.** Completed work and actual recorded verification; claimed-but-unverified work; proposals; failures; recorded decisions. Use thematic headings and short paragraphs. Keep facts, owner decisions and assistant proposals distinct.
3. **Next actions.** The smallest useful set with the evidence or approval needed to proceed. Do not silently convert historical suggestions into authorized tasks.
4. **Evidence and coverage appendix.** Session aliases and full IDs, source/turn/item citations, date ranges, accepted page counts, terminal flags, restart handling, omissions, rejected/unread scope, and relevant caveats. Fields absent from native output are `Not reported`, never invented as true.

Use proper heading styles, page numbers, readable text size and spacing, repeating table headers, and a contents list for long reports. Prefer a short main report plus appendices over shrinking text or deleting qualifications. Review the rendered document where tools permit.

## Evidence discipline

A chronological history is not a current repository audit. Reported builds/deployments/tests retain their historical dates and supporting session references. Projection completeness is not semantic completeness or knowledge of linked sessions. Media, diffs and tool payloads may be missing by design and can still matter. Mention meaningful gaps in the overview; move diagnostic detail to the appendix.

When converting an already completed handoff, do not reread the same history only for formatting. Preserve the supplied report's claims, terminology and uncertainties. An executive summary may condense them, but the supporting detail should remain accessible.

## Template

```
[Project] — Historical handoff
Historical cutoff: [date/range]
Prepared: [date]
Evidence: [selected sessions; actual current checks or none]

Overview
[Recorded state, main open issue and next steps]

Findings
[Thematic sections with source references]

Decisions and next actions
[Separate approved decisions from proposed actions]

Appendix A — Sources and coverage
[Session key, dates, page counts, terminal fields, citations, exclusions]
```

## Multi-root and cross-session appendix

Include the confirmed root set, every root's catalog status, archives/background policy, rejected/excluded candidates, and any search-coverage gap. Keep every distinct user session even when cwd/title match. Use short evidence aliases in the body and durable names/dates/cwd/source item locators in the appendix; in-memory handles are bookmarks, not permanent citations. Search samples are not complete history. Distinguish latest evidence from superseded measurements and recommendations from recorded user decisions.
