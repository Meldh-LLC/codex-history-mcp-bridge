"""Read-only local smoke test. Prints counts/flags, never transcript text or tokens.

This checks continuation offsets in the pages actually read. It is not an
independent audit of Codex's underlying storage. Use --max-pages to bound work.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from codex_bridge import BRIDGE_VERSION, CodexBridgeError, list_sessions, read_session


async def run(args: argparse.Namespace) -> int:
    thread_id = args.thread_id
    if not thread_id:
        listing = await list_sessions(limit=1)
        sessions = listing.get("sessions") or []
        if not sessions:
            raise ValueError("No sessions found. Supply --thread-id for the session you intend to test.")
        thread_id = sessions[0]["id"]
    print(json.dumps({"bridge_version": BRIDGE_VERSION, "thread_id": thread_id, "max_pages": args.max_pages}), flush=True)
    token = None
    seen_tokens: set[str] = set()
    field_progress: dict[tuple[str, int, str], tuple[int, int, bool]] = {}
    total_chars = 0
    fallback_seen = False
    for page_number in range(1, args.max_pages + 1):
        result = await read_session(
            thread_id=thread_id,
            max_turns=args.max_turns,
            max_chars=args.max_chars,
            page_token=token,
            include_tool_output=getattr(args, "include_tool_output", False),
            include_diffs=getattr(args, "include_diffs", False),
        )
        fallback_seen |= result.get("history_source") == "local_rollout_fallback"
        if result.get("history_restart_required"):
            field_progress.clear()
            total_chars = 0
            print(json.dumps({"notice": "SOURCE_RESTART", "action": "Discarded prior native pages; verifying local recovery from its beginning."}), flush=True)
        has_more = result["has_more_content"]
        next_token = result["next_page_token"]
        if has_more != bool(next_token):
            raise CodexBridgeError("FAIL: continuation flag/token mismatch.")
        if result["character_truncated"] and not next_token:
            raise CodexBridgeError("FAIL: unread content has no continuation token.")
        if next_token and next_token in seen_tokens:
            raise CodexBridgeError("FAIL: continuation repeated without advancing.")
        for turn in result["turns"]:
            for item in turn["items"]:
                for fragment in item["fragments"]:
                    key = (str(turn["id"]), item["item_index"], fragment["field"])
                    end, size, completed = field_progress.get(key, (0, fragment["total_chars"], False))
                    if completed or fragment["offset"] != end or fragment["total_chars"] != size:
                        raise CodexBridgeError("FAIL: a field has a gap, overlap, duplicate or changed size.")
                    if fragment["end_offset"] != end + len(fragment["text"]):
                        raise CodexBridgeError("FAIL: a field fragment length does not match its offsets.")
                    if fragment["end_offset"] > size or (fragment["complete"] and fragment["end_offset"] != size):
                        raise CodexBridgeError("FAIL: invalid field completion boundary.")
                    field_progress[key] = (fragment["end_offset"], size, fragment["complete"])
        total_chars += result["chars_returned"]
        print(json.dumps({"page": page_number, **{key: result[key] for key in ("page_complete", "character_truncated", "has_older_turns", "has_more_content", "continuation_reason", "turns_returned", "items_returned", "chars_returned")}, "payload_chars": result["output_info"]["payload_chars"], "next_token_present": bool(next_token), "history_source": result.get("history_source"), "fallback_info": result.get("fallback_info"), "coverage_complete": result.get("coverage_complete"), "projection_coverage_complete": result.get("projection_coverage_complete"), "record_support_complete": result.get("record_support_complete"), "unknown_records": (result.get("coverage_report") or {}).get("unknown_records")}), flush=True)
        if not has_more:
            if getattr(args, "require_fallback", False) and not fallback_seen:
                raise CodexBridgeError("FALLBACK_NOT_EXERCISED: the tested pages used App Server successfully. This does not test local recovery; no history-read failure was observed.")
            if any(not completed or end != size for end, size, completed in field_progress.values()):
                raise CodexBridgeError("FAIL: the read ended with an unfinished field.")
            print(json.dumps({"result": "PASS_HISTORY_READ_FINISHED_WITH_COVERAGE_WARNINGS" if result.get("coverage_complete") is False else "PASS_HISTORY_READ_FINISHED", "pages_read": page_number, "content_chars_read": total_chars, "coverage_complete": result.get("coverage_complete"), "warnings": result.get("warnings", []), "coverage_report": result.get("coverage_report"), "note": "All returned field offsets are continuous and no continuation remains. This is not an independent storage audit. A warning-qualified finish does not establish full content or active-branch coverage."}), flush=True)
            return 0
        seen_tokens.add(next_token)
        token = next_token
    if getattr(args, "require_fallback", False) and not fallback_seen:
        raise CodexBridgeError("FALLBACK_NOT_EXERCISED: the bounded pages used App Server, not local recovery. More source pages may remain.")
    print(json.dumps({"result": "PASS_PAGES_TESTED_MORE_AVAILABLE", "pages_read": args.max_pages, "content_chars_read": total_chars, "note": "The local test stopped at --max-pages, not at the end of the session. No full-history coverage claim is made."}), flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread-id", help="Exact Codex thread ID. Without this, tests the newest session.")
    parser.add_argument("--max-pages", type=int, default=3, help="Maximum pages to test; default 3.")
    parser.add_argument("--max-turns", type=int, default=100)
    parser.add_argument("--require-fallback", action="store_true", help="Require that the tested pages actually exercise automatic local rollout recovery; does not force recovery.")
    parser.add_argument("--max-chars", type=int, default=120000)
    parser.add_argument("--include-tool-output", action="store_true", help="Opt into sanitized tool payloads, including collaboration prompts/results. Values are read for verification but never printed by this helper.")
    parser.add_argument("--include-diffs", action="store_true", help="Opt into file diffs for verification; their contents are never printed by this helper.")
    args = parser.parse_args()
    if not 1 <= args.max_pages <= 5000:
        parser.error("--max-pages must be between 1 and 5000")
    if not 1 <= args.max_turns <= 100:
        parser.error("--max-turns must be between 1 and 100")
    if not 2000 <= args.max_chars <= 120000:
        parser.error("--max-chars must be between 2000 and 120000")
    try:
        return asyncio.run(run(args))
    except (CodexBridgeError, ValueError) as exc:
        # Known diagnostic messages only; do not print the returned transcript.
        print(json.dumps({"result": "FAIL", "error_type": type(exc).__name__, "error": str(exc)}), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
