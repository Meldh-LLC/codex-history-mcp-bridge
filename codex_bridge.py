from __future__ import annotations

import asyncio
import base64
import binascii
import copy
import hashlib
import re
import json
import logging
import os
import shutil
import subprocess
import sys
from collections import defaultdict, deque
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BRIDGE_VERSION = "0.3.0"

import rollout_fallback
STREAM_LIMIT = 16 * 1024 * 1024
READ_METHODS = frozenset({"thread/list", "thread/read", "thread/turns/list", "thread/items/list", "thread/search", "thread/searchOccurrences"})

LOGGER = logging.getLogger("codex_history_bridge")

USER_FACING_SOURCE_KINDS = ["cli", "vscode", "appServer"]

# Current App Server ThreadItem variants plus bridge-generated/legacy aliases.
# Keep this centralized so history projection and cross-session search cannot
# silently drift apart. Unknown future variants remain explicit coverage gaps.
NATIVE_THREAD_ITEM_TYPES = frozenset({
    "userMessage", "hookPrompt", "agentMessage", "functionCallOutput", "plan",
    "commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall",
    "collabAgentToolCall", "collabToolCall", "subAgentActivity", "webSearch",
    "imageView", "sleep", "imageGeneration", "enteredReviewMode",
    "exitedReviewMode", "contextCompaction", "turnMetadata",
})

ALL_SOURCE_KINDS = [
    "cli",
    "vscode",
    "exec",
    "appServer",
    "subAgent",
    "subAgentReview",
    "subAgentCompact",
    "subAgentThreadSpawn",
    "subAgentOther",
    "unknown",
]


class CodexBridgeError(RuntimeError):
    """Base exception for bridge failures."""


class CodexNotFoundError(CodexBridgeError):
    """Raised when the Codex CLI cannot be located."""


class CodexAppServerError(CodexBridgeError):
    """Raised when Codex app-server returns an error or exits unexpectedly."""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def _clip(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            value = str(value)
    if len(value) <= limit:
        return value
    omitted = len(value) - limit
    return f"{value[:limit]}\n… [{omitted} characters omitted]"


def _iso_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    try:
        numeric = float(value)
        # Be tolerant if a future schema sends milliseconds.
        if numeric > 10_000_000_000:
            numeric /= 1000.0
        return datetime.fromtimestamp(numeric, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return str(value)


def resolve_codex_executable() -> str:
    configured = os.environ.get("CODEX_EXECUTABLE", "").strip().strip('"')
    if configured:
        candidate = Path(os.path.expandvars(os.path.expanduser(configured)))
        if candidate.exists():
            return str(candidate.resolve())
        located = shutil.which(configured)
        if located:
            return located
        raise CodexNotFoundError(
            f"CODEX_EXECUTABLE points to '{configured}', but that file/command was not found."
        )

    located = shutil.which("codex")
    if located:
        return located

    raise CodexNotFoundError(
        "The 'codex' command was not found on PATH. Install/update the Codex CLI, "
        "or set CODEX_EXECUTABLE to the full path of codex.exe/codex/codex.cmd."
    )


def build_codex_command(*extra_args: str) -> list[str]:
    executable = resolve_codex_executable()
    arguments = [executable, *extra_args]

    # npm global installs commonly expose .cmd shims on Windows. CreateProcess
    # cannot reliably launch them directly, so route those through cmd.exe.
    if os.name == "nt" and Path(executable).suffix.lower() in {".cmd", ".bat"}:
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        command_line = subprocess.list2cmdline(arguments)
        return [comspec, "/d", "/s", "/c", command_line]

    return arguments


def get_codex_version(timeout_seconds: float = 10.0) -> dict[str, Any]:
    executable = resolve_codex_executable()
    command = build_codex_command("--version")
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
    )
    return {
        "executable": executable,
        "command": command,
        "return_code": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


class CodexAppServerClient:
    """Small async client for the documented Codex app-server stdio protocol."""

    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=80)
        self._request_lock = asyncio.Lock()

    async def __aenter__(self) -> "CodexAppServerClient":
        try:
            await self.start()
        except BaseException:
            await self.close()
            raise
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    async def start(self) -> None:
        if self.process and self.process.returncode is None:
            return

        command = build_codex_command("app-server", "--listen", "stdio://")
        LOGGER.debug("Starting Codex app-server: %s", command)
        try:
            self.process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=os.environ.copy(),
                limit=STREAM_LIMIT,
            )
        except FileNotFoundError as exc:
            raise CodexNotFoundError(str(exc)) from exc
        except OSError as exc:
            raise CodexAppServerError(f"Could not start Codex app-server: {exc}") from exc

        self._stderr_task = asyncio.create_task(self._drain_stderr())

        initialize_result = await self._request_raw(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex_history_mcp_bridge",
                    "title": "Codex History MCP Bridge",
                    "version": BRIDGE_VERSION,
                },
                "capabilities": {
                    "experimentalApi": True,
                    "optOutNotificationMethods": [
                        "thread/started",
                        "thread/status/changed",
                        "item/agentMessage/delta",
                        "item/plan/delta",
                        "item/reasoning/summaryTextDelta",
                        "item/reasoning/summaryPartAdded",
                        "item/reasoning/textDelta",
                        "item/commandExecution/outputDelta",
                    ],
                },
            },
            timeout_seconds=30.0,
        )
        LOGGER.debug("Codex app-server initialized: %s", initialize_result)
        await self.notify("initialized", {})

    async def _drain_stderr(self) -> None:
        process = self.process
        if not process or not process.stderr:
            return
        try:
            while True:
                raw = await process.stderr.readline()
                if not raw:
                    break
                text = raw.decode("utf-8", errors="replace").rstrip()
                self._stderr_tail.append(text)
                LOGGER.debug("codex app-server stderr: %s", text)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Failed while reading Codex app-server stderr")

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._write_message({"method": method, "params": params or {}})

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_seconds: float = 90.0,
    ) -> Any:
        if method not in READ_METHODS:
            raise CodexBridgeError(f"Read-only bridge refuses method: {method}")
        if not self.process or self.process.returncode is not None:
            await self.start()
        return await self._request_raw(method, params or {}, timeout_seconds=timeout_seconds)

    async def _request_raw(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> Any:
        async with self._request_lock:
            request_id = self._next_id
            self._next_id += 1
            await self._write_message({"method": method, "id": request_id, "params": params})

            try:
                return await asyncio.wait_for(
                    self._read_until_response(request_id), timeout=timeout_seconds
                )
            except asyncio.TimeoutError as exc:
                raise CodexAppServerError(
                    f"Timed out after {timeout_seconds:g}s waiting for '{method}'."
                ) from exc

    async def _write_message(self, message: dict[str, Any]) -> None:
        process = self.process
        if not process or not process.stdin or process.returncode is not None:
            raise CodexAppServerError(self._process_exit_message())
        wire = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        process.stdin.write(wire.encode("utf-8"))
        try:
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise CodexAppServerError(self._process_exit_message()) from exc

    async def _read_until_response(self, request_id: int) -> Any:
        process = self.process
        if not process or not process.stdout:
            raise CodexAppServerError("Codex app-server stdout is unavailable.")

        while True:
            try:
                raw = await process.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError) as exc:
                raise CodexAppServerError(
                    "Codex response exceeded the 16 MiB stream-line limit. "
                    "No history was silently truncated. Start a new read with fewer max_turns where turn paging is supported; "
                    "an individual item or required legacy whole-history response above this limit needs a different read strategy."
                ) from exc
            if not raw:
                raise CodexAppServerError(self._process_exit_message())
            try:
                message = json.loads(raw.decode("utf-8", errors="strict"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CodexAppServerError(
                    f"Codex app-server emitted invalid UTF-8/JSON ({len(raw)} bytes). Raw content was not included in this error."
                ) from exc
            if not isinstance(message, dict):
                raise CodexAppServerError("Codex app-server emitted a non-object JSON message.")

            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    error = message.get("error") or {}
                    code = error.get("code")
                    detail = error.get("message") or str(error)
                    raise CodexAppServerError(
                        f"Codex app-server request failed ({code}): {detail}", code=code
                    )
                return message.get("result")

            # This read-only client should never receive approval or elicitation
            # requests. Fail closed instead of accidentally authorizing anything.
            if "id" in message and "method" in message:
                await self._write_message(
                    {
                        "id": message["id"],
                        "error": {
                            "code": -32601,
                            "message": "Read-only bridge does not service server-initiated requests.",
                        },
                    }
                )
                continue

            # Notifications are irrelevant for list/read calls.
            LOGGER.debug("Ignoring Codex app-server notification: %s", message.get("method"))

    def _process_exit_message(self) -> str:
        process = self.process
        code = process.returncode if process else None
        return f"Codex app-server exited or disconnected (return code: {code}). Raw stderr is not included in tool errors."

    async def close(self) -> None:
        process = self.process
        self.process = None
        try:
            if process:
                if process.stdin:
                    with suppress(Exception):
                        process.stdin.close()
                    with suppress(Exception):
                        await asyncio.wait_for(process.stdin.wait_closed(), timeout=1.0)
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=3.0)
                    except asyncio.TimeoutError:
                        with suppress(ProcessLookupError):
                            process.kill()
                        with suppress(Exception):
                            await process.wait()
                else:
                    with suppress(Exception):
                        await process.wait()
        finally:
            if self._stderr_task:
                self._stderr_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await self._stderr_task
                self._stderr_task = None


def _thread_summary(thread: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": thread.get("id"),
        "name": thread.get("name"),
        "preview": _clip(thread.get("preview", ""), 1200),
        "cwd": thread.get("cwd"),
        "created_at": _iso_timestamp(thread.get("createdAt")),
        "updated_at": _iso_timestamp(thread.get("updatedAt")),
        "recency_at": _iso_timestamp(thread.get("recencyAt")),
        "model_provider": thread.get("modelProvider"),
        "source_kind": thread.get("sourceKind") or thread.get("source"),
        "is_pinned": thread.get("isPinned"),
        "ephemeral": thread.get("ephemeral"),
        "git_info": thread.get("gitInfo"),
        "status": thread.get("status"),
    }


async def list_sessions(
    *,
    query: str | None = None,
    cwd: str | None = None,
    archived: bool = False,
    include_background: bool = False,
    scan_legacy_logs: bool = False,
    limit: int = 20,
    cursor: str | None = None,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), 100))
    params: dict[str, Any] = {
        "limit": limit,
        "sortKey": "recency_at",
        "sortDirection": "desc",
        "archived": bool(archived),
        # State-only listing avoids App Server's optional scan-and-repair pass.
        "useStateDbOnly": not bool(scan_legacy_logs),
    }
    if cursor:
        params["cursor"] = cursor
    if query:
        params["searchTerm"] = query
    if cwd:
        params["cwd"] = cwd
    if include_background:
        params["sourceKinds"] = ALL_SOURCE_KINDS
    else:
        params["sourceKinds"] = USER_FACING_SOURCE_KINDS

    async with CodexAppServerClient() as client:
        result = await client.request("thread/list", params)

    data = (result or {}).get("data", [])
    return {
        "sessions": [_thread_summary(item) for item in data],
        "next_cursor": (result or {}).get("nextCursor"),
        "filters": {
            "query": query,
            "cwd": cwd,
            "archived": archived,
            "include_background": include_background,
            "scan_legacy_logs": scan_legacy_logs,
        },
        "notes": [
            "query matches Codex's extracted thread title, not every transcript message.",
            "Pass next_cursor back to this tool to continue pagination.",
        ],
    }


async def list_projects(
    *,
    archived: bool = False,
    include_background: bool = False,
    scan_legacy_logs: bool = False,
    max_sessions: int = 200,
) -> dict[str, Any]:
    max_sessions = max(1, min(int(max_sessions), 500))
    source_kinds = ALL_SOURCE_KINDS if include_background else USER_FACING_SOURCE_KINDS
    collected: list[dict[str, Any]] = []
    cursor: str | None = None

    async with CodexAppServerClient() as client:
        while len(collected) < max_sessions:
            page_limit = min(100, max_sessions - len(collected))
            params: dict[str, Any] = {
                "limit": page_limit,
                "sortKey": "recency_at",
                "sortDirection": "desc",
                "archived": bool(archived),
                "useStateDbOnly": not bool(scan_legacy_logs),
                "sourceKinds": source_kinds,
            }
            if cursor:
                params["cursor"] = cursor
            result = await client.request("thread/list", params)
            page = (result or {}).get("data", [])
            collected.extend(page)
            cursor = (result or {}).get("nextCursor")
            if not cursor or not page:
                break

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for thread in collected:
        cwd = thread.get("cwd") or "(no working directory recorded)"
        groups[str(cwd)].append(thread)

    projects: list[dict[str, Any]] = []
    for cwd, threads in groups.items():
        newest = max(
            threads,
            key=lambda item: item.get("recencyAt")
            or item.get("updatedAt")
            or item.get("createdAt")
            or 0,
        )
        projects.append(
            {
                "cwd": cwd,
                "session_count_in_sample": len(threads),
                "latest_session_id": newest.get("id"),
                "latest_session_name": newest.get("name"),
                "latest_session_preview": _clip(newest.get("preview", ""), 500),
                "latest_activity": _iso_timestamp(
                    newest.get("recencyAt")
                    or newest.get("updatedAt")
                    or newest.get("createdAt")
                ),
            }
        )

    projects.sort(key=lambda item: item.get("latest_activity") or "", reverse=True)
    return {
        "projects": projects,
        "sessions_examined": len(collected),
        "more_sessions_exist": cursor is not None,
        "archived": archived,
        "include_background": include_background,
        "scan_legacy_logs": scan_legacy_logs,
    }


# ---- Lossless visible-content pagination (bridge token version 2) ----

PAGE_TOKEN_PREFIX = "cb2_"
MAX_PAGE_TOKEN_CHARS = 32_768
_DATA_URI = re.compile(r"data:[^,\s]*,[^\s]*", re.I)
_BINARY_MARKER = "[binary/image payload omitted]"


def _native_kind(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 256:
        return ""
    return re.sub(r"[_-]", "", value).lower()


def _is_patch_tool(value: Any) -> bool:
    return isinstance(value, str) and value.casefold() in {"apply_patch", "functions.apply_patch"}


def _native_status_projection(value: Any, include_payload: bool) -> Any:
    """Status tags are public metadata; completion/error bodies are tool output."""
    if include_payload:
        return _sanitize_native_value(value)
    if not isinstance(value, dict):
        return value
    return {key: "[agent status payload omitted; include_tool_output=true]" for key in value}


def _sanitize_native_value(value: Any, *, _depth: int = 0) -> Any:
    """Apply the fallback reader's unconditional privacy rules to native JSON."""
    if _depth > rollout_fallback.MAX_DEPTH:
        raise CodexBridgeError("Native tool payload nesting exceeds the privacy-filter limit.")
    if isinstance(value, str):
        return _DATA_URI.sub(_BINARY_MARKER, value)
    if isinstance(value, list):
        return [_sanitize_native_value(one, _depth=_depth + 1) for one in value]
    if not isinstance(value, dict):
        return value

    kind = _native_kind(value.get("type"))
    if (kind.startswith(("reasoning", "agentreasoning"))
            or _native_kind(value.get("role")) in {"system", "developer"}
            or _native_kind(value.get("channel")) in {"analysis", "reasoning"}
            or _native_kind(value.get("phase")) in {"analysis", "reasoning"}):
        return "[reasoning omitted]"
    if kind in {
        "image", "inputimage", "outputimage", "imagegeneration",
        "imagegenerationcall", "audio", "inputaudio", "outputaudio",
        "resource", "resourcelink",
    }:
        return _BINARY_MARKER

    sanitized: dict[str, Any] = {}
    for key, child in value.items():
        lowered = key.lower()
        if lowered in rollout_fallback.PRIVATE_KEYS:
            continue
        binary_like = (
            lowered in {"data", "blob", "bytes", "base64_data"}
            and isinstance(child, str)
            and len(child) >= 16_384
            and re.fullmatch(r"[A-Za-z0-9+/=_-]+", child) is not None
        )
        if (binary_like or lowered in rollout_fallback.BINARY_KEYS
                or lowered in {"image_url", "imageurl", "image", "images"}):
            sanitized[key] = _BINARY_MARKER
        else:
            sanitized[key] = _sanitize_native_value(child, _depth=_depth + 1)
    return sanitized


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _pack_read_token(state: dict[str, Any]) -> str:
    """Encode navigation state only: never put transcript text or credentials in a token.

    The checksum detects accidental corruption; it is not authentication. Tokens
    do not grant access and must still be validated against this read request.
    """
    raw = json.dumps(state, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    token = PAGE_TOKEN_PREFIX + body + "." + hashlib.sha256(raw).hexdigest()[:24]
    if len(token) > MAX_PAGE_TOKEN_CHARS:
        raise CodexBridgeError("Continuation state exceeds the token size limit; no content was silently discarded.")
    return token


def _unpack_read_token(token: str, thread_id: str, include_tool_output: bool, include_diffs: bool) -> dict[str, Any]:
    if not isinstance(token, str):
        raise ValueError("page_token must be the string returned as next_page_token.")
    if token.startswith("cb1_"):
        raise ValueError("This is a pre-0.2.3 token. Restart this session read without page_token; old tokens cannot recover text omitted by the old renderer.")
    if len(token) > MAX_PAGE_TOKEN_CHARS or not token.startswith(PAGE_TOKEN_PREFIX):
        raise ValueError("Invalid page_token. Copy next_page_token exactly; do not decode or edit it.")
    try:
        encoded, checksum = token[len(PAGE_TOKEN_PREFIX):].split(".")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", encoded):
            raise ValueError("Invalid token alphabet")
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        if not re.fullmatch(r"[0-9a-f]{24}", checksum) or hashlib.sha256(raw).hexdigest()[:24] != checksum:
            raise ValueError("Invalid checksum")
        state = json.loads(raw.decode("ascii"))
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise ValueError("Corrupted page_token. Copy next_page_token exactly.") from exc
    if not isinstance(state, dict) or state.get("v") != 2 or state.get("kind") != "read":
        raise ValueError("page_token is for another operation or version.")
    if state.get("thread_id") != thread_id:
        raise ValueError("page_token belongs to a different Codex session.")
    if state.get("options") != {"tool_output": include_tool_output, "diffs": include_diffs}:
        raise ValueError("Keep include_tool_output and include_diffs unchanged while continuing a read; otherwise restart without page_token.")
    if state.get("backend") not in {"paginated", "legacy_turns", "legacy_full"}:
        raise ValueError("Invalid history backend in page_token.")
    if type(state.get("limit")) is not int or not 1 <= state["limit"] <= 100:
        raise ValueError("Invalid turn-page size in page_token.")
    cursor = state.get("cursor")
    if cursor is not None and (not isinstance(cursor, str) or not cursor):
        raise ValueError("Invalid Codex cursor in page_token.")
    end = state.get("legacy_end")
    if end is not None and (type(end) is not int or end < 0):
        raise ValueError("Invalid legacy boundary in page_token.")
    if state["backend"] == "legacy_full" and (end is None or cursor is not None):
        raise ValueError("Invalid legacy navigation state in page_token.")
    if state["backend"] != "legacy_full" and end is not None:
        raise ValueError("Invalid turn navigation state in page_token.")
    position = state.get("position")
    if not isinstance(position, dict) or set(position) != {"turn", "item", "field", "text"}:
        raise ValueError("Invalid content position in page_token.")
    if any(type(n) is not int or n < 0 for n in position.values()):
        raise ValueError("Invalid content offset in page_token.")
    fingerprint = state.get("fingerprint")
    if fingerprint is not None and (not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)):
        raise ValueError("Invalid page fingerprint in page_token.")
    if fingerprint is None and any(position.values()):
        raise ValueError("An in-page continuation requires a page fingerprint.")
    return state


def _visible_user_content(content: Any) -> str:
    """Keep visible text in full; attachments are references, not downloaded pixels."""
    if not isinstance(content, list):
        value = content if isinstance(content, str) else _json_text(content)
        return _sanitize_native_value(value)
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            value = block if isinstance(block, str) else _json_text(block)
            parts.append(_sanitize_native_value(value))
            continue
        kind = block.get("type")
        if kind == "text":
            value = block.get("text", "")
            value = value if isinstance(value, str) else _json_text(value)
            parts.append(_sanitize_native_value(value))
        elif kind == "image":
            url = str(block.get("url") or "")
            parts.append("[inline image omitted]" if url.lower().startswith("data:") else f"[image: {url}]")
        elif kind == "localImage":
            parts.append(f"[local image: {block.get('path') or ''}]")
        elif kind in {"skill", "mention"}:
            parts.append(f"[{kind}: {block.get('name') or ''} {block.get('path') or ''}]")
        else:
            parts.append(f"[{kind or 'input'} omitted: unsupported non-text input]")
    return "\n".join(parts)


def _project_item(item: dict[str, Any], *, include_tool_output: bool, include_diffs: bool) -> dict[str, Any] | None:
    """Explicit privacy allowlist. No per-message clipping and no raw-object fallback."""
    kind = item.get("type", "unknown")
    if kind == "reasoning":
        return None
    common = {"type": kind, "id": item.get("id")}
    if kind == "userMessage":
        return {**common, "role": "user", "text": _visible_user_content(item.get("content", []))}
    if kind == "hookPrompt":
        # Hook prompt fragments are internal injected context, not ordinary user
        # conversation. Recognize the item without exposing its content.
        return {**common, "note": "Hook prompt content omitted by privacy policy."}
    if kind == "agentMessage":
        if (_native_kind(item.get("phase")) in {"analysis", "reasoning"}
                or _native_kind(item.get("role")) in {"system", "developer"}
                or _native_kind(item.get("channel")) in {"analysis", "reasoning"}):
            return None
        return {**common, "role": "assistant", "phase": item.get("phase"), "text": _sanitize_native_value(item.get("text", ""))}
    if kind == "plan":
        return {**common, "role": "plan", "text": _sanitize_native_value(item.get("text", ""))}
    if kind == "commandExecution":
        out = {**common, "command": item.get("command", ""), "cwd": item.get("cwd"), "status": item.get("status"), "exit_code": item.get("exitCode"), "duration_ms": item.get("durationMs")}
        if include_tool_output and item.get("aggregatedOutput") is not None:
            out["output"] = _sanitize_native_value(item["aggregatedOutput"])
        return out
    if kind == "fileChange":
        changes = []
        for change in item.get("changes") or []:
            if not isinstance(change, dict):
                raise CodexBridgeError("Malformed file-change entry; refusing to skip history content.")
            one = {"path": change.get("path"), "kind": change.get("kind")}
            if include_diffs and change.get("diff") is not None:
                one["diff"] = _sanitize_native_value(change["diff"])
            changes.append(one)
        return {**common, "status": item.get("status"), "changes": changes}
    if kind == "mcpToolCall":
        out = {**common, "server": item.get("server"), "tool": item.get("tool"), "status": item.get("status"), "app_context": item.get("appContext")}
        payload_allowed = include_diffs if _is_patch_tool(item.get("tool")) else include_tool_output
        if payload_allowed:
            for key in ("arguments", "result", "error"):
                if item.get(key) is not None:
                    out[key] = _sanitize_native_value(item[key])
        return out
    if kind == "dynamicToolCall":
        out = {**common, "tool": item.get("tool"), "status": item.get("status"), "success": item.get("success"), "duration_ms": item.get("durationMs")}
        payload_allowed = include_diffs if _is_patch_tool(item.get("tool")) else include_tool_output
        if payload_allowed:
            out["arguments"] = _sanitize_native_value(item.get("arguments"))
            if item.get("contentItems") is not None:
                out["content"] = _sanitize_native_value(item["contentItems"])
        return out
    if kind == "functionCallOutput":
        out = {**common, "name": item.get("name"), "namespace": item.get("namespace")}
        if include_tool_output:
            out["output"] = _sanitize_native_value(item.get("output"))
        return out
    if kind == "webSearch":
        return {**common, "query": item.get("query", ""), "action": item.get("action")}
    if kind in {"collabAgentToolCall", "collabToolCall"}:
        # Prompts and linked-agent content are deliberately not projected.
        out = {**common, "tool": item.get("tool"), "status": item.get("status"),
            "sender_thread_id": item.get("senderThreadId"),
            "receiver_thread_ids": item.get("receiverThreadIds"),
            "receiver_thread_id": item.get("receiverThreadId"),
            "new_thread_id": item.get("newThreadId")}
        if item.get("agentStatus") is not None:
            out["agent_status"] = _native_status_projection(item["agentStatus"], include_tool_output)
        return out
    if kind == "subAgentActivity":
        return {**common, "kind": item.get("kind"),
            "agent_thread_id": item.get("agentThreadId"),
            "agent_path": item.get("agentPath")}
    if kind == "imageView":
        return {**common, "path": item.get("path")}
    if kind == "sleep":
        return {**common, "duration_ms": item.get("durationMs")}
    if kind == "imageGeneration":
        # Never expose the generated result bytes. Revised prompts are tool
        # payload and remain opt-in; safe status/failure metadata is retained.
        out = {**common, "status": item.get("status"),
            "saved_path": item.get("savedPath"),
            "transparent_background": item.get("transparentBackground")}
        failure = item.get("failure")
        if isinstance(failure, dict):
            out["failure"] = {k: failure.get(k) for k in ("code", "message") if failure.get(k) is not None}
        if include_tool_output and item.get("revisedPrompt") is not None:
            out["revised_prompt"] = _sanitize_native_value(item.get("revisedPrompt"))
        return out
    if kind in {"enteredReviewMode", "exitedReviewMode"}:
        return {**common, "review": item.get("review", "")}
    if kind == "contextCompaction":
        return {**common, "note": "Codex compacted the conversation context here."}
    return {**common, "note": "Unsupported item type: raw payload intentionally not exposed."}


def _prepare_visible_page(turns: list[dict[str, Any]], *, include_tool_output: bool, include_diffs: bool) -> tuple[list[dict[str, Any]], int]:
    projected = []
    reasoning_excluded = 0
    for turn in turns:
        if not isinstance(turn, dict):
            raise CodexBridgeError("Malformed turn; refusing to skip history content.")
        items = []
        # Put errors into a pageable field rather than unbounded turn metadata.
        if turn.get("error") is not None:
            items.append({"type": "turnMetadata", "id": None, "error": _sanitize_native_value(turn["error"])})
        for raw in turn.get("items") or []:
            if not isinstance(raw, dict):
                raise CodexBridgeError("Malformed history item; refusing to skip content.")
            visible = _project_item(raw, include_tool_output=include_tool_output, include_diffs=include_diffs)
            if visible is None:
                reasoning_excluded += 1
            else:
                items.append(visible)
        projected.append({"id": turn.get("id"), "status": turn.get("status"), "items": items})
    return projected, reasoning_excluded


def _visible_fingerprint(turns: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    for chunk in encoder.iterencode(turns):
        digest.update(chunk.encode("utf-8"))
    return digest.hexdigest()


def _item_fields(item: dict[str, Any]) -> list[tuple[str, str, str]]:
    """One text stream per allowed top-level field; compound fields use JSON text."""
    return [(name, "text" if isinstance(value, str) else "json", value if isinstance(value, str) else _json_text(value))
            for name, value in item.items() if name not in {"type", "id", "role", "phase"}]


def _position(turn: int = 0, item: int = 0, field: int = 0, text: int = 0) -> dict[str, int]:
    return {"turn": turn, "item": item, "field": field, "text": text}


def _render_visible_page(turns: list[dict[str, Any]], *, max_chars: int, position: dict[str, int]) -> tuple[list[dict[str, Any]], dict[str, int] | None]:
    """Return contiguous field fragments, bounded by serialized turns characters.

    Every unfinished field has an exact offset. Nothing is clipped or replaced
    with an 'omitted' suffix. Item/field offsets refer to the privacy projection,
    not to private reasoning items.
    """
    output: list[dict[str, Any]] = []
    t0, i0, f0, x0 = (position[k] for k in ("turn", "item", "field", "text"))
    if t0 > len(turns) or (t0 == len(turns) and any((i0, f0, x0))):
        raise ValueError("Continuation turn offset is outside this history page.")
    if t0 == len(turns):
        return output, None

    def candidate(t: int, i: int | None, fragment: dict[str, Any] | None, item_complete: bool = False) -> list[dict[str, Any]]:
        result = copy.deepcopy(output)
        if not result or result[-1]["turn_index"] != t:
            result.append({"id": turns[t]["id"], "status": turns[t]["status"], "turn_index": t, "items": []})
        if i is None:
            return result
        rows = result[-1]["items"]
        if not rows or rows[-1]["item_index"] != i:
            item = turns[t]["items"][i]
            row = {key: item[key] for key in ("type", "id", "role", "phase") if key in item}
            row.update({"item_index": i, "continued_from_previous_page": t == t0 and i == i0 and (f0 > 0 or x0 > 0), "item_complete": item_complete, "fragments": []})
            rows.append(row)
        rows[-1]["item_complete"] = item_complete
        if fragment is not None:
            rows[-1]["fragments"].append(fragment)
        return result

    def fits(value: list[dict[str, Any]]) -> bool:
        return len(_json_text(value)) <= max_chars

    for t in range(t0, len(turns)):
        items = turns[t]["items"]
        first_i = i0 if t == t0 else 0
        if first_i > len(items) or (first_i == len(items) and t == t0 and any((f0, x0))):
            raise ValueError("Continuation item offset is outside this history page.")
        if not items:
            maybe = candidate(t, None, None)
            if not fits(maybe):
                if not output:
                    raise CodexBridgeError("Turn metadata exceeds max_chars; increase max_chars. No history was discarded.")
                return output, _position(t)
            output = maybe
        for i in range(first_i, len(items)):
            fields = _item_fields(items[i])
            first_f = f0 if t == t0 and i == i0 else 0
            if first_f >= len(fields):
                raise ValueError("Continuation field offset is outside this history item.")
            for f in range(first_f, len(fields)):
                name, encoding, text = fields[f]
                start = x0 if t == t0 and i == i0 and f == f0 else 0
                if start > len(text) or (start == len(text) and start != 0):
                    raise ValueError("Continuation text offset is outside this history field.")
                available = len(text) - start

                def with_count(count: int) -> list[dict[str, Any]]:
                    end = start + count
                    complete = end == len(text)
                    fragment = {"field": name, "encoding": encoding, "offset": start, "end_offset": end, "total_chars": len(text), "complete": complete, "text": text[start:end]}
                    return candidate(t, i, fragment, item_complete=complete and f == len(fields) - 1)

                if available == 0:
                    maybe = with_count(0)
                    if not fits(maybe):
                        if not output:
                            raise CodexBridgeError("Item metadata exceeds max_chars; increase max_chars. No history was discarded.")
                        return output, _position(t, i, f, start)
                    output = maybe
                    continue
                # Check the complete field first. JSON's true is one character
                # shorter than false, so the final completion flags can make an
                # exact-boundary full field fit where a near-full fragment does not.
                if available <= max_chars:
                    whole = with_count(available)
                    if fits(whole):
                        output = whole
                        continue
                # In the partial interval both completion flags stay false, so
                # serialized size is monotonic and bisection is safe.
                lo, hi = 0, min(available - 1, max_chars)
                best: list[dict[str, Any]] | None = None
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    maybe = with_count(mid)
                    if fits(maybe):
                        lo, best = mid, maybe
                    else:
                        hi = mid - 1
                if lo == 0:
                    if not output:
                        raise CodexBridgeError("Item metadata leaves no room for content; increase max_chars. No history was discarded.")
                    return output, _position(t, i, f, start)
                output = best if best is not None else with_count(lo)
                if lo < available:
                    return output, _position(t, i, f, start + lo)
    return output, None


def _unsupported_method(exc: CodexAppServerError) -> bool:
    return getattr(exc, "code", None) == -32601 or "method not found" in str(exc).lower() or "unsupported method" in str(exc).lower()


def _page_response(result: Any, method: str) -> tuple[list[dict[str, Any]], str | None]:
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        raise CodexBridgeError(f"Unexpected {method} response: expected a data array; refusing to report an empty history.")
    data = result["data"]
    if any(not isinstance(row, dict) for row in data):
        raise CodexBridgeError(f"Unexpected {method} row; refusing to skip history content.")
    cursor = result.get("nextCursor")
    if cursor is not None and (not isinstance(cursor, str) or not cursor):
        raise CodexBridgeError(f"Unexpected {method} nextCursor; expected string or null.")
    return data, cursor


async def _hydrate_turn_items(client: CodexAppServerClient, *, thread_id: str, turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Hydrate selected turns only, with checked per-turn item pagination."""
    hydrated = []
    for turn in turns:
        turn_id = turn.get("id")
        if not isinstance(turn_id, str) or not turn_id:
            raise CodexBridgeError("A turn lacks an ID; refusing to replace its content with an empty list.")
        copied = dict(turn)
        items: list[dict[str, Any]] = []
        cursor = None
        seen: set[str] = set()
        empty_pages = 0
        while True:
            params: dict[str, Any] = {"threadId": thread_id, "turnId": turn_id, "limit": 100, "sortDirection": "asc"}
            if cursor is not None:
                params["cursor"] = cursor
            result = await client.request("thread/items/list", params, timeout_seconds=120.0)
            entries, next_cursor = _page_response(result, "thread/items/list")
            for entry in entries:
                if entry.get("turnId", turn_id) != turn_id or not isinstance(entry.get("item"), dict):
                    raise CodexBridgeError("Invalid item-page entry or wrong turn ID; refusing to skip content.")
                items.append(entry["item"])
            if next_cursor is None:
                break
            if next_cursor == cursor or next_cursor in seen:
                raise CodexBridgeError("Codex repeated an item cursor; pagination stopped instead of looping or skipping content.")
            seen.add(next_cursor)
            empty_pages = empty_pages + 1 if not entries else 0
            if empty_pages > 100:
                raise CodexBridgeError("More than 100 empty item pages; pagination stopped without declaring history complete.")
            cursor = next_cursor
        copied.update({"items": items, "itemsView": "full"})
        hydrated.append(copied)
    return hydrated


async def _load_read_window(client: CodexAppServerClient, *, thread_id: str, thread: dict[str, Any], state: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    """Load a turn window. Legacy whole reads are a labelled last-resort fallback."""
    backend = state.get("backend") or ("paginated" if thread.get("historyMode") == "paginated" else "legacy_turns")
    limit = state["limit"]
    cursor = state.get("cursor")
    if backend != "legacy_full":
        params: dict[str, Any] = {"threadId": thread_id, "limit": limit, "sortDirection": "desc", "itemsView": "notLoaded" if backend == "paginated" else "full"}
        if cursor is not None:
            params["cursor"] = cursor
        try:
            response = await client.request("thread/turns/list", params, timeout_seconds=120.0)
        except CodexAppServerError as exc:
            # Never hide a paginated-session error behind a whole-history read.
            if backend == "legacy_turns" and state.get("backend") is None and _unsupported_method(exc):
                backend = "legacy_full"
            else:
                raise
        else:
            turns_desc, next_cursor = _page_response(response, "thread/turns/list")
            if next_cursor is not None and next_cursor == cursor:
                raise CodexBridgeError("Codex repeated a turn cursor; refusing an infinite pagination loop.")
            turns = list(reversed(turns_desc))
            if backend == "paginated":
                turns = await _hydrate_turn_items(client, thread_id=thread_id, turns=turns)
                source = "thread/turns/list + thread/items/list"
            else:
                for turn in turns:
                    if not isinstance(turn.get("items"), list) or turn.get("itemsView") in {"summary", "notLoaded"}:
                        raise CodexBridgeError("Codex did not return full legacy turn items; refusing to treat summaries as complete history.")
                source = "thread/turns/list (legacy full items)"
            window = {"backend": backend, "cursor": cursor, "legacy_end": None, "next_cursor": next_cursor, "next_legacy_end": None, "has_older_turns": next_cursor is not None}
            return turns, window, source
    response = await client.request("thread/read", {"threadId": thread_id, "includeTurns": True}, timeout_seconds=180.0)
    full = response.get("thread") if isinstance(response, dict) else None
    if not isinstance(full, dict) or not isinstance(full.get("turns"), list):
        raise CodexBridgeError("Legacy thread/read did not return turns; refusing to report an empty history.")
    all_turns = full["turns"]
    end = state.get("legacy_end")
    if end is None:
        end = len(all_turns)
    if end > len(all_turns):
        raise CodexBridgeError("Legacy history changed while paging. Restart without page_token after the session is idle.")
    start = max(0, end - limit)
    window = {"backend": "legacy_full", "cursor": None, "legacy_end": end, "next_cursor": None, "next_legacy_end": start if start else None, "has_older_turns": start > 0}
    return list(all_turns[start:end]), window, "thread/read legacy fallback (whole history loaded)"


async def _read_session_app_server(*, thread_id: str, max_turns: int = 100, max_chars: int = 120_000, include_tool_output: bool = False, include_diffs: bool = False, page_token: str | None = None) -> dict[str, Any]:
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise ValueError("thread_id is required")
    thread_id = thread_id.strip()
    max_turns = max(1, min(int(max_turns), 100))
    max_chars = max(2_000, min(int(max_chars), 120_000))
    if type(include_tool_output) is not bool or type(include_diffs) is not bool:
        raise ValueError("include_tool_output and include_diffs must be booleans, not strings or numbers.")
    options = {"tool_output": include_tool_output, "diffs": include_diffs}
    state = _unpack_read_token(page_token, thread_id, include_tool_output, include_diffs) if page_token is not None else {
        "v": 2, "kind": "read", "thread_id": thread_id, "backend": None, "limit": max_turns, "cursor": None, "legacy_end": None, "position": _position(), "fingerprint": None, "options": options,
    }
    # max_turns is frozen by the token; max_chars may change between calls.
    async with CodexAppServerClient() as client:
        response = await client.request("thread/read", {"threadId": thread_id, "includeTurns": False})
        thread = response.get("thread") if isinstance(response, dict) else None
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            raise CodexBridgeError("Codex did not return metadata for the requested session.")
        raw_turns, window, source = await _load_read_window(client, thread_id=thread_id, thread=thread, state=state)
    visible, reasoning_excluded = _prepare_visible_page(raw_turns, include_tool_output=options["tool_output"], include_diffs=options["diffs"])
    fingerprint = _visible_fingerprint(visible)
    if state.get("fingerprint") is not None and state["fingerprint"] != fingerprint:
        raise CodexBridgeError("The selected history page changed during this read. No continuation was returned because offsets may no longer match. Let the Codex session become idle, then restart without page_token; do not combine the old and new read as a complete transcript.")
    turns, remaining_position = _render_visible_page(visible, max_chars=max_chars, position=state["position"])
    next_state = None
    character_truncated = remaining_position is not None
    if character_truncated:
        next_state = {**state, "backend": window["backend"], "cursor": window["cursor"], "legacy_end": window["legacy_end"], "position": remaining_position, "fingerprint": fingerprint}
    elif window["has_older_turns"]:
        next_state = {**state, "backend": window["backend"], "cursor": window["next_cursor"], "legacy_end": window["next_legacy_end"], "position": _position(), "fingerprint": None}
    next_token = _pack_read_token(next_state) if next_state else None
    metadata = _thread_summary(thread)
    metadata.update({"session_id": thread.get("sessionId"), "forked_from_id": thread.get("forkedFromId"), "history_mode": thread.get("historyMode")})
    fragments = [fragment for turn in turns for item in turn["items"] for fragment in item["fragments"]]
    payload_chars = len(_json_text(turns))
    content_chars = sum(len(fragment["text"]) for fragment in fragments)
    reason = "same_turn_page" if character_truncated else "older_turns" if next_token else None
    return {
        "bridge_version": BRIDGE_VERSION,
        "output_format": "field_fragments_v1",
        "thread": metadata,
        "turns": turns,
        "history_source": source,
        "page_complete": not character_truncated,
        "character_truncated": character_truncated,
        "has_older_turns": window["has_older_turns"],
        "has_more_content": next_token is not None,
        "next_page_token": next_token,
        "newer_page_token": None,
        "continuation_reason": reason,
        "turns_returned": len(turns),
        "items_returned": sum(len(turn["items"]) for turn in turns),
        "fragments_returned": len(fragments),
        "chars_returned": content_chars,
        "output_info": {"max_chars": max_chars, "max_turns": state["limit"], "payload_chars": payload_chars, "content_chars": content_chars, "character_budget_remaining": max_chars - payload_chars, "truncated": character_truncated, "source_page_turns": len(visible), "reasoning_items_excluded_in_source_page": reasoning_excluded, "in_page_start": state["position"], "in_page_next": remaining_position},
        "privacy_note": "Reasoning is always excluded. Tool output/arguments/results and diffs are opt-in. Unsupported item payloads and non-text attachment bytes are not exposed. Completeness refers to this visible-content projection, not to all raw Codex records.",
        "pagination_note": "Read items[].fragments[].text; offsets are zero-based, end-exclusive Python characters within each field. Concatenate fragments for the same turn/item/field by offset; JSON-encoded fields are decoded only after assembly. Whenever has_more_content is true, pass next_page_token unchanged as page_token, even when has_older_turns is false. A turn window is chronological; successive windows move backward. Keep privacy flags unchanged. Do not claim complete coverage until has_more_content is false. Metadata previews are not transcript content.",
    }



async def read_session(*, thread_id: str, max_turns: int = 100, max_chars: int = 120_000, include_tool_output: bool = False, include_diffs: bool = False, page_token: str | None = None) -> dict[str, Any]:
    """Prefer App Server; recover ONE permitted local rollout after a read failure.

    A backend change never pretends to preserve native item offsets. A recovery
    restart is labelled explicitly and must replace, not extend, prior pages.
    """
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise ValueError("thread_id is required")
    thread_id = thread_id.strip()
    if type(include_tool_output) is not bool or type(include_diffs) is not bool:
        raise ValueError("include_tool_output and include_diffs must be booleans.")
    if page_token is not None and not isinstance(page_token, str):
        raise ValueError("page_token must be the returned string, not an object.")
    if isinstance(page_token, str):
        page_token = page_token.strip()
    max_turns = max(1, min(int(max_turns), 100))
    max_chars = max(2_000, min(int(max_chars), 120_000))
    if page_token and page_token.startswith(("cbr1_", "cbr2_", "cbr3_")):
        raise CodexBridgeError("The local token belongs to an earlier recovery transport. Restart this session without a token after upgrading to bridge 0.3.0; do not combine projections.")
    local_continuation = bool(page_token and page_token.startswith(rollout_fallback.PREFIX))
    enabled = os.environ.get("CODEX_HISTORY_ROLLOUT_FALLBACK", "1") != "0"
    trigger = None
    if not local_continuation:
        try:
            return await _read_session_app_server(thread_id=thread_id, max_turns=max_turns, max_chars=max_chars,
                include_tool_output=include_tool_output, include_diffs=include_diffs, page_token=page_token)
        except CodexAppServerError as exc:
            # Recover only from bridge-observed transport/size failures. App
            # Server error responses can encode authorization decisions in
            # wording or codes that this bridge cannot safely enumerate.
            message = str(exc)
            lowered = message.lower()
            if exc.code is not None:
                trigger = None
            elif "16 mib" in lowered:
                trigger = "stream_limit"
            elif "timed out" in lowered:
                trigger = "timeout"
            elif any(marker in lowered for marker in (
                "exited or disconnected",
                "stdout is unavailable",
                "emitted invalid utf-8/json",
                "emitted a non-object json message",
            )):
                trigger = "app_server_transport_error"
            else:
                trigger = None
            if not enabled or trigger is None:
                raise
    elif not enabled:
        raise CodexBridgeError("Local rollout recovery is disabled by CODEX_HISTORY_ROLLOUT_FALLBACK=0.")
    try:
        result = await asyncio.to_thread(rollout_fallback.read_page,
            thread_id=thread_id, max_turns=max_turns, max_chars=max_chars,
            include_tool_output=include_tool_output, include_diffs=include_diffs,
            page_token=page_token if local_continuation else None,
            restarted=page_token is not None and not local_continuation)
    except (rollout_fallback.RolloutReadError, OSError) as exc:
        detail = str(exc) if isinstance(exc, rollout_fallback.RolloutReadError) else "The local rollout could not be opened/read; check local permissions and file availability."
        if local_continuation:
            raise CodexBridgeError(f"Local fallback continuation failed: {detail}") from exc
        raise CodexBridgeError(f"App Server history was not recovered. Local fallback: {detail}") from exc
    result["bridge_version"] = BRIDGE_VERSION
    result["fallback_info"]["trigger"] = trigger or "continuation_of_prior_recovery"
    return result


def _match_excerpt(text: str, query: str, radius: int = 260) -> str:
    folded_text = text.casefold()
    folded_query = query.casefold()
    index = folded_text.find(folded_query)
    if index < 0:
        return _clip(text, radius * 2)
    start = max(0, index - radius)
    end = min(len(text), index + len(query) + radius)
    prefix = "…" if start else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[start:end] + suffix


def _search_text(value: Any) -> str:
    """Normalize searchable native text through the unconditional privacy filter."""
    text = value if isinstance(value, str) else str(value or "")
    sanitized = _sanitize_native_value(text)
    return sanitized if isinstance(sanitized, str) else ""


def _search_thread_summary(thread: dict[str, Any]) -> dict[str, Any]:
    """Return search identity metadata without an unclassified native preview."""
    summary = _thread_summary(thread)
    summary.pop("preview", None)
    sanitized = _sanitize_native_value(summary)
    if not isinstance(sanitized, dict):
        raise CodexBridgeError("Native search thread metadata could not be safely projected.")
    return sanitized


def _searchable_item_texts(item: dict[str, Any]) -> list[tuple[str, str]]:
    item_type = item.get("type")
    if item_type == "reasoning":
        return []
    if item_type == "userMessage":
        projected = _project_item(item, include_tool_output=False, include_diffs=False)
        text = projected.get("text") if projected else ""
        return [("user_message", _search_text(text))] if text else []
    if item_type == "agentMessage":
        projected = _project_item(item, include_tool_output=False, include_diffs=False)
        text = projected.get("text") if projected else ""
        return [("assistant_message", _search_text(text))] if text else []
    if item_type == "plan":
        return [("plan", _search_text(item.get("text")))]
    if item_type == "commandExecution":
        return [("command", _search_text(item.get("command")))]
    if item_type == "fileChange":
        values = []
        for change in item.get("changes") or []:
            if isinstance(change, dict) and change.get("path"):
                values.append(("file_path", _search_text(change.get("path"))))
        return values
    if item_type == "mcpToolCall":
        return [
            ("mcp_tool", _search_text(f"{item.get('server') or ''} {item.get('tool') or ''}".strip()))
        ]
    if item_type == "dynamicToolCall":
        return [("dynamic_tool", _search_text(item.get("tool")))]
    if item_type == "webSearch":
        return [("web_search", _search_text(item.get("query")))]
    if item_type in {"enteredReviewMode", "exitedReviewMode"}:
        return [("review", _search_text(item.get("review")))]
    if item_type == "imageView":
        return [("image_path", _search_text(item.get("path")))]
    return []


async def _manual_search_history(
    client: CodexAppServerClient,
    *,
    query: str,
    threads: list[dict[str, Any]],
    turns_per_session: int,
    max_results: int,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], int]:
    """Bounded compatibility scan. Older builds may require a full legacy read."""
    matches: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []
    turns_scanned = 0
    folded_query = query.casefold()

    for thread in threads:
        if len(matches) >= max_results:
            break
        thread_id = str(thread.get("id") or "")
        if not thread_id:
            continue
        thread_matches: list[dict[str, Any]] = []
        for field_name in ("name", "preview", "cwd"):
            value = thread.get(field_name)
            searchable_value = _search_text(value)
            if searchable_value and folded_query in searchable_value.casefold():
                thread_matches.append({
                    "turn_id": None,
                    "item_id": None,
                    "field": f"thread_{field_name}",
                    "excerpt": _match_excerpt(searchable_value, query),
                })

        try:
            if thread.get("historyMode") == "paginated":
                result = await client.request(
                    "thread/turns/list",
                    {
                        "threadId": thread_id,
                        "limit": turns_per_session,
                        "sortDirection": "desc",
                        "itemsView": "notLoaded",
                    },
                    timeout_seconds=120.0,
                )
                turns = list((result or {}).get("data") or [])
                turns = await _hydrate_turn_items(
                    client,
                    thread_id=thread_id,
                    turns=turns,
                )
            else:
                full = await client.request(
                    "thread/read",
                    {"threadId": thread_id, "includeTurns": True},
                    timeout_seconds=180.0,
                )
                turns = list(((full or {}).get("thread") or {}).get("turns") or [])[-turns_per_session:]
        except CodexAppServerError as exc:
            warnings.append({"thread_id": thread_id, "warning": str(exc)})
            turns = []

        turns_scanned += len(turns)
        for turn in turns:
            if len(thread_matches) >= 8:
                break
            for item in turn.get("items") or []:
                if not isinstance(item, dict):
                    continue
                for field, text in _searchable_item_texts(item):
                    if text and folded_query in text.casefold():
                        thread_matches.append({
                            "turn_id": turn.get("id"),
                            "item_id": item.get("id"),
                            "field": field,
                            "excerpt": _match_excerpt(text, query),
                        })
                        if len(thread_matches) >= 8:
                            break
                if len(thread_matches) >= 8:
                    break

        if thread_matches:
            matches.append({"thread": _search_thread_summary(thread), "matches": thread_matches})

    return matches, warnings, turns_scanned


async def search_history(
    *,
    query: str,
    cwd: str | None = None,
    archived: bool = False,
    include_background: bool = False,
    scan_legacy_logs: bool = False,
    max_sessions: int = 30,
    turns_per_session: int = 20,
    max_results: int = 20,
) -> dict[str, Any]:
    """Search persisted Codex history, preferring Codex's native history-search API."""
    query = query.strip()
    if not query:
        raise ValueError("query is required")
    if len(query) > 500:
        raise ValueError("query must be 500 characters or fewer")
    max_sessions = max(1, min(int(max_sessions), 100))
    turns_per_session = max(1, min(int(turns_per_session), 100))
    max_results = max(1, min(int(max_results), 100))
    source_kinds = ALL_SOURCE_KINDS if include_background else USER_FACING_SOURCE_KINDS

    async with CodexAppServerClient() as client:
        # Native search is purpose-built for persisted history and avoids loading
        # complete turn payloads. Page until enough cwd-matching results are found
        # or the caller's bounded scan budget is exhausted.
        native_results: list[dict[str, Any]] = []
        native_cursor: str | None = None
        native_examined = 0
        native_error: str | None = None
        try:
            while native_examined < max_sessions and len(native_results) < max_results:
                page_limit = min(100, max_sessions - native_examined)
                params: dict[str, Any] = {
                    "searchTerm": query,
                    "limit": page_limit,
                    "sortKey": "recency_at",
                    "sortDirection": "desc",
                    "archived": bool(archived),
                    "sourceKinds": source_kinds,
                }
                if native_cursor:
                    params["cursor"] = native_cursor
                response = await client.request("thread/search", params, timeout_seconds=120.0)
                page = list((response or {}).get("data") or [])
                native_examined += len(page)
                native_cursor = (response or {}).get("nextCursor")
                for result in page:
                    if not isinstance(result, dict):
                        continue
                    thread = result.get("thread") or {}
                    if cwd and str(thread.get("cwd") or "") != cwd:
                        continue
                    occurrences: list[dict[str, Any]] = []
                    try:
                        occurrence_response = await client.request(
                            "thread/searchOccurrences",
                            {
                                "threadId": str(thread.get("id") or ""),
                                "searchTerm": query,
                                "limit": 8,
                            },
                            timeout_seconds=120.0,
                        )
                        for occurrence in list((occurrence_response or {}).get("data") or []):
                            if not isinstance(occurrence, dict):
                                continue
                            occurrences.append({
                                "turn_id": occurrence.get("turnId"),
                                "item_id": occurrence.get("itemId"),
                                "field": "native_search_match",
                                "excerpt": "[native search excerpt omitted because its source item privacy could not be verified]",
                            })
                    except CodexAppServerError:
                        pass
                    if not occurrences:
                        occurrences = [{
                            "turn_id": None,
                            "item_id": None,
                            "field": "native_search_match",
                            "excerpt": "[native search excerpt omitted because its source item privacy could not be verified]",
                        }]
                    native_results.append({
                        "thread": _search_thread_summary(thread),
                        "matches": occurrences,
                    })
                    if len(native_results) >= max_results:
                        break
                if not page or not native_cursor:
                    break
        except CodexAppServerError as exc:
            native_error = str(exc)

        if native_error is None:
            return {
                "query": query,
                "cwd": cwd,
                "results": native_results,
                "search_source": "thread/search + thread/searchOccurrences",
                "sessions_scanned": native_examined,
                "turns_scanned": 0,
                "sessions_considered": native_examined,
                "more_sessions_exist": native_cursor is not None,
                "warnings": [],
                "limits": {
                    "max_sessions": max_sessions,
                    "turns_per_session": turns_per_session,
                    "max_results": max_results,
                    "max_matches_per_session": 8,
                },
                "privacy_note": (
                    "Native Codex history search is used. Reasoning, command output, tool arguments/results, "
                    "and file diffs are not deliberately surfaced by this bridge."
                ),
                "scan_legacy_logs": scan_legacy_logs,
            }

        # Compatibility fallback for Codex builds without the experimental search API.
        threads: list[dict[str, Any]] = []
        cursor: str | None = None
        while len(threads) < max_sessions:
            params = {
                "limit": min(100, max_sessions - len(threads)),
                "sortKey": "recency_at",
                "sortDirection": "desc",
                "archived": bool(archived),
                "useStateDbOnly": not bool(scan_legacy_logs),
                "sourceKinds": source_kinds,
            }
            if cwd:
                params["cwd"] = cwd
            if cursor:
                params["cursor"] = cursor
            listed = await client.request("thread/list", params)
            page = list((listed or {}).get("data") or [])
            threads.extend(page)
            cursor = (listed or {}).get("nextCursor")
            if not page or not cursor:
                break

        matches, warnings, turns_scanned = await _manual_search_history(
            client,
            query=query,
            threads=threads,
            turns_per_session=turns_per_session,
            max_results=max_results,
        )
        warnings.insert(0, {
            "thread_id": "(bridge)",
            "warning": f"Native history search unavailable; used compatibility fallback: {native_error}",
        })

    return {
        "query": query,
        "cwd": cwd,
        "results": matches,
        "search_source": "compatibility fallback",
        "sessions_scanned": len(threads),
        "turns_scanned": turns_scanned,
        "sessions_considered": len(threads),
        "more_sessions_exist": cursor is not None,
        "warnings": warnings,
        "limits": {
            "max_sessions": max_sessions,
            "turns_per_session": turns_per_session,
            "max_results": max_results,
            "max_matches_per_session": 8,
        },
        "privacy_note": (
            "Reasoning, command output, tool arguments/results, and file diffs are not searched or returned."
        ),
    }


async def bridge_status() -> dict[str, Any]:
    status: dict[str, Any] = {
        "bridge_version": BRIDGE_VERSION,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "codex_home": os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"),
        "read_only_tools": True,
        "local_rollout_fallback": os.environ.get("CODEX_HISTORY_ROLLOUT_FALLBACK", "1") != "0",
        "local_rollout_fallback_version": "2",
        "coverage_model": "scoped_projection_v2",
    }

    try:
        version = await asyncio.to_thread(get_codex_version)
        status["codex"] = version
        status["codex_cli_ok"] = version.get("return_code") == 0
        if version.get("return_code") != 0:
            status["codex_cli_error"] = (
                version.get("stderr") or version.get("stdout") or "codex --version failed"
            )
    except Exception as exc:
        status["codex_cli_ok"] = False
        status["codex_cli_error"] = str(exc)
        status["app_server_ok"] = False
        status["app_server_error"] = "Codex CLI probe failed; App Server was not started."
        return status

    try:
        async with CodexAppServerClient() as client:
            result = await client.request(
                "thread/list",
                {
                    "limit": 1,
                    "sortKey": "recency_at",
                    "sortDirection": "desc",
                    "archived": False,
                    "useStateDbOnly": True,
                    "sourceKinds": USER_FACING_SOURCE_KINDS,
                },
                timeout_seconds=45.0,
            )
        status["app_server_ok"] = True
        status["visible_session_count_in_probe"] = len((result or {}).get("data") or [])
    except Exception as exc:
        status["app_server_ok"] = False
        status["app_server_error"] = str(exc)
    return status
