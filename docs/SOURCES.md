# External references

Last checked 2026-09-20. Product interfaces, permissions, and software versions can change.

## Setup and dependencies

- [ChatGPT developer mode and custom MCP apps](https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt)
- [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
- [Secure MCP Tunnel onboarding](https://github.com/openai/tunnel-client/blob/master/docs/onboarding.md)
- [Codex CLI](https://developers.openai.com/codex/cli/)
- [Official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [MCP Python SDK documentation](https://py.sdk.modelcontextprotocol.io/)
- [MCP Python SDK 2.2.0 package metadata](https://pypi.org/project/mcp/2.2.0/)

The direct runtime dependency is pinned to MCP Python SDK 2.2.0. Transitive dependencies are resolved from that package's metadata; this project does not claim a fully hash-locked, cross-platform dependency closure.

The GitHub Actions workflow pins `actions/checkout` and `actions/setup-python` to specific commits. Review those pins when updating the workflow rather than describing them as permanently current.

## Codex protocol references

- [App Server documentation](https://developers.openai.com/codex/app-server/), consulted 2026-09-17
- [Current generated `ThreadItem` union](https://github.com/openai/codex/blob/17baabd01b3131a8d07327185a00cb2a771ff1a7/codex-rs/app-server-protocol/schema/typescript/v2/ThreadItem.ts)
- [`ImageGenerationItem` fields](https://github.com/openai/codex/blob/17baabd01b3131a8d07327185a00cb2a771ff1a7/codex-rs/app-server-protocol/schema/typescript/ImageGenerationItem.ts)
- [`SleepItem` fields](https://github.com/openai/codex/blob/17baabd01b3131a8d07327185a00cb2a771ff1a7/codex-rs/app-server-protocol/schema/typescript/SleepItem.ts)

These references support the implemented compatibility rules. Unknown future variants remain explicit coverage gaps. See [Protocol notes](../PROTOCOL_NOTES.md) for the older persisted-history references used by the fallback reader.
