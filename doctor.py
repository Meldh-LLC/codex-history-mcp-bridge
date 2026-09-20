from __future__ import annotations

import asyncio
import json
import sys

from codex_bridge import bridge_status, list_sessions


async def main() -> int:
    print("Checking Codex History MCP Bridge prerequisites...", flush=True)
    try:
        status = await bridge_status()
        print(json.dumps(status, indent=2, ensure_ascii=False), flush=True)
        if not status.get("app_server_ok"):
            print("\nFAILED: Codex app-server could not be queried.", file=sys.stderr)
            return 1

        sessions = await list_sessions(limit=5)
        print("\nFive-session probe:", flush=True)
        print(json.dumps(sessions, indent=2, ensure_ascii=False), flush=True)
        print("\nDoctor completed successfully.", flush=True)
        return 0
    except Exception as exc:
        print(f"\nFAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
