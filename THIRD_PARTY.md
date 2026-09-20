# Third-party components and attribution

This is an unofficial integration; no endorsement is claimed.

- The [official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) is installed separately using `requirements.txt` (`mcp==2.2.0`, verified 2026-09-20). Its upstream license is MIT. No SDK source is bundled.
- [OpenAI Codex](https://github.com/openai/codex) and [Secure MCP Tunnel client](https://github.com/openai/tunnel-client) are separate programs with their own upstream licenses and terms. Their binaries, private runtime configuration, and account credentials are not shipped.
- Historical schema references are documented in [PROTOCOL_NOTES.md](PROTOCOL_NOTES.md). Referencing a protocol is not a guarantee of compatibility with all future versions.
- GitHub Actions used by the CI workflow run separately under their upstream licenses; they are not vendored into this package.

AI tools were used during development. Automated tests use synthetic data and do not substitute for independent security review or broad platform validation. Preserve upstream notices if future contributions copy upstream implementation code; this file does not replace those notices.
