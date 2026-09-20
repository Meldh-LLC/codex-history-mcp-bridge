# Architecture

- `codex_bridge.py`: App Server client, explicit read-only RPC allowlist, native paginated reader and advanced diagnostics.
- `rollout_fallback.py`: bounded JSONL recovery after eligible App Server read failure; privacy projection and short process-local continuation handles.
- `session_resolver.py`: exact-directory state inventory, candidate validation and verified-reference reader. Also supplies the document-first report guide.
- `project_discovery.py`: global/restricted verified-message scanning plus explicitly confirmed multi-root scopes and independent root catalog pagination. It calls the reader internally; no second low-level parser.
- `server.py`: MCP registration, read-only annotations and safe wrappers.
- `verify_discovery.py`: local search and scope verifier; prints no excerpts or tokens.

Search does not call the native full-text index, infer a directory alias, or repair storage. Scope catalog is metadata-only. A conceptual scope is a user decision over exact recorded cwd roots. Source metadata and query matching cannot prove complete conceptual membership.

All work is read-only with respect to project/history mutations. Client startup can generate ordinary App Server runtime logs. Search has bounded in-memory metadata/excerpt/offset state and no disk index. Raw-ID diagnostics remain advanced; normal callers use verified short references. [Full search/scope contract](DISCOVERY.md).
