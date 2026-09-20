"""Local resolver check: no model requests, no transcript text or token output.

Default is metadata-only. --pages-per-session 1 reads one page from each verified
reference, not an exhaustive reconstruction. Run from the source folder.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import sys

from codex_bridge import BRIDGE_VERSION, CodexBridgeError
from session_resolver import resolve_project_sessions, read_session_ref


async def run(args: argparse.Namespace) -> int:
    print(json.dumps({"bridge_version": BRIDGE_VERSION, "project": args.project,
                      "pages_per_session": args.pages_per_session}), flush=True)
    catalog_token = None
    sessions, rejects = [], []
    exhausted = False
    for _ in range(args.max_catalog_pages):
        result = await resolve_project_sessions(project=args.project, include_archived=args.include_archived,
                                                include_background=False, page_token=catalog_token, limit=20)
        if result["status"] != "resolved":
            print(json.dumps(result, ensure_ascii=True), flush=True)
            return 2
        sessions.extend(result["sessions"])
        rejects.extend(result["rejected_candidates"])
        print(json.dumps({"catalog_page": _ + 1, "verified": len(result["sessions"]),
            "rejected_candidates": result["rejected_candidates"], "excluded_candidates": result["excluded_candidates"],
            "has_more_candidates": result["has_more_candidates"], "inventory_source": result["inventory_source"]}), flush=True)
        catalog_token = result["next_page_token"]
        if catalog_token is None:
            exhausted = True
            break
    if not exhausted:
        print(json.dumps({"result": "PARTIAL_CATALOG", "reason": "max_catalog_pages", "verified_in_prefix": len(sessions)}), flush=True)
        return 2
    failed = []
    for s in sessions:
        print(json.dumps({"session": s["display_name"], "cwd": s["cwd"], "archived": s["archived"],
                          "validation_status": s["validation_status"], "history_mode": s["history_mode"]}), flush=True)
        token = None
        for n in range(args.pages_per_session):
            try:
                result = await read_session_ref(session_ref=s["session_ref"], page_token=token,
                    max_turns=1, max_chars=4000)
                if result.get("is_error") or result.get("ok") is False:
                    raise CodexBridgeError(result.get("status", "read_error"))
                print(json.dumps({"session": s["display_name"], "page": n + 1, "history_source": result.get("history_source"),
                    "has_more_content": result["has_more_content"], "next_token_present": bool(result["next_page_token"]),
                    "chars_returned": result.get("chars_returned"), "history_restart_required": result.get("history_restart_required", False)}), flush=True)
                token = result["next_page_token"]
                if token is None:
                    break
            except (CodexBridgeError, ValueError) as exc:
                failed.append(s["display_name"])
                print(json.dumps({"session": s["display_name"], "result": "READ_FAILED", "error_type": type(exc).__name__,
                    "message": "Resolve/read failed; no transcript or raw exception body printed."}), flush=True)
                break
    label = "PASS_RESOLUTION_AND_BOUNDED_READS" if args.pages_per_session else "PASS_METADATA_RESOLUTION"
    if not sessions: label = "NO_VERIFIED_SESSIONS"
    if rejects or failed: label = "PARTIAL_WITH_CANDIDATE_OR_READ_FAILURES"
    print(json.dumps({"result": label, "verified_sessions": len(sessions), "rejected_candidates": len(rejects),
        "failed_reads": len(failed), "inventory_complete": exhausted,
        "note": "Metadata verification is not history recovery. Bounded page reads do not establish full-session coverage."}), flush=True)
    return 0 if label.startswith("PASS") else 2


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--project", required=True, help="Exact absolute project directory; a name returns choices only.")
    p.add_argument("--include-archived", action="store_true")
    p.add_argument("--pages-per-session", type=int, default=0, choices=range(0, 4))
    p.add_argument("--max-catalog-pages", type=int, default=25)
    args = p.parse_args()
    if not 1 <= args.max_catalog_pages <= 100:
        p.error("--max-catalog-pages must be 1..100")
    try:
        return asyncio.run(run(args))
    except (CodexBridgeError, ValueError, OSError) as exc:
        print(json.dumps({"result": "FAIL", "error_type": type(exc).__name__,
            "message": "Could not complete resolver check. No transcript or raw exception body printed."}), file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
