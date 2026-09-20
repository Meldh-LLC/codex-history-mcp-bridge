"""Read-only schema inventory for one local Codex rollout; no payloads are printed.

This audits record classification, not retrieval of all projected text. It does
not invoke Codex/App Server or the tunnel and does not force an automatic fallback.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import sys
from typing import Any

import rollout_fallback as rf
from codex_bridge import BRIDGE_VERSION


def audit(thread_id: str, *, max_records: int = 2_000_000,
          include_tool_output: bool = False, include_diffs: bool = False) -> dict[str, Any]:
    if type(max_records) is not int or max_records < 1:
        raise ValueError("max_records must be positive")
    path = rf.find_rollout(thread_id)
    counts: Counter[str] = Counter()
    examples: dict[str, dict[str, Any]] = {}
    stats = rf.empty_stats()
    with path.open("rb") as handle:
        snapshot = rf.identity(handle)
        scanner = rf.Scanner(handle, snapshot["size"])
        first = scanner.record()
        if first is None or rf.small(first[2].get("type")) != "session_meta" or not isinstance(first[2].get("payload"), dict) or rf.small(first[2]["payload"].get("id")).lower() != thread_id.lower():
            raise rf.RolloutReadError("Rollout ownership metadata did not match the requested session.")
        meta = first[2]["payload"]
        if any(meta.get(k) is not None for k in ("forked_from_rollout", "forked_from_rollout_id", "forked_from_ordinal", "forked_from_ordinal_exclusive", "rollout_reference", "history_base")):
            raise rf.RolloutReadError("Referenced history requires Codex materialization; this single-file audit will not claim complete coverage.")
        entry = first
        while entry is not None and stats["records"] < max_records:
            start, _, record = entry
            kind, _, _, info = rf.project(record, include_tool_output=include_tool_output, include_diffs=include_diffs)
            rf.account_record(stats, kind, info)
            label = info["source_record_type"]
            if label not in counts and len(counts) >= 256:
                label = "additional_type_names_not_listed"
            counts[label] += 1
            if info.get("unrecognized_types") or info.get("projection_gaps"):
                if label not in examples and len(examples) < 32:
                    payload = record["payload"]
                    item = payload.get("item")
                    examples[label] = {"first_record_byte": start,
                        "payload_field_names": sorted(rf._label(k) for k in payload)[:40],
                        "item_field_names": sorted(rf._label(k) for k in item)[:40] if isinstance(item, dict) else [],
                        "unknown_types": info.get("unrecognized_types", []),
                        "projection_gaps": info.get("projection_gaps", []),
                        "values_exposed": False}
            if stats["records"] == max_records:
                break
            entry = scanner.record()
        scanner.spaces(lines=True)
        eof = scanner.peek() is None
        if rf.identity(handle) != snapshot or rf.stat_identity(path.stat()) != snapshot:
            raise rf.RolloutReadError("Rollout changed during the inventory; retry when the session is idle.")
    report = rf.coverage_report(stats, eof)
    support_complete = report.pop("projection_coverage_complete")
    report["projection_coverage_complete"] = None  # Text fragments were not retrieved.
    report["record_support_complete"] = support_complete
    return {"bridge_version": BRIDGE_VERSION,
            "result": "AUDIT_FINISHED" if eof else "AUDIT_STOPPED_AT_LIMIT",
            "thread_id": thread_id, "source_file_bytes": snapshot["size"],
            "inventory_only": True, "payloads_exposed": False,
            "text_retrieval_tested": False, "counts_by_record_type": dict(sorted(counts.items())),
            "coverage_report": report, "diagnostic_examples": examples,
            "note": "This inventoried record types and structural fields, not transcript text. Use verify_pagination.py to test retrieval. An EOF inventory is not semantic or repository-state verification."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--max-records", type=int, default=2_000_000)
    parser.add_argument("--include-tool-output", action="store_true")
    parser.add_argument("--include-diffs", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.max_records <= 10_000_000:
        parser.error("--max-records must be between 1 and 10000000")
    try:
        result = audit(args.thread_id, max_records=args.max_records,
                       include_tool_output=args.include_tool_output, include_diffs=args.include_diffs)
    except (rf.RolloutReadError, ValueError, OSError) as exc:
        error = str(exc) if isinstance(exc, (rf.RolloutReadError, ValueError)) else "Cannot read the local rollout; check filesystem access."
        print(json.dumps({"result": "FAIL", "error_type": type(exc).__name__, "error": error}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
