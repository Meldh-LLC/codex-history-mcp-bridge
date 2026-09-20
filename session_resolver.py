"""Read-only, state-inventory-backed session selection. No transcript cache.

The reader remains in codex_bridge.py. This module verifies the selection and
wraps reader cursors in bounded process-local handles. Handles are not auth:
access control remains the MCP connection's responsibility.
"""
from __future__ import annotations

import asyncio
import copy
import ntpath
import os
import posixpath
import re
import secrets
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import codex_bridge as bridge

SESSION_PREFIX = "cbs1_"
CATALOG_PREFIX = "cbq1_"
CONTINUATION_PREFIX = "cbc1_"
CACHE_LIMIT = 4096
IDLE_SECONDS = 12 * 60 * 60
MAX_DISCOVERY_ROWS = 5000
MAX_SCAN_PAGES = 100
MAX_CANDIDATES_PER_CALL = 50
METADATA_TIMEOUT = 10.0
CALL_BUDGET = 40.0


class ResolutionError(bridge.CodexBridgeError):
    """Safe error with no automatic substitution of another thread."""


class HandleCache:
    def __init__(self, prefix: str, *, limit: int = CACHE_LIMIT, ttl: float = IDLE_SECONDS):
        self.prefix, self.limit, self.ttl = prefix, limit, ttl
        self._data: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._lock = threading.RLock()

    def put(self, state: dict[str, Any]) -> str:
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            token = self.prefix + secrets.token_hex(20)
            self._data[token] = (now, copy.deepcopy(state))
            while len(self._data) > self.limit:
                self._data.popitem(last=False)
        return token

    def get(self, token: str) -> dict[str, Any]:
        if not isinstance(token, str) or not re.fullmatch(re.escape(self.prefix) + r"[0-9a-f]{40}", token):
            raise ResolutionError("Invalid reference format. Copy the returned reference unchanged; do not supply a raw thread ID.")
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            entry = self._data.get(token)
            if entry is None:
                raise ResolutionError("Reference expired, was evicted, or belongs to another bridge process. Resolve the project again and start the affected session from page 1; do not combine abandoned chains.")
            self._data.move_to_end(token)
            self._data[token] = (now, entry[1])
            return copy.deepcopy(entry[1])

    def _prune(self, now: float) -> None:
        while self._data:
            key, (touched, _) = next(iter(self._data.items()))
            if now - touched < self.ttl:
                break
            self._data.pop(key)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


_SESSIONS = HandleCache(SESSION_PREFIX)
_CATALOGS = HandleCache(CATALOG_PREFIX)
_PAGES = HandleCache(CONTINUATION_PREFIX)


def _bool(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise ResolutionError(f"{name} must be a boolean.")
    return value


def _text(value: Any, name: str, *, optional: bool = False, max_len: int = 4096) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > max_len or any(ord(c) < 32 for c in value):
        raise ResolutionError(f"Invalid {name} metadata; not selectable.")
    return value.strip()


def canonical_path(value: str) -> str:
    """Lexical path equivalence only; never follow local symlinks or aliases."""
    path = _text(value, "path")
    assert path is not None
    if path.lower().startswith("file:"):
        url = urlsplit(path)
        if url.query or url.fragment or url.username or url.password:
            raise ResolutionError("Unsupported file URI in path.")
        path = unquote(url.path)
        if url.netloc and url.netloc != "localhost":
            path = "//" + url.netloc + path
        elif re.match(r"^/[A-Za-z]:/", path):
            path = path[1:]
    win = path.replace("\\", "/")
    if win.startswith("//?/UNC/") or win.startswith("//?/unc/"):
        win = "//" + win[8:]
    elif win.startswith("//?/"):
        win = win[4:]
    if re.match(r"^[A-Za-z]:/", win) or win.startswith("//"):
        return "win:" + ntpath.normpath(win).casefold()
    if path.startswith("/"):
        return "posix:" + posixpath.normpath(path)
    raise ResolutionError("Use an absolute project directory or a project name, not a relative path.")


def _is_path(value: str) -> bool:
    return value.startswith(("/", "\\", "~", ".")) or bool(re.match(r"^[A-Za-z]:", value)) or value.lower().startswith("file:")


def _home_key() -> str:
    # Changing CODEX_HOME must never silently retarget a cached reference.
    path = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    return os.path.normcase(os.path.abspath(os.path.expanduser(path)))


def _label(value: Any, default: str = "Untitled session") -> str:
    if not isinstance(value, str) or not value.strip():
        return default
    return re.sub(r"[\x00-\x1f\x7f]", " ", value)[:240]


def _source(thread: dict[str, Any]) -> tuple[str | None, str | None, bool]:
    kind = thread.get("sourceKind") or thread.get("source")
    thread_source = thread.get("threadSource")
    if thread_source is not None and not isinstance(thread_source, str):
        raise ResolutionError("Unknown thread-source metadata shape.")
    background = bool(thread.get("parentThreadId"))
    if isinstance(kind, dict):
        # Tagged source variants: only report their discriminant, never payload.
        background = True
        kind = "subAgent" if "subagent" in kind or "subAgent" in kind else "unknown"
    elif kind is not None and not isinstance(kind, str):
        raise ResolutionError("Unknown source-kind metadata shape.")
    if kind not in bridge.USER_FACING_SOURCE_KINDS:
        background = True
    if thread_source not in (None, "user"):
        background = True
    return kind, thread_source, background


def _identity(thread: Any, *, expected_id: str | None = None, cwd: str | None = None) -> dict[str, Any]:
    if not isinstance(thread, dict):
        raise ResolutionError("App Server did not return thread metadata.")
    tid = _text(thread.get("id"), "thread ID", max_len=160)
    if expected_id is not None and tid != expected_id:
        raise ResolutionError("Returned thread ID does not match the selected candidate.")
    directory = _text(thread.get("cwd"), "working directory")
    directory_key = canonical_path(directory)
    if cwd is not None and directory_key != canonical_path(cwd):
        raise ResolutionError("Returned working directory does not match the selected project.")
    if thread.get("ephemeral") is True:
        raise ResolutionError("Ephemeral threads are not selectable as stored history.")
    if thread.get("historyMode") not in ("legacy", "paginated"):
        raise ResolutionError("History mode is missing or unknown; no session reference issued.")
    kind, thread_source, background = _source(thread)
    created = thread.get("createdAt")
    if type(created) not in (int, float) or created < 0:
        raise ResolutionError("Creation timestamp is missing or invalid.")
    path = thread.get("path")
    path_key = canonical_path(path) if path else None
    name = thread.get("name")
    if name is not None:
        name = _text(name, "session name", optional=True)
    return {
        "thread_id": tid, "session_id": _text(thread.get("sessionId"), "session-tree ID", optional=True, max_len=160),
        "parent_thread_id": _text(thread.get("parentThreadId"), "parent ID", optional=True, max_len=160),
        "forked_from_id": _text(thread.get("forkedFromId"), "fork ID", optional=True, max_len=160),
        "name": name, "cwd": directory, "cwd_key": directory_key,
        "created_at": created, "updated_at": thread.get("updatedAt"),
        "history_mode": thread["historyMode"], "source_kind": kind,
        "thread_source": thread_source, "background": background,
        "rollout_path": path, "rollout_path_key": path_key,
    }


def _differences(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    # updated_at changes with new messages and is not a change of identity.
    # sourceKind/threadSource labels may be sparsely populated by listing.
    fields = ("thread_id", "session_id", "parent_thread_id", "forked_from_id", "cwd_key",
              "name", "created_at", "history_mode", "background", "rollout_path_key")
    return [key for key in fields if a.get(key) != b.get(key)]


def _descriptor(ref: str, state: dict[str, Any], diagnostics: bool = False) -> dict[str, Any]:
    i = state["identity"]
    out = {"session_ref": ref, "display_name": i["name"] or "Untitled session", "cwd": i["cwd"],
           "created_at": bridge._iso_timestamp(i["created_at"]), "updated_at": bridge._iso_timestamp(i["updated_at"]),
           "archived": state["archived"], "history_mode": i["history_mode"],
           "validation_status": "metadata_verified", "selectable": True,
           "history_readability": "not_probed", "background": i["background"],
           "is_fork": bool(i["forked_from_id"]), "has_parent": bool(i["parent_thread_id"]),
           "source_kind": i["source_kind"]}
    if diagnostics:
        out["diagnostics"] = {k: i[k] for k in ("thread_id", "session_id", "parent_thread_id", "forked_from_id", "rollout_path")}
    return out


async def _metadata(client: Any, tid: str) -> Any:
    reply = await client.request("thread/read", {"threadId": tid, "includeTurns": False}, timeout_seconds=METADATA_TIMEOUT)
    return reply.get("thread") if isinstance(reply, dict) else None


def _page(reply: Any) -> tuple[list[dict[str, Any]], str | None]:
    if not isinstance(reply, dict) or not isinstance(reply.get("data"), list) or any(not isinstance(x, dict) for x in reply["data"]):
        raise ResolutionError("Malformed state inventory response; completeness not established.")
    cursor = reply.get("nextCursor")
    if cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > 32768):
        raise ResolutionError("Malformed inventory continuation cursor.")
    if len(reply["data"]) > 100:
        raise ResolutionError("App Server exceeded the requested bounded inventory page.")
    return reply["data"], cursor


def _candidate_row(row: dict[str, Any]) -> dict[str, Any]:
    # Do not cache previews, turns, configuration, or arbitrary App Server extras.
    fields = ("id", "name", "cwd", "sessionId", "parentThreadId", "forkedFromId", "historyMode", "createdAt", "path")
    return {k: copy.deepcopy(row[k]) for k in fields if k in row}


async def _list(client: Any, *, cwd: str | None, archived: bool, background: bool, cursor: str | None, limit: int = 50) -> tuple[list[dict[str, Any]], str | None]:
    params: dict[str, Any] = {"limit": limit, "sortKey": "recency_at", "sortDirection": "desc",
        "archived": archived, "useStateDbOnly": True,
        "sourceKinds": bridge.ALL_SOURCE_KINDS if background else bridge.USER_FACING_SOURCE_KINDS}
    if cwd is not None:
        params["cwd"] = cwd
    if cursor is not None:
        params["cursor"] = cursor
    return _page(await client.request("thread/list", params, timeout_seconds=METADATA_TIMEOUT))


async def _project_choices(client: Any, project: str, include_archived: bool, include_background: bool) -> dict[str, Any]:
    choices: dict[str, dict[str, Any]] = {}
    rows, pages, complete = 0, 0, True
    deadline = time.monotonic() + CALL_BUDGET
    for archived in ([False, True] if include_archived else [False]):
        cursor = None
        seen = set()
        while True:
            if rows >= MAX_DISCOVERY_ROWS or pages >= MAX_SCAN_PAGES or time.monotonic() >= deadline:
                complete = False
                break
            data, next_cursor = await _list(client, cwd=None, archived=archived, background=include_background, cursor=cursor)
            pages += 1
            rows += len(data)
            for t in data:
                directory = t.get("cwd")
                try:
                    key = canonical_path(directory)
                except (ResolutionError, TypeError):
                    continue
                base = ntpath.basename(directory.rstrip("/\\")) if key.startswith("win:") else posixpath.basename(directory.rstrip("/"))
                if project.casefold() in base.casefold() or project.casefold() in directory.casefold():
                    group = choices.setdefault(key, {"project": directory, "display_name": base, "sessions_in_inventory_sample": 0})
                    group["sessions_in_inventory_sample"] += 1
            if next_cursor is None:
                break
            if next_cursor in seen:
                raise ResolutionError("Inventory cursor repeated. Project discovery is incomplete.")
            seen.add(next_cursor)
            cursor = next_cursor
        if not complete:
            break
    # Never silently pick a unique fuzzy/name match, particularly in a capped sample.
    return {"ok": True, "status": "choose_project" if choices else "project_not_found_in_inventory",
            "project_choices": sorted(choices.values(), key=lambda x: x["project"]), "sessions": [],
            "inventory_complete": complete, "inventory_rows_examined": rows,
            "instruction": "Ask the user to confirm the exact directory. Call again with that path; do not infer a directory alias. No session bodies were read."}


async def resolve_project_sessions(*, project: str, include_archived: bool = False, include_background: bool = False,
                                  limit: int = 20, page_token: str | None = None, diagnostics: bool = False) -> dict[str, Any]:
    """State-only discovery, metadata validation, isolated candidate failures."""
    project = _text(project, "project")
    _bool(include_archived, "include_archived"); _bool(include_background, "include_background"); _bool(diagnostics, "diagnostics")
    if type(limit) is not int or not 1 <= limit <= MAX_CANDIDATES_PER_CALL:
        raise ResolutionError("limit must be an integer from 1 to 50.")
    if page_token is not None:
        state = _CATALOGS.get(page_token)
        if state["home"] != _home_key() or state["options"] != [include_archived, include_background, diagnostics] or canonical_path(project) != state["cwd_key"]:
            raise ResolutionError("Inventory token belongs to a different project, Codex home, or filter set.")
    else:
        if not _is_path(project):
            async with bridge.CodexAppServerClient() as client:
                result = await _project_choices(client, project, include_archived, include_background)
            result["bridge_version"] = bridge.BRIDGE_VERSION
            return result
        key = canonical_path(project)
        state = {"home": _home_key(), "cwd": project, "cwd_key": key, "options": [include_archived, include_background, diagnostics],
                 "phase": 0, "cursor": None, "pending": [], "page_loaded": False,
                 "seen_ids": {}, "seen_cursors": [], "done": False, "rows": 0}
    selected, rejected, excluded = [], [], []
    count = 0
    deadline = time.monotonic() + CALL_BUDGET
    async with bridge.CodexAppServerClient() as client:
        while count < limit and not state["done"] and time.monotonic() < deadline:
            if not state["pending"]:
                if state["page_loaded"] and state["cursor"] is None:
                    if include_archived and state["phase"] == 0:
                        state["phase"] = 1; state["page_loaded"] = False
                    else:
                        state["done"] = True
                        break
                data, cursor = await _list(client, cwd=state["cwd"], archived=bool(state["phase"]), background=include_background,
                                           cursor=state["cursor"], limit=min(limit, 50))
                if cursor is not None:
                    ck = str(state["phase"]) + ":" + cursor
                    if ck in state["seen_cursors"]:
                        raise ResolutionError("Inventory cursor repeated; do not treat this catalog as complete.")
                    state["seen_cursors"].append(ck)
                state["cursor"], state["page_loaded"], state["pending"] = cursor, True, [_candidate_row(t) for t in data]
                if not data:
                    # Empty terminal pages are fine; empty nonterminal pages keep their cursor.
                    if len(state["seen_cursors"]) > MAX_SCAN_PAGES:
                        raise ResolutionError("Inventory scan bound reached; refine the project scope.")
                    continue
            row = state["pending"].pop(0)
            count += 1; state["rows"] += 1
            if state["rows"] > MAX_DISCOVERY_ROWS:
                raise ResolutionError("Project inventory exceeds the safety bound. No complete catalog is claimed.")
            label = _label(row.get("name"))
            tid = row.get("id")
            failure: dict[str, Any] = {"display_name": label, "archived": bool(state["phase"]), "selectable": False}
            if diagnostics:
                failure["candidate_thread_id"] = tid if isinstance(tid, str) and len(tid) <= 160 else None
            try:
                tid = _text(tid, "candidate thread ID", max_len=160)
                # Do not coalesce a family by sessionId. Dedupe ONLY exact thread ID.
                if tid in state["seen_ids"]:
                    reason = "active_archive_collision" if state["seen_ids"][tid] != state["phase"] else "duplicate_thread_id"
                    excluded.append({**failure, "reason": reason})
                    continue
                state["seen_ids"][tid] = state["phase"]
                if canonical_path(row.get("cwd")) != state["cwd_key"]:
                    raise ResolutionError("Inventory returned a candidate from another directory.")
                meta = await _metadata(client, tid)
                identity = _identity(meta, expected_id=tid, cwd=state["cwd"])
                # Listing may omit session tree/source fields; compare only present stable fields.
                if row.get("name") is not None and row.get("name") != identity["name"]:
                    raise ResolutionError("Candidate name changed between listing and validation. Refresh the catalog.")
                if row.get("createdAt") is not None and row["createdAt"] != identity["created_at"]:
                    raise ResolutionError("Candidate creation timestamp changed between listing and validation.")
                for source_key, identity_key in (("sessionId", "session_id"), ("parentThreadId", "parent_thread_id"),
                    ("forkedFromId", "forked_from_id"), ("historyMode", "history_mode")):
                    if row.get(source_key) is not None and row[source_key] != identity[identity_key]:
                        raise ResolutionError("Candidate identity changed between listing and validation: " + source_key)
                if row.get("path") and canonical_path(row["path"]) != identity["rollout_path_key"]:
                    raise ResolutionError("Candidate rollout path changed between listing and validation.")
                if not include_background and identity["background"]:
                    excluded.append({**failure, "reason": "background_or_unclassified_source"})
                    continue
                entry = {"identity": identity, "home": state["home"], "archived": bool(state["phase"]),
                         "include_background": include_background, "diagnostics": diagnostics}
                ref = _SESSIONS.put(entry)
                selected.append(_descriptor(ref, entry, diagnostics))
            except (bridge.CodexBridgeError, ValueError, OSError, asyncio.TimeoutError) as exc:
                failure.update({"reason": "metadata_validation_failed", "error_type": type(exc).__name__,
                    "detail": str(exc)[:500] if isinstance(exc, ResolutionError) else "App Server could not validate this candidate; no history read attempted."})
                rejected.append(failure)
    # Recognize a known terminal page even when the candidate limit was reached exactly.
    if not state["pending"] and state["page_loaded"] and state["cursor"] is None and (not include_archived or state["phase"] == 1):
        state["done"] = True
    token = None if state["done"] else _CATALOGS.put(state)
    return {"ok": True, "bridge_version": bridge.BRIDGE_VERSION, "status": "resolved", "project": state["cwd"],
            "inventory_source": "codex_state_database", "scan_legacy_logs": False, "include_archived": include_archived,
            "include_background": include_background, "inventory_complete": state["done"],
            "has_more_candidates": bool(token), "next_page_token": token,
            "sessions": selected, "rejected_candidates": rejected, "excluded_candidates": excluded,
            "inventory_rows_accounted_for": state["rows"], "session_contents_read": False,
            "validation_note": "References confirm metadata identity, not history readability or completeness. No raw ID is needed for normal reads. Duplicate titles are separate sessions.",
            "instruction": "Use codex_read_session_ref for verified references. Finish one session before the next; isolate errors. Continue this catalog while has_more_candidates=true. Legacy-only sessions are outside this state-only scope."}


async def _stale(entry: dict[str, Any], reason: str) -> dict[str, Any]:
    # Refresh for suggestions only: NEVER automatically redirect a read by title.
    replacement = None
    try:
        replacement = await resolve_project_sessions(project=entry["identity"]["cwd"], include_archived=entry["archived"],
            include_background=entry["include_background"], limit=20, diagnostics=entry["diagnostics"])
    except (bridge.CodexBridgeError, OSError, ValueError, asyncio.TimeoutError):
        pass
    choices = [] if replacement is None else [s for s in replacement.get("sessions", []) if s["display_name"] == (entry["identity"]["name"] or "Untitled session")]
    return {"ok": False, "is_error": True, "bridge_version": bridge.BRIDGE_VERSION, "status": "stale_session_reference",
            "history_complete": False, "history_restart_required": True, "reason": reason,
            "replacement_candidates": choices, "replacement_confirmation_required": bool(choices),
            "refresh_inventory_complete": replacement.get("inventory_complete") if replacement else False,
            "refresh_next_page_token": replacement.get("next_page_token") if replacement else None,
            "independent_sessions_may_continue": True,
            "instruction": "No transcript was returned. Ask the user to confirm any replacement, even a unique name match. Start its read without a token. Never merge the abandoned chain. Other independent sessions may continue."}


async def read_session_ref(*, session_ref: str, max_turns: int = 100, max_chars: int = 120000,
                           include_tool_output: bool = False, include_diffs: bool = False, page_token: str | None = None) -> dict[str, Any]:
    entry = _SESSIONS.get(session_ref)
    if entry["home"] != _home_key():
        raise ResolutionError("Codex home changed. Resolve the project again; no read attempted.")
    _bool(include_tool_output, "include_tool_output"); _bool(include_diffs, "include_diffs")
    if type(max_turns) is not int or not 1 <= max_turns <= 100 or type(max_chars) is not int or not 2000 <= max_chars <= 120000:
        raise ResolutionError("Use max_turns=1..100 and max_chars=2000..120000.")
    flags = [include_tool_output, include_diffs]
    wrapped = _PAGES.get(page_token) if page_token is not None else None
    if wrapped:
        if wrapped["session_ref"] != session_ref or wrapped["home"] != entry["home"] or wrapped["flags"] != flags:
            raise ResolutionError("Continuation belongs to another session, Codex home, or privacy selection. No read attempted.")
        max_turns = wrapped["max_turns"]
        page_number = wrapped["page_number"] + 1
    else:
        page_number = 1
        try:
            async with bridge.CodexAppServerClient() as client:
                current = _identity(await _metadata(client, entry["identity"]["thread_id"]), expected_id=entry["identity"]["thread_id"], cwd=entry["identity"]["cwd"])
            differences = _differences(entry["identity"], current)
            if differences:
                return await _stale(entry, "Metadata changed: " + ", ".join(differences))
        except (bridge.CodexBridgeError, OSError, ValueError, asyncio.TimeoutError):
            return await _stale(entry, "The selected identity could not be revalidated.")
    result = await bridge.read_session(thread_id=entry["identity"]["thread_id"], max_turns=max_turns, max_chars=max_chars,
        include_tool_output=include_tool_output, include_diffs=include_diffs,
        page_token=wrapped["reader_token"] if wrapped else None)
    if not isinstance(result, dict) or result.get("thread", {}).get("id") != entry["identity"]["thread_id"]:
        raise ResolutionError("Reader returned a different or missing thread identity. No history is returned to the caller.")
    if result.get("is_error") or result.get("ok") is False:
        return result
    returned_cwd = result.get("thread", {}).get("cwd")
    if returned_cwd is not None and canonical_path(returned_cwd) != entry["identity"]["cwd_key"]:
        raise ResolutionError("Reader metadata moved to another project after validation. No history is returned to the caller.")
    native_token = result.get("next_page_token")
    if type(result.get("has_more_content")) is not bool or result["has_more_content"] != bool(native_token):
        raise ResolutionError("Reader continuation flag/token mismatch; no completion claim made.")
    out = copy.deepcopy(result)
    out["next_page_token"] = _PAGES.put({"session_ref": session_ref, "reader_token": native_token, "home": entry["home"],
        "flags": flags, "max_turns": max_turns, "page_number": page_number}) if native_token else None
    # Never expose upstream cursors in the ordinary flow, including the unused reverse cursor.
    out["newer_page_token"] = None
    out["session_ref"] = session_ref
    out["resolved_session"] = _descriptor(session_ref, entry, entry["diagnostics"])
    out["accepted_page_number"] = page_number
    out["reference_note"] = "cbs1/cbq1/cbc1 references are process-local (12-hour idle expiry, bounded cache). Keep the bridge running. A token is a bookmark, not evidence or authorization."
    out["pagination_note"] = "Continue this same session_ref with next_page_token as page_token while has_more_content=true. Preserve privacy flags. If history_restart_required=true, replace earlier source coverage. On error mark only this session partial and continue other independently resolved sessions. Cite thread/item identifiers as evidence, never reconstruct them as inputs."
    return out


REPORT_GUIDE = {
    "preferred_format": "docx",
    "chat_summary_words": 150,
    "layout": ["One-page overview: historical cutoff, scope, key state, most important unresolved issue, next actions.",
               "Short thematic sections: completed/reported verification; claimed but unverified; proposals; failures; decisions.",
               "Evidence and coverage appendix: source key, session IDs and item references, page counts, terminal flags, omissions, unread scope."],
    "instructions": "Create an actual downloadable Word document using the chat's document/file tools. Use heading styles, readable spacing, page numbers, compact tables only for comparisons, and a contents list for a long report. Keep qualifications and evidence; do not replace them with stronger wording. Keep raw coverage JSON and long IDs out of the overview. Do not re-read already completed history merely to format it. In chat return the file link, a brief finding, and unresolved blockers, normally under 150 words. Never invent a file link. If document tools are unavailable, state that and provide document-ready Markdown rather than claim a DOCX was created.",
    "scope_note": "The MCP supplies history and this guide. It does not write reports on the user's computer, run Word, or independently verify current project state. Document creation is performed by the consuming chat with its own available tools."
}


def report_guide() -> dict[str, Any]:
    result = copy.deepcopy(REPORT_GUIDE)
    result["scope_and_search"] = "Include confirmed roots, roots not searched or incompletely inventoried, archive/background filters, candidate failures, and discovery search coverage in the appendix. A single cwd can contain multiple sessions; never deduplicate by title or family ID. Keyword search is not conceptual completeness or full transcript delivery. Use short evidence aliases in the body, with durable names/dates/cwd and source item locators in the appendix; process-local handles are not permanent citations."
    result["evidence_reconciliation"] = "Preserve chronology: later measurements or explicit decisions supersede earlier snapshots only where the record supports that. Do not rebrand a past assistant suggestion as a user decision, or choose an early metric as the latest one. No current-state checks are implied."
    return result
