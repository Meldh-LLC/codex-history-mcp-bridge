"""Read-only, bounded-memory recovery of ONE Codex JSONL rollout.

This is a labelled record projection, not a reimplementation of Codex's UI or
rollback/fork materializer. Called only after an App Server read failure (or with
its own continuation token). No writes, shell commands, network, or dependencies.
Strings are indexed as byte spans; image/reasoning payloads are never assembled.
"""
from __future__ import annotations

from collections import OrderedDict
import codecs
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from dataclasses import dataclass
from typing import Any, BinaryIO, Iterator

PREFIX = "cbr4_"
CHUNK = 64 * 1024
MAX_STATE_BYTES = 64 * 1024
TOKEN_CACHE_LIMIT = 4096
TOKEN_TTL_SECONDS = 12 * 60 * 60
_TOKEN_RE = re.compile(r"cbr4_[0-9a-f]{40}\Z")
_TOKEN_CACHE: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
_TOKEN_LOCK = threading.RLock()
MAX_RECORD_NODES = 100_000
MAX_DEPTH = 64
MAX_SCAN_RECORDS = 2000
UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
SPECIAL = re.compile(rb'["\\\x00-\x1f]')
HEX = re.compile(rb"[0-9a-fA-F]{4}\Z")
NUMBER = re.compile(rb"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")
URI_START = re.compile(r"data:[A-Za-z0-9.+/-]+(?:;[A-Za-z0-9=.+-]+)*;base64,", re.I)
URI_BODY = re.compile(r"[A-Za-z0-9+/=_-]*")
BINARY_MARKER = "[binary/image payload omitted]"
PRIVATE_KEYS = frozenset({"reasoning", "encrypted_content", "encryptedcontent", "raw_content", "rawcontent", "summary_text", "summarytext", "chain_of_thought", "chainofthought"})
BINARY_KEYS = frozenset({"base64", "b64_json", "image_base64", "image_bytes", "audio_bytes", "bytes", "encryptedcontent", "encrypted_content"})


class RolloutReadError(RuntimeError):
    """A safe diagnostic, never containing a raw record or secret."""


def _wire(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class StringSpan:
    start: int
    end: int
    chars: int
    head: str


class Scanner:
    """Strict JSONL parser whose strings remain references to the source file."""
    def __init__(self, handle: BinaryIO, end: int, offset: int = 0):
        self.f = handle
        self.end = end
        self.f.seek(offset)
        self.buf = b""
        self.i = 0
        self.base = offset
        self.nodes = 0

    @property
    def pos(self) -> int:
        return self.base + self.i

    def fill(self) -> bool:
        if self.i < len(self.buf):
            return True
        self.base += len(self.buf)
        self.i = 0
        self.buf = self.f.read(min(CHUNK, max(0, self.end - self.base)))
        return bool(self.buf)

    def peek(self) -> int | None:
        return self.buf[self.i] if self.fill() else None

    def take(self, n: int) -> bytes:
        pieces = []
        while n:
            if not self.fill():
                self.fail("Incomplete JSON record; no end-of-history claim is safe")
            count = min(n, len(self.buf) - self.i)
            pieces.append(self.buf[self.i:self.i + count])
            self.i += count
            n -= count
        return b"".join(pieces)

    def fail(self, reason: str) -> None:
        raise RolloutReadError(f"Local rollout at byte {self.pos}: {reason}. No raw content included.")

    def spaces(self, *, lines: bool = False) -> None:
        permitted = b" \t\r\n" if lines else b" \t\r"
        while self.peek() is not None and self.peek() in permitted:
            self.i += 1

    def string_chunks(self) -> Iterator[str]:
        if self.take(1) != b'"':
            self.fail("Expected JSON string")
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        try:
            while True:
                if not self.fill():
                    self.fail("Unterminated JSON string")
                match = SPECIAL.search(self.buf, self.i)
                stop = match.start() if match else len(self.buf)
                segment = self.buf[self.i:stop]
                self.i = stop
                if segment:
                    decoded = decoder.decode(segment, final=False)
                    if decoded:
                        yield decoded
                if match is None:
                    continue
                ch = self.take(1)
                tail = decoder.decode(b"", final=True)
                if tail:
                    yield tail
                decoder = codecs.getincrementaldecoder("utf-8")("strict")
                if ch == b'"':
                    return
                if ch != b"\\":
                    self.fail("Unescaped control character in JSON string")
                escaped = self.take(1)
                simple = {b'"': '"', b"\\": "\\", b"/": "/", b"b": "\b", b"f": "\f", b"n": "\n", b"r": "\r", b"t": "\t"}
                if escaped in simple:
                    yield simple[escaped]
                elif escaped == b"u":
                    digits = self.take(4)
                    if HEX.fullmatch(digits) is None:
                        self.fail("Invalid Unicode escape")
                    point = int(digits, 16)
                    if 0xD800 <= point <= 0xDBFF:
                        if self.take(2) != b"\\u":
                            self.fail("Unpaired Unicode surrogate")
                        low = self.take(4)
                        if HEX.fullmatch(low) is None or not 0xDC00 <= int(low, 16) <= 0xDFFF:
                            self.fail("Invalid Unicode surrogate pair")
                        point = 0x10000 + (point - 0xD800) * 1024 + int(low, 16) - 0xDC00
                    elif 0xDC00 <= point <= 0xDFFF:
                        self.fail("Unpaired Unicode surrogate")
                    yield chr(point)
                else:
                    self.fail("Invalid JSON escape")
        except UnicodeDecodeError as exc:
            raise RolloutReadError(f"Invalid UTF-8 in local rollout near byte {self.pos}; raw content omitted.") from exc

    def string(self) -> StringSpan:
        start = self.pos
        chars = 0
        head = ""
        for text in self.string_chunks():
            chars += len(text)
            if len(head) < 256:
                head += text[:256 - len(head)]
        return StringSpan(start, self.pos, chars, head)

    def value(self, depth: int = 0) -> Any:
        self.nodes += 1
        if depth > MAX_DEPTH or self.nodes > MAX_RECORD_NODES:
            self.fail("JSON structure exceeds safe parser bounds; record was not skipped")
        self.spaces()
        ch = self.peek()
        if ch == 34:
            return self.string()
        if ch in (123, 91):
            is_object = ch == 123
            self.i += 1
            out: Any = {} if is_object else []
            closer = 125 if is_object else 93
            self.spaces()
            if self.peek() == closer:
                self.i += 1
                return out
            while True:
                if is_object:
                    if self.peek() != 34:
                        self.fail("Expected object key")
                    key_ref = self.string()
                    if key_ref.chars > 256:
                        self.fail("Oversized object key")
                    key = key_ref.head
                    if key in out:
                        self.fail("Duplicate JSON object key")
                    self.spaces()
                    if self.take(1) != b":":
                        self.fail("Expected colon")
                    out[key] = self.value(depth + 1)
                else:
                    out.append(self.value(depth + 1))
                self.spaces()
                separator = self.take(1)[0]
                if separator == closer:
                    return out
                if separator != 44:
                    self.fail("Expected comma or closing delimiter")
                self.spaces()
        for literal, result in ((b"true", True), (b"false", False), (b"null", None)):
            if ch == literal[0]:
                if self.take(len(literal)) != literal:
                    self.fail("Invalid JSON literal")
                return result
        if ch is not None and ch in b"-0123456789":
            token = bytearray()
            while self.peek() is not None and self.peek() in b"-0123456789.eE+":
                token += self.take(1)
                if len(token) > 128:
                    self.fail("Oversized JSON number")
            if NUMBER.fullmatch(token) is None:
                self.fail("Invalid JSON number")
            # Preserve numeric lexical form without possible float overflow.
            return Numeric(token.decode("ascii"))
        self.fail("Invalid or incomplete JSON value")

    def record(self) -> tuple[int, int, dict[str, Any]] | None:
        if self.pos == 0:
            self.fill()
            if self.buf.startswith(b"\xef\xbb\xbf"):
                self.i = 3
        start = self.pos
        self.spaces(lines=True)
        if self.peek() is None:
            return None
        self.nodes = 0
        value = self.value()
        if not isinstance(value, dict):
            self.fail("Rollout record must be an object")
        self.spaces()
        if self.peek() not in (10, None):
            self.fail("Extra data after JSON record")
        if self.peek() == 10:
            self.i += 1
        return start, self.pos, value


@dataclass(frozen=True)
class Numeric:
    raw: str


def small(value: Any, default: str = "", *, limit: int = 256) -> str:
    if isinstance(value, StringSpan):
        if value.chars > min(limit, 256):
            raise RolloutReadError("Oversized metadata field in local rollout; content not exposed.")
        return value.head
    if isinstance(value, str):
        if len(value) > limit:
            raise RolloutReadError("Oversized metadata field in local rollout.")
        return value
    if value is None:
        return default
    raise RolloutReadError("Unexpected metadata field type in local rollout.")


def chunks(value: StringSpan | str, handle: BinaryIO) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    else:
        source = Scanner(handle, value.end, value.start)
        yield from source.string_chunks()


def redact_data_uris(parts: Iterator[str]) -> Iterator[str]:
    """Bounded lookbehind; suppress inline data URIs even across buffer boundaries."""
    pending = ""
    dropping = False
    for part in parts:
        pending += part
        while pending:
            if dropping:
                end = URI_BODY.match(pending).end()
                pending = pending[end:]
                if not pending:
                    break
                dropping = False
            match = URI_START.search(pending)
            if match:
                if match.start():
                    yield pending[:match.start()]
                yield BINARY_MARKER
                pending = pending[match.end():]
                dropping = True
            else:
                # A MIME data URI header longer than this is not recognized;
                # typed image/binary fields are independently excluded.
                safe = max(0, len(pending) - 512)
                if safe:
                    yield pending[:safe]
                    pending = pending[safe:]
                break
    if pending and not dropping:
        yield pending


def _kind(value: Any) -> str:
    return re.sub(r"[_-]", "", small(value)).lower()



def _safe_kind(value: Any) -> str:
    # Arbitrary tool JSON may use numeric/object-valued "type" or "role" fields.
    # Those are data, not protocol tags. Top-level record parsing stays strict.
    if value is None:
        return ""
    if isinstance(value, StringSpan):
        return _kind(value) if value.chars <= 256 else "unrecognized"
    if isinstance(value, str):
        return _kind(value) if len(value) <= 256 else "unrecognized"
    return ""


def json_chunks(value: Any, handle: BinaryIO) -> Iterator[str]:
    """Sanitized opt-in tool data; no raw dump of unknown/private/binary objects."""
    if isinstance(value, (StringSpan, str)):
        yield '"'
        for part in redact_data_uris(chunks(value, handle)):
            yield json.dumps(part, ensure_ascii=False)[1:-1]
        yield '"'
    elif isinstance(value, Numeric):
        yield value.raw
    elif isinstance(value, list):
        yield "["
        for i, one in enumerate(value):
            if i:
                yield ","
            yield from json_chunks(one, handle)
        yield "]"
    elif isinstance(value, dict):
        kind = _safe_kind(value.get("type"))
        if kind.startswith(("reasoning", "agentreasoning")) or _safe_kind(value.get("role")) in {"system", "developer"} or _safe_kind(value.get("channel")) in {"analysis", "reasoning"} or _safe_kind(value.get("phase")) in {"analysis", "reasoning"}:
            yield _wire("[reasoning omitted]")
            return
        if kind in {"image", "inputimage", "outputimage", "imagegeneration", "imagegenerationcall", "audio", "inputaudio", "outputaudio", "resource", "resourcelink"}:
            yield _wire(BINARY_MARKER)
            return
        yield "{"
        first = True
        for key, child in value.items():
            if key.lower() in PRIVATE_KEYS:
                continue
            if not first:
                yield ","
            first = False
            yield _wire(key) + ":"
            binary_like = key.lower() in {"data", "blob", "bytes", "base64_data"} and isinstance(child, StringSpan) and child.chars >= 16384 and re.fullmatch(r"[A-Za-z0-9+/=_-]+", child.head) is not None
            if binary_like or key.lower() in BINARY_KEYS or key.lower() in {"image_url", "imageurl", "image", "images"}:
                yield _wire(BINARY_MARKER)
            else:
                yield from json_chunks(child, handle)
        yield "}"
    else:
        yield _wire(value)


@dataclass
class Field:
    name: str
    values: list[Any]
    encoding: str = "text"

    def parts(self, handle: BinaryIO) -> Iterator[str]:
        if self.encoding == "json":
            yield from json_chunks(self.values[0], handle)
        else:
            def raw() -> Iterator[str]:
                for v in self.values:
                    if isinstance(v, (StringSpan, str)):
                        yield from chunks(v, handle)
                    else:
                        raise RolloutReadError("Unexpected visible text field; no raw object was exposed.")
            yield from redact_data_uris(raw())


def _content_parts(content: Any) -> list[Any]:
    if content is None:
        return []
    if isinstance(content, (StringSpan, str)):
        return [content]
    if not isinstance(content, list):
        raise RolloutReadError("Unrecognized message content shape in local rollout.")
    out = []
    for block in content:
        if not isinstance(block, dict):
            raise RolloutReadError("Unrecognized content block in local rollout.")
        kind = _kind(block.get("type"))
        if kind in {"text", "inputtext", "outputtext"}:
            if "text" not in block:
                raise RolloutReadError("A visible text block lacks its text field.")
            out.append(block["text"])
        elif kind in {"image", "inputimage", "outputimage", "localimage", "audio", "inputaudio", "outputaudio", "file", "inputfile"}:
            out.append(BINARY_MARKER)
        elif kind == "encryptedcontent":
            out.append("[encrypted content omitted]")
        else:
            out.append("[unsupported non-text content block omitted]")
    return out


def _unknown_content(content: Any) -> bool:
    allowed = {"text", "inputtext", "outputtext", "image", "inputimage", "outputimage", "localimage", "audio", "inputaudio", "outputaudio", "file", "inputfile", "encryptedcontent"}
    return isinstance(content, list) and any(isinstance(b, dict) and _kind(b.get("type")) not in allowed for b in content)


def _legacy_project(record: dict[str, Any], *, include_tool_output: bool, include_diffs: bool) -> tuple[str, str | None, list[Field], dict[str, Any]]:
    """Recover known public record fields, explicitly label duplicates/unsupported data."""
    top = small(record.get("type"))
    payload = record.get("payload")
    if not isinstance(payload, dict):
        raise RolloutReadError("A local rollout record lacks an object payload.")
    subtype = _kind(payload.get("type"))
    info: dict[str, Any] = {"record_family": top, "possible_duplicate_representation": False}
    if record.get("timestamp") is not None:
        info["source_timestamp"] = small(record["timestamp"])
    # Never expose prompts/instructions/compaction replacements or reasoning.
    if top in {"session_meta", "turn_context", "world_state", "retained_context", "token_usage_record", "security_risk_score", "inter_agent_communication_metadata"}:
        return "ignored", None, [], info
    if top == "compacted":
        return "contextCompaction", None, [Field("note", ["Context compaction marker. Replacement/model-context payload omitted."])], info
    if top == "event_msg" and subtype in {"agentreasoning", "agentreasoningrawcontent", "agentreasoningsectionbreak", "reasoning", "token_count", "tokencount", "turnstarted", "taskstarted", "itemstarted", "threadsettingsapplied"}:
        return "ignored", None, [], info
    item = payload
    completed = top == "event_msg" and subtype == "itemcompleted"
    if completed:
        item = payload.get("item")
        if not isinstance(item, dict):
            raise RolloutReadError("item_completed lacks a valid item in local rollout.")
        subtype = _kind(item.get("type"))
        info["possible_duplicate_representation"] = True
    if top == "response_item" and isinstance(item.get("item"), dict):
        # Accept older/alternate envelope with item + metadata, without exposing metadata.
        item = item["item"]
        subtype = _kind(item.get("type"))
    for public_key in ("id", "call_id", "turn_id"):
        value = item.get(public_key)
        if value is not None:
            info["source_" + public_key] = small(value)
    if completed and payload.get("turn_id") is not None:
        info["source_turn_id"] = small(payload["turn_id"])
    if subtype.startswith(("reasoning", "agentreasoning")) or subtype in {"compaction", "configurationupdate", "additionaltools", "hookprompt"}:
        return "ignored", None, [], info
    if _kind(item.get("channel")) in {"analysis", "reasoning"} or _kind(item.get("phase")) in {"analysis", "reasoning"}:
        return "ignored", None, [], info
    if top == "response_item" and subtype in {"message", "agentmessage"}:
        role = "assistant" if subtype == "agentmessage" else small(item.get("role"))
        if role not in {"user", "assistant"}:
            return "ignored", None, [], info
        content = item.get("content", item.get("text"))
        if _unknown_content(content):
            info["coverage_warning"] = "unsupported_record"
        values = _content_parts(content)
        if not values:
            raise RolloutReadError("Message record has no recognized content; cannot claim full coverage.")
        return "userMessage" if role == "user" else "agentMessage", role, [Field("text", values)], info
    if (completed and subtype in {"usermessage", "agentmessage"}) or (top == "event_msg" and subtype in {"usermessage", "agentmessage"}):
        role = "user" if subtype == "usermessage" else "assistant"
        content = item.get("content", item.get("text", item.get("message")))
        if _unknown_content(content):
            info["coverage_warning"] = "unsupported_record"
        values = _content_parts(content)
        if not values:
            raise RolloutReadError("Message event has no recognized content.")
        info["possible_duplicate_representation"] = True
        return "userMessage" if role == "user" else "agentMessage", role, [Field("text", values)], info
    if subtype == "plan":
        return "plan", "plan", [Field("text", [item.get("text", "")])], info
    if top == "event_msg" and subtype in {"turncomplete", "taskcomplete"}:
        fields = [Field("note", ["Stored turn-completion marker; not proof of successful repository work."])]
        if isinstance(item.get("last_agent_message"), (StringSpan, str)):
            fields.append(Field("text", [item["last_agent_message"]]))
            info["possible_duplicate_representation"] = True
        if isinstance(item.get("error"), dict) and isinstance(item["error"].get("message"), (StringSpan, str)):
            fields.append(Field("error", [item["error"]["message"]]))
        return "completionMarker", None, fields, info
    if top == "event_msg" and subtype == "contextcompacted":
        return "contextCompaction", None, [Field("note", ["Stored context-compaction marker."])], info
    if top == "event_msg" and subtype == "threadrolledback":
        return "rollbackMarker", None, [Field("note", ["Rollback marker: earlier raw records may no longer be active in the Codex UI. This fallback does not apply rollback semantics."]), Field("num_turns", [item.get("num_turns")], "json")], {**info, "coverage_warning": "rollback_not_materialized"}
    if top == "event_msg" and subtype in {"turnaborted", "error"}:
        fields = [Field("note", ["Stored failure/abort marker; consult its message rather than inferring an outcome."])]
        if isinstance(item.get("message"), (StringSpan, str)):
            fields.append(Field("message", [item["message"]]))
        return "failureMarker", None, fields, info
    if subtype in {"commandexecution", "execcommandend", "execcommandbegin"}:
        fields = [Field(name, [item[source]], "json") for name, source in (("command", "command"), ("cwd", "cwd"), ("status", "status"), ("exit_code", "exit_code")) if source in item]
        if include_tool_output:
            fields += [Field(name, [item[name]], "text" if isinstance(item[name], (StringSpan, str)) else "json") for name in ("stdout", "stderr", "aggregated_output", "aggregatedOutput", "output") if name in item]
        return "commandExecution", None, fields or [Field("note", ["Command lifecycle record; details not stored."])], info
    if subtype in {"filechange", "patchapplyend", "patchapplybegin"}:
        fields = [Field("note", ["Stored file-change record; payload requires include_diffs=true."])]
        if "status" in item:
            fields.append(Field("status", [item["status"]], "json"))
        if "success" in item:
            fields.append(Field("success", [item["success"]], "json"))
        if include_diffs and "changes" in item:
            fields.append(Field("changes", [item["changes"]], "json"))
        return "fileChange", None, fields, info
    if subtype in {"functioncall", "customtoolcall", "functioncalloutput", "customtoolcalloutput", "dynamictoolcall", "mcptoolcall", "mcptoolcallend", "localshellcall", "toolsearchcall", "toolsearchoutput"}:
        name = small(item.get("name", item.get("tool")), default="unknown")
        fields = [Field("tool", [name])]
        if "status" in item:
            fields.append(Field("status", [item["status"]], "json"))
        # Patch arguments are diffs, even though they are stored as a tool input.
        allowed = include_diffs if _is_patch_tool(name) else include_tool_output
        if allowed:
            for key in ("arguments", "input", "output", "result", "content_items", "contentItems", "error"):
                if key in item:
                    fields.append(Field(key, [item[key]], "text" if isinstance(item[key], (StringSpan, str)) else "json"))
        return "storedToolRecord", None, fields, info
    if subtype in {"websearch", "websearchcall", "websearchend"}:
        fields = [Field("query", [item.get("query", "")])]
        return "webSearch", None, fields, info
    if subtype in {"imagegeneration", "imagegenerationcall", "imagegenerationend", "imageview", "viewimagetoolcall"}:
        fields = [Field("note", [BINARY_MARKER])]
        if "status" in item:
            fields.append(Field("status", [item["status"]], "json"))
        return "imageRecord", None, fields, info
    # Unknown content is never leaked or mistaken for complete transcript support.
    return "unsupportedRecord", None, [Field("note", ["Unsupported stored record type; raw payload not exposed."])], {**info, "coverage_warning": "unsupported_record"}


# These are explicit supported variants, not prefix-based declarations that an
# unknown future record is harmless. See PROTOCOL_NOTES.md for pinned sources.
COLLAB_TYPES = frozenset({
    "collabagentspawnbegin", "collabagentspawnend", "collabagentinteractionbegin",
    "collabagentinteractionend", "collabwaitingbegin", "collabwaitingend",
    "collabclosebegin", "collabcloseend", "collabresumebegin", "collabresumeend",
    "collabagenttoolcall", "subagentactivity",
})
IMAGE_TYPES = frozenset({
    "imagegenerationbegin", "imagegenerationend", "imagegenerationcall",
    "imagegeneration", "imageview", "viewimagetoolcall",
})
CONTROL_TYPES = frozenset({
    "tokencount", "turnstarted", "taskstarted", "shutdowncomplete",
    "websearchbegin",
    "authrecoverystarted", "authrecoverycompleted", "safetybuffering",
})
PRIVATE_TOPS = frozenset({
    "session_meta", "turn_context", "world_state", "retained_context",
    "token_usage_record", "security_risk_score", "inter_agent_communication_metadata",
})
NOTICE_TYPES = frozenset({
    "warning", "deprecationnotice", "streamerror", "streaminfo", "error", "turnaborted",
    "modelreroute", "modelverification", "environmentconnected", "environmentdisconnected",
    "mcpstartupupdate", "mcpstartupcomplete",
})
TYPE_LABEL = re.compile(r"[A-Za-z][A-Za-z0-9_.:/-]{0,95}\Z")
TYPE_BUCKET_LIMIT = 24
STAT_INTS = (
    "records", "projected_records", "known_control_records", "intentionally_excluded_records",
    "unknown_records", "records_with_unknown_content", "compaction_checkpoints",
    "rollback_records", "unknown_type_overflow", "control_type_overflow",
)
STAT_MAPS = ("unknown_type_counts", "known_control_type_counts", "omission_counts", "gap_counts")
OMISSION_CODES = frozenset({
    "private_reasoning", "system_developer_context", "image_audio_binary", "encrypted_content",
    "tool_payloads", "file_diffs", "model_context_metadata", "untyped_compaction_summary",
    "nontext_attachments", "binary_command_stream", "unknown_payload",
})
GAP_CODES = frozenset({
    "unreadable_compaction_checkpoint", "unhandled_checkpoint_shape", "referenced_history_not_read",
    "uninterpreted_realtime_record", "unclassified_known_record_fields",
})


def _label(value: Any, default: str = "missing") -> str:
    """Diagnostic type/field labels only: no arbitrary payload or long strings."""
    try:
        name = small(value, default)
    except RolloutReadError:
        return "invalid_or_oversized_label"
    return name if TYPE_LABEL.fullmatch(name) else "invalid_label"


def _record_info(record: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    payload = record["payload"]
    family = _label(record.get("type"))
    label = family + "." + _label(payload.get("type"), "metadata")
    if item is not payload:
        label += "." + _label(item.get("type"))
    info: dict[str, Any] = {"record_family": family, "source_record_type": label,
        "possible_duplicate_representation": False}
    if record.get("timestamp") is not None:
        info["source_timestamp"] = small(record["timestamp"])
    for key in ("id", "call_id", "turn_id"):
        if item.get(key) is not None:
            info["source_" + key] = small(item[key])
    if payload.get("turn_id") is not None:
        info["source_turn_id"] = small(payload["turn_id"])
    return info


def _typed_omissions(value: Any) -> set[str]:
    """Inspect only structural metadata, never materialize text/binary strings."""
    out: set[str] = set()
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, dict):
            kind = _safe_kind(node.get("type"))
            if kind.startswith(("reasoning", "agentreasoning")):
                out.add("private_reasoning")
                continue
            if _safe_kind(node.get("role")) in {"system", "developer"} or _safe_kind(node.get("channel")) in {"analysis", "reasoning"} or _safe_kind(node.get("phase")) in {"analysis", "reasoning"}:
                out.add("system_developer_context")
                continue
            if kind in {"image", "inputimage", "outputimage", "localimage", "audio", "inputaudio", "outputaudio", "imagegeneration", "imagegenerationcall"}:
                out.add("image_audio_binary")
            if kind in {"file", "inputfile"}:
                out.add("nontext_attachments")
            for key, child in node.items():
                name = key.lower()
                if name in {"encrypted_content", "encryptedcontent"} and child is not None:
                    out.add("encrypted_content")
                elif name in PRIVATE_KEYS:
                    out.add("private_reasoning")
                elif name in BINARY_KEYS or name in {"image_url", "imageurl", "image", "images", "local_images", "audio", "local_audio"}:
                    out.add("image_audio_binary")
                elif isinstance(child, (dict, list)):
                    stack.append(child)
    return out


def _fields(item: dict[str, Any], names: tuple[str, ...]) -> list[Field]:
    return [Field(name, [item[name]], "text" if isinstance(item[name], (StringSpan, str)) else "json")
            for name in names if name in item and item[name] is not None]


def _is_patch_tool(value: Any) -> bool:
    return small(value, default="unknown").casefold() in {"apply_patch", "functions.apply_patch"}


def _status_projection(value: Any, include_payload: bool) -> Any:
    """Status tags are public metadata; completed/error text is tool output."""
    if include_payload or not isinstance(value, dict):
        return value
    # AgentStatus is externally tagged. Preserve tags, not their message payloads.
    return {k: "[agent status payload omitted; include_tool_output=true]" for k in value}


def _agents_projection(value: Any, include_payload: bool) -> Any:
    if isinstance(value, list):
        return [_agents_projection(v, include_payload) for v in value]
    if not isinstance(value, dict):
        return value
    out = {}
    for key in ("thread_id", "threadId", "agent_nickname", "agentNickname", "agent_role", "agentRole", "agent_type", "agent_path", "agentPath", "status"):
        if key in value:
            out[key] = _status_projection(value[key], include_payload) if key == "status" else value[key]
    return out


def project(record: dict[str, Any], *, include_tool_output: bool, include_diffs: bool) -> tuple[str, str | None, list[Field], dict[str, Any]]:
    """Visible historical log projection with explicit exclusions and diagnostics.

    Checkpoint text is labelled as checkpoint text, never as a new executed action.
    This does not replay Codex's private model context or choose an active branch.
    """
    top = small(record.get("type"))
    payload = record.get("payload")
    if not isinstance(payload, dict):
        raise RolloutReadError("A local rollout record lacks an object payload.")
    subtype = _kind(payload.get("type"))
    item = payload
    wrapped = top == "event_msg" and subtype in {"itemstarted", "itemcompleted"}
    if wrapped or (top == "response_item" and isinstance(payload.get("item"), dict)):
        item = payload.get("item")
        if not isinstance(item, dict):
            raise RolloutReadError("A stored item lifecycle event lacks a valid item.")
        subtype = _kind(item.get("type"))
    info = _record_info(record, item)
    if wrapped:
        info["lifecycle"] = "started" if _kind(payload.get("type")) == "itemstarted" else "completed"
        info["possible_duplicate_representation"] = True
    omissions = _typed_omissions(item)
    unknowns: list[str] = []
    gaps: set[str] = set()

    def done(kind: str, role: str | None, fields: list[Field], *, classification: str = "projected", extra: dict[str, Any] | None = None):
        combined = {**info, **(extra or {}), "coverage_class": classification}
        if omissions:
            combined["omission_categories"] = sorted(omissions)
        if unknowns:
            combined["unrecognized_types"] = sorted(set(unknowns))[:TYPE_BUCKET_LIMIT]
            combined["coverage_warning"] = "unsupported_record"
        if gaps:
            combined["projection_gaps"] = sorted(gaps)
        return kind, role, fields, combined

    # Privacy precedence applies even to familiar event/type names.
    if subtype.startswith(("reasoning", "agentreasoning")) or _kind(item.get("channel")) in {"analysis", "reasoning"} or _kind(item.get("phase")) in {"analysis", "reasoning"}:
        omissions.add("private_reasoning")
        return done("ignored", None, [], classification="intentionally_excluded")
    if top == "response_item" and _kind(item.get("role")) in {"system", "developer"}:
        omissions.add("system_developer_context")
        return done("ignored", None, [], classification="intentionally_excluded")
    if top in PRIVATE_TOPS:
        omissions.add("model_context_metadata" if top != "token_usage_record" else "system_developer_context")
        return done("ignored", None, [], classification="intentionally_excluded")

    if top == "compacted":
        info["checkpoint_semantics"] = "model_input_replacement_not_new_actions"
        info["effective_context_reconstructed"] = False
        fields = [Field("note", ["Compaction checkpoint. Public replacement-history fields below describe a model-input snapshot, not new user requests or executed work. Earlier historical events are retained in this log."])]
        fields += _fields(item, ("window_number", "first_window_id", "previous_window_id", "window_id", "compaction_response_id"))
        # The untyped message may be a private model-context summary. Do not
        # disclose it by calling it ordinary conversation text.
        if item.get("message") is not None and not (isinstance(item["message"], (str, StringSpan)) and (len(item["message"]) if isinstance(item["message"], str) else item["message"].chars) == 0):
            omissions.add("untyped_compaction_summary")
        if any(item.get(k) is not None for k in ("guardian_history", "retained_context", "replacement_history_metadata", "mcp_resource_origins")):
            omissions.add("model_context_metadata")
        replacement = item.get("replacement_history")
        if replacement is None:
            gaps.add("unreadable_compaction_checkpoint")
            fields.append(Field("checkpoint_status", ["No typed replacement history is stored; effective context cannot be reconstructed from this marker."]))
        elif not isinstance(replacement, list):
            gaps.add("unhandled_checkpoint_shape")
            fields.append(Field("checkpoint_status", ["Unrecognized replacement-history container; content withheld."]))
        else:
            fields.append(Field("replacement_items", [str(len(replacement))]))
            for index, entry in enumerate(replacement):
                if not isinstance(entry, dict):
                    unknowns.append("checkpoint.invalid_item")
                    continue
                child = entry.get("item", entry)
                if not isinstance(child, dict):
                    unknowns.append("checkpoint.invalid_envelope")
                    continue
                # Nested raw-rollout records are not ResponseItems. Do not guess.
                child_record = {"type": "response_item", "payload": child}
                kind, role, child_fields, child_info = project(child_record, include_tool_output=include_tool_output, include_diffs=include_diffs)
                omissions.update(child_info.get("omission_categories", []))
                unknowns.extend("checkpoint." + name for name in child_info.get("unrecognized_types", []))
                gaps.update(child_info.get("projection_gaps", []))
                if child_info.get("coverage_class") == "unknown" and not child_info.get("unrecognized_types"):
                    unknowns.append("checkpoint." + _label(child.get("type")))
                if child_fields:
                    prefix = f"checkpoint[{index}]"
                    fields.append(Field(prefix + ".record_kind", [kind]))
                    if role:
                        fields.append(Field(prefix + ".role", [role]))
                    for field in child_fields:
                        fields.append(Field(prefix + "." + field.name, field.values, field.encoding))
        return done("contextCompaction", None, fields)

    if top not in {"response_item", "event_msg", "inter_agent_communication", "realtime_item"}:
        unknowns.append(info["source_record_type"])
        omissions.add("unknown_payload")
        return done("unsupportedRecord", None, [Field("note", ["Unknown rollout record family; payload withheld."])], classification="unknown")

    if subtype in {"compaction", "contextcompaction", "compactiontrigger"} and top == "response_item":
        omissions.add("model_context_metadata")
        # An opaque response compaction item is not a visible conversation turn.
        return done("contextCompaction", None, [Field("note", ["Opaque response-level compaction marker; private/encrypted model context is excluded. This is not a new action."])])

    # Paginated histories persist a completed ContextCompaction TurnItem in
    # addition to the separate `compacted` checkpoint record that carries any
    # typed replacement history. The lifecycle marker has no additional public
    # transcript payload and must not be mistaken for an unknown record or a
    # second checkpoint.
    if top == "event_msg" and wrapped and subtype == "contextcompaction":
        omissions.add("model_context_metadata")
        return done("contextCompactionLifecycle", None, [Field("note", ["Context-compaction lifecycle marker. Any recoverable replacement-history fields are projected from the separate compaction checkpoint record."])], classification="known_control")

    # Image generation is stored as a generic Extension TurnItem in newer
    # paginated rollouts. Whitelist only the documented image-generation kind;
    # future extension kinds remain unknown rather than being silently trusted.
    if top == "event_msg" and wrapped and subtype == "extension":
        extension_kind = _kind(item.get("kind"))
        if extension_kind == "imagegen.generation":
            omissions.add("image_audio_binary")
            fields = [Field("note", ["Stored image-generation extension. Image/binary result bytes are omitted; completion status is not proof the saved artifact still exists."]),
                      Field("extension_kind", ["image_gen.generation"])]
            fields += _fields(item, ("status", "saved_path", "savedPath", "transparent_background", "transparentBackground"))
            if isinstance(item.get("failure"), dict):
                fields += [Field("failure." + field.name, field.values, field.encoding)
                           for field in _fields(item["failure"], ("code", "message"))]
            fields += _fields(item, ("error",))
            if include_tool_output:
                fields += _fields(item, ("revised_prompt", "revisedPrompt", "prompt"))
            elif any(k in item for k in ("revised_prompt", "revisedPrompt", "prompt")):
                omissions.add("tool_payloads")
            # `result` is the generated image payload and is never projected.
            return done("imageRecord", None, fields)

    if top == "event_msg" and subtype in CONTROL_TYPES and not wrapped:
        control_fields = {"type", "id", "call_id", "thread_id", "turn_id", "root_turn_id", "timestamp", "started_at", "started_at_ms", "model_context_window", "collaboration_mode", "collaboration_mode_kind", "info", "rate_limits", "total_token_usage", "last_token_usage", "token_usage", "response_id", "response", "usage", "status", "message_id", "request_id", "attempt", "max_attempts", "reason", "duration_ms", "completed_at_ms", "completed_at"}
        if set(item) - control_fields:
            gaps.add("unclassified_known_record_fields")
            unknowns.extend("field." + _label(k) for k in sorted(set(item) - control_fields))
            return done("knownControlWithGap", None, [Field("note", ["Known lifecycle record has unclassified fields; values withheld."])])
        return done("knownControl", None, [], classification="known_control")
    if top == "event_msg" and subtype in {"threadsettingsapplied", "turnmoderationmetadata", "guardianassessment", "guardianwarning"}:
        omissions.add("model_context_metadata")
        return done("ignored", None, [], classification="intentionally_excluded")
    if top == "event_msg" and subtype in {"rawresponsecompleted", "threadqueuechanged"}:
        omissions.add("model_context_metadata")
        if any(k in item for k in ("response", "messages", "queue", "items")):
            gaps.add("referenced_history_not_read")
        return done("workflowContainer", None, [Field("note", ["Known workflow container; embedded response/queue history is not expanded here."])])
    if top == "event_msg" and subtype == "sessionconfigured":
        omissions.add("model_context_metadata")
        if payload.get("initial_messages"):
            gaps.add("referenced_history_not_read")
        return done("configurationMarker", None, [Field("note", ["Session configuration recorded; internal configuration and any initial-message replay are not part of this projection."])])

    if subtype in COLLAB_TYPES or top == "inter_agent_communication" or (top == "response_item" and subtype == "agentmessage" and any(k in item for k in ("author", "recipient"))):
        info["linked_sessions_read"] = False
        fields = [Field("event", [info["source_record_type"]])]
        fields += _fields(item, ("tool", "sender_thread_id", "senderThreadId", "receiver_thread_id", "receiverThreadId", "receiver_thread_ids", "receiverThreadIds", "new_thread_id", "newThreadId", "new_agent_nickname", "new_agent_role", "receiver_agent_nickname", "receiver_agent_role", "agent_thread_id", "agent_path", "agentPath", "kind", "author", "recipient", "other_recipients", "trigger_turn", "model"))
        for key in ("status", "agent_statuses", "agentStatuses", "receiver_agents", "receiverAgents", "statuses"):
            if key not in item:
                continue
            if key == "status":
                value = _status_projection(item[key], include_tool_output)
            elif key == "statuses" and isinstance(item[key], dict):
                value = {k: _status_projection(v, include_tool_output) for k, v in item[key].items()}
            else:
                value = _agents_projection(item[key], include_tool_output)
            fields.append(Field(key, [value], "json"))
        payload_names = ("prompt", "message", "content", "completion_message", "result", "error")
        if include_tool_output:
            fields += _fields(item, payload_names)
        elif any(k in item for k in payload_names) or any(k in item for k in ("status", "statuses", "agent_statuses", "agentStatuses")):
            omissions.add("tool_payloads")
        return done("collaborationEvent", None, fields)

    if subtype in IMAGE_TYPES:
        omissions.add("image_audio_binary")
        fields = [Field("note", ["Stored image operation. Image/binary bytes are omitted; a status is not proof the saved artifact still exists."])]
        fields += _fields(item, ("status", "saved_path", "savedPath", "path"))
        if isinstance(item.get("failure"), dict):
            fields += [Field("failure." + field.name, field.values, field.encoding) for field in _fields(item["failure"], ("code", "message"))]
        fields += _fields(item, ("error",))
        if include_tool_output:
            fields += _fields(item, ("revised_prompt", "revisedPrompt", "prompt"))
        elif any(k in item for k in ("revised_prompt", "revisedPrompt", "prompt")):
            omissions.add("tool_payloads")
        return done("imageRecord", None, fields)

    if top == "realtime_item":
        # Know the family, but do not pretend every realtime schema is understood.
        omissions.add("image_audio_binary")
        gaps.add("uninterpreted_realtime_record")
        unknowns.append(info["source_record_type"])
        return done("unsupportedRecord", None, [Field("note", ["Realtime record requires a typed transcript decoder; raw payload withheld."])], classification="unknown")

    if top == "event_msg" and subtype in NOTICE_TYPES:
        fields = [Field("event", [info["source_record_type"]])]
        fields += _fields(item, ("message", "reason", "code", "summary", "details", "additional_details", "status", "server", "ready", "failed", "cancelled", "from_model", "to_model"))
        return done("failureMarker" if subtype in {"error", "streamerror", "turnaborted"} else "workflowNotice", None, fields)

    if subtype in {"planupdate", "threadgoalupdated", "enteredreviewmode", "exitedreviewmode", "enteredreview", "exitedreview"}:
        names = ("explanation", "plan", "goal", "status", "review", "review_output", "reviewOutput", "user_facing_hint", "target", "review_request")
        return done("planningOrReviewRecord", None, _fields(item, names) or [Field("note", ["Stored planning/review lifecycle marker."])])

    if top == "event_msg" and subtype in {"agentmessagecontentdelta", "agentmessagedelta", "plandelta"}:
        info["possible_duplicate_representation"] = True
        info["delta_not_complete_message"] = True
        fields = _fields(item, ("delta", "text"))
        if not fields:
            unknowns.append(info["source_record_type"] + ".missing_text")
        return done("messageDelta", "assistant", fields or [Field("note", ["Message delta missing recognized text."])])

    if top == "event_msg" and subtype in {"terminalinteraction", "execcommandoutputdelta"}:
        fields = _fields(item, ("call_id", "process_id", "stream"))
        if subtype == "execcommandoutputdelta":
            omissions.add("binary_command_stream")
            fields.append(Field("note", ["Byte-stream command delta omitted; inspect the corresponding command-end output for decoded text."]))
        elif include_tool_output:
            fields += _fields(item, ("stdin",))
        else:
            omissions.add("tool_payloads")
        return done("commandStreamRecord", None, fields)

    if subtype in {"patchapplyupdated", "turndiff"}:
        fields = [Field("note", ["Stored patch/diff update; not proof of successful application."])]
        if include_diffs:
            fields += _fields(item, ("changes", "unified_diff"))
        else:
            omissions.add("file_diffs")
        return done("fileChange", None, fields)

    if subtype in {"mcptoolcallbegin", "mcptoolcallend", "dynamictoolcallrequest", "dynamictoolcallresponse", "requestuserinput", "elicitationrequest", "execapprovalrequest", "applypatchapprovalrequest", "requestpermissions"}:
        fields = [Field("event", [info["source_record_type"]])]
        invocation = item.get("invocation")
        tool_name = item.get("tool", item.get("name"))
        if isinstance(invocation, dict):
            fields += _fields(invocation, ("server", "tool"))
            tool_name = invocation.get("tool", tool_name)
            payload_allowed = include_diffs if _is_patch_tool(tool_name) else include_tool_output
            if payload_allowed:
                fields += _fields(invocation, ("arguments",))
        else:
            payload_allowed = include_diffs if _is_patch_tool(tool_name) else include_tool_output
        fields += _fields(item, ("server", "tool", "name", "status", "success", "reason"))
        if payload_allowed:
            fields += _fields(item, ("arguments", "input", "result", "output", "content_items", "contentItems", "error", "questions"))
        else:
            omissions.add("file_diffs" if _is_patch_tool(tool_name) else "tool_payloads")
        if include_diffs:
            fields += _fields(item, ("changes",))
        elif "changes" in item:
            omissions.add("file_diffs")
        return done("storedToolRecord", None, fields)

    if subtype in {"hookstarted", "hookcompleted"}:
        omissions.add("model_context_metadata")
        fields = [Field("note", ["Hook lifecycle record. Internal hook prompts/instructions are omitted."])]
        fields += _fields(item, ("status", "status_message", "event_name", "handler_type", "duration_ms"))
        return done("hookLifecycle", None, fields)
    if subtype == "sleep":
        return done("sleepRecord", None, _fields(item, ("duration", "duration_ms", "seconds", "status")) or [Field("note", ["Stored sleep/wait marker."])])

    if top == "event_msg" and subtype == "rawresponseitem" and isinstance(item.get("item"), dict):
        kind, role, fields, child_info = project({"type": "response_item", "payload": item["item"]}, include_tool_output=include_tool_output, include_diffs=include_diffs)
        return kind, role, fields, {**child_info, **info, "possible_duplicate_representation": True}

    # Retain tested 0.2.4 handling of ordinary messages and opt-in tool payloads.
    delegate = record
    if wrapped and _kind(payload.get("type")) == "itemstarted":
        delegate = {**record, "payload": {**payload, "type": "item_completed"}}
    kind, role, fields, old_info = _legacy_project(delegate, include_tool_output=include_tool_output, include_diffs=include_diffs)
    if kind == "unsupportedRecord":
        unknowns.append(info["source_record_type"])
        omissions.add("unknown_payload")
    elif old_info.get("coverage_warning") == "unsupported_record":
        content = item.get("content", item.get("text", item.get("message")))
        if isinstance(content, list):
            for block in content:
                if _unknown_content([block]):
                    unknowns.append("content." + _label(block.get("type")))
    if not include_tool_output and kind in {"storedToolRecord", "commandExecution"}:
        omissions.add("tool_payloads")
    if kind == "fileChange" and not include_diffs:
        omissions.add("file_diffs")
    if kind == "fileChange" and include_tool_output:
        fields += _fields(item, ("stdout", "stderr"))
    classification = "unknown" if kind == "unsupportedRecord" else "intentionally_excluded" if kind == "ignored" else "projected"
    if classification == "intentionally_excluded" and not omissions:
        omissions.add("model_context_metadata")
    return done(kind, role, fields, classification=classification, extra=old_info)


def empty_stats() -> dict[str, Any]:
    return {**{key: 0 for key in STAT_INTS}, **{key: {} for key in STAT_MAPS}}


def _count_bucket(stats: dict[str, Any], name: str, label: str) -> None:
    table = stats[name]
    if label in table or len(table) < TYPE_BUCKET_LIMIT:
        table[label] = table.get(label, 0) + 1
    elif name == "unknown_type_counts":
        stats["unknown_type_overflow"] += 1
    elif name == "known_control_type_counts":
        stats["control_type_overflow"] += 1
    else:
        raise RolloutReadError("Coverage classification bounds exceeded; no completeness claim returned.")


def account_record(stats: dict[str, Any], kind: str, info: dict[str, Any]) -> None:
    """Count a source record once, on its first accepted fragment or exclusion."""
    stats["records"] += 1
    classification = info.get("coverage_class", "unknown")
    target = {"projected": "projected_records", "known_control": "known_control_records",
              "intentionally_excluded": "intentionally_excluded_records", "unknown": "unknown_records"}[classification]
    stats[target] += 1
    if classification == "known_control":
        _count_bucket(stats, "known_control_type_counts", info["source_record_type"])
    unknowns = info.get("unrecognized_types", [])
    if unknowns and classification != "unknown":
        stats["records_with_unknown_content"] += 1
    for label in unknowns:
        _count_bucket(stats, "unknown_type_counts", label)
    for omission in info.get("omission_categories", []):
        _count_bucket(stats, "omission_counts", omission)
    for gap in info.get("projection_gaps", []):
        _count_bucket(stats, "gap_counts", gap)
    stats["compaction_checkpoints"] += int(kind == "contextCompaction")
    stats["rollback_records"] += int(kind == "rollbackMarker")


def validate_stats(stats: Any) -> None:
    if not isinstance(stats, dict) or set(stats) != set(STAT_INTS + STAT_MAPS):
        raise RolloutReadError("Invalid coverage counters in local token; restart the read.")
    if any(type(stats[k]) is not int or stats[k] < 0 for k in STAT_INTS):
        raise RolloutReadError("Invalid coverage counters in local token.")
    if stats["records"] != sum(stats[k] for k in ("projected_records", "known_control_records", "intentionally_excluded_records", "unknown_records")):
        raise RolloutReadError("Inconsistent coverage counters in local token.")
    for name in STAT_MAPS:
        values = stats[name]
        if not isinstance(values, dict) or len(values) > TYPE_BUCKET_LIMIT or any(not isinstance(k, str) or len(k) > 384 or re.fullmatch(r"[A-Za-z0-9_.:/-]+", k) is None or type(v) is not int or v < 1 for k, v in values.items()):
            raise RolloutReadError("Invalid coverage histogram in local token.")
    if set(stats["omission_counts"]) - OMISSION_CODES or set(stats["gap_counts"]) - GAP_CODES:
        raise RolloutReadError("Unknown coverage classification in local token.")


def coverage_report(stats: dict[str, Any], eof: bool) -> dict[str, Any]:
    unknown = stats["unknown_records"] + stats["records_with_unknown_content"]
    projection_complete = eof and unknown == 0 and not stats["gap_counts"]
    record_support_complete = (unknown == 0) if eof else None
    return {
        "scope": "allowlisted_visible_fields_in_one_chronological_local_file",
        "traversal_complete": eof,
        "projection_coverage_complete": projection_complete,
        "record_support_complete": record_support_complete,
        "unknown_records": unknown,
        "unknown_type_counts": dict(sorted(stats["unknown_type_counts"].items())),
        "unknown_type_inventory_overflow": stats["unknown_type_overflow"],
        "known_control_records": stats["known_control_records"],
        "known_control_type_counts": dict(sorted(stats["known_control_type_counts"].items())),
        "intentionally_omitted_data": dict(sorted(stats["omission_counts"].items())),
        "projection_gaps": dict(sorted(stats["gap_counts"].items())),
        "records_accounted_for": stats["records"],
        "compaction_checkpoints": stats["compaction_checkpoints"],
        "rollback_records": stats["rollback_records"],
        "semantic_coverage_complete": None,
        "semantic_coverage_status": "not_assessed_by_a_record_reader",
        "effective_context_reconstructed": False,
        "raw_fidelity_complete": False,
        "repository_state_verified": False,
        "linked_sessions_read": False,
        "definition": "Projection completeness means all recognized allowlisted fields were traversed under the selected privacy flags. It does not prove that every relevant fact, image, sub-agent session, effective model-context item or current repository change was recovered.",
    }


def _linked(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def find_rollout(thread_id: str, hint: str | None = None) -> Path:
    if UUID.fullmatch(thread_id) is None:
        raise RolloutReadError("Local fallback requires an exact UUID thread ID, not a path or arbitrary string.")
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
    roots = [home / "sessions", home / "archived_sessions"]
    def acceptable(path: Path) -> bool:
        try:
            if path.suffix.lower() != ".jsonl" or not path.name.lower().startswith("rollout-") or not path.name.lower().endswith(thread_id.lower() + ".jsonl"):
                return False
            for root in roots:
                if _linked(root):
                    continue
                try:
                    path.absolute().relative_to(root)
                except ValueError:
                    continue
                cursor = path
                while cursor != home:
                    if _linked(cursor):
                        return False
                    cursor = cursor.parent
                return path.is_file() and path.resolve().is_relative_to(root.resolve())
        except (OSError, RuntimeError):
            return False
        return False
    if hint and acceptable(Path(hint)):
        return Path(hint).resolve()
    found = []
    examined = 0
    compressed = False
    def scan_error(_: OSError) -> None:
        raise RolloutReadError("Cannot inspect a permitted rollout directory; check local read permissions.")
    for root in roots:
        if not root.exists() or _linked(root):
            continue
        for directory, dirs, names in os.walk(root, followlinks=False, onerror=scan_error):
            dirs[:] = [d for d in dirs if not _linked(Path(directory) / d)]
            examined += len(names)
            if examined > 500_000:
                raise RolloutReadError("Rollout filename-discovery bound reached; no content files were broadly scanned.")
            for name in names:
                if name.lower().endswith(thread_id.lower() + ".jsonl.zst"):
                    compressed = True
                candidate = Path(directory) / name
                if name.lower().endswith(thread_id.lower() + ".jsonl") and acceptable(candidate):
                    found.append(candidate.resolve())
    if len(set(found)) != 1:
        if compressed and not found:
            raise RolloutReadError("Only compressed rollout storage was found. This targeted fallback supports plain JSONL only; it did not edit or decompress Codex files.")
        raise RolloutReadError("No unique permitted JSONL rollout was found for this thread. Missing, ambiguous, linked, or external files are not guessed.")
    return found[0]


def stat_identity(s: os.stat_result) -> dict[str, int]:
    if not stat.S_ISREG(s.st_mode):
        raise RolloutReadError("Fallback target is not a regular file.")
    # Windows pathname stat and open-handle fstat can disagree on ctime.
    # Size, content mtime and stable file identity remain mandatory.
    return {"size": s.st_size, "mtime": s.st_mtime_ns, "dev": s.st_dev, "ino": s.st_ino}


def identity(handle: BinaryIO) -> dict[str, int]:
    return stat_identity(os.fstat(handle.fileno()))


def _purge_token_cache(now: float) -> None:
    expired = [token for token, (accessed, _) in _TOKEN_CACHE.items() if now - accessed > TOKEN_TTL_SECONDS]
    for token in expired:
        _TOKEN_CACHE.pop(token, None)
    while len(_TOKEN_CACHE) > TOKEN_CACHE_LIMIT:
        _TOKEN_CACHE.popitem(last=False)


def clear_token_cache() -> None:
    """Clear process-local continuation state. Intended for tests and shutdown diagnostics."""
    with _TOKEN_LOCK:
        _TOKEN_CACHE.clear()


def pack(state: dict[str, Any]) -> str:
    """Return a short process-local handle instead of asking the model to echo state JSON.

    The secure tunnel keeps one MCP server process alive for a traversal. The
    full state remains only in bounded memory and contains no transcript text.
    A bridge restart intentionally invalidates outstanding handles.
    """
    raw = _wire(state).encode("utf-8")
    if len(raw) > MAX_STATE_BYTES:
        raise RolloutReadError("Local continuation state exceeds its safe limit.")
    token = PREFIX + hashlib.sha256(raw).hexdigest()[:40]
    now = time.monotonic()
    with _TOKEN_LOCK:
        _purge_token_cache(now)
        existing = _TOKEN_CACHE.get(token)
        if existing is not None and existing[1] != raw:
            raise RolloutReadError("Local continuation identifier collision; restart this read.")
        _TOKEN_CACHE[token] = (now, raw)
        _TOKEN_CACHE.move_to_end(token)
        while len(_TOKEN_CACHE) > TOKEN_CACHE_LIMIT:
            _TOKEN_CACHE.popitem(last=False)
    return token


def unpack(token: str, thread_id: str, options: dict[str, bool]) -> dict[str, Any]:
    if isinstance(token, str):
        token = token.strip()
    if not isinstance(token, str) or _TOKEN_RE.fullmatch(token) is None:
        raise RolloutReadError("Invalid local continuation token; copy next_page_token exactly.")
    now = time.monotonic()
    with _TOKEN_LOCK:
        _purge_token_cache(now)
        entry = _TOKEN_CACHE.get(token)
        if entry is None:
            raise RolloutReadError("Local continuation token expired or the bridge process restarted. Restart this session read without page_token and do not combine the abandoned partial traversal with the new one.")
        raw = entry[1]
        _TOKEN_CACHE[token] = (now, raw)
        _TOKEN_CACHE.move_to_end(token)
    try:
        state = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise RolloutReadError("Stored local continuation state is corrupt; restart this session read.") from exc
    if not isinstance(state, dict) or state.get("v") != 2 or state.get("thread_id") != thread_id or state.get("options") != options:
        raise RolloutReadError("Local token has different session/privacy settings; restart without a token.")
    for name in ("record", "field", "text", "limit"):
        if type(state.get(name)) is not int or state[name] < 0:
            raise RolloutReadError("Invalid local continuation offset.")
    if not 1 <= state["limit"] <= 100:
        raise RolloutReadError("Invalid local continuation limits.")
    snapshot = state.get("snapshot")
    if not isinstance(snapshot, dict) or set(snapshot) != {"size", "mtime", "dev", "ino"} or any(type(v) is not int or v < 0 for v in snapshot.values()):
        raise RolloutReadError("Invalid rollout snapshot in token.")
    if state["record"] > snapshot["size"]:
        raise RolloutReadError("Local token points past end of file.")
    validate_stats(state.get("stats"))
    return state


def _field_slice(field: Field, handle: BinaryIO, start: int, bound: int) -> tuple[str, int]:
    """Count projected text exactly, retaining only this response-sized slice."""
    total = 0
    selected = []
    for part in field.parts(handle):
        end = total + len(part)
        if end > start and total < start + bound:
            selected.append(part[max(0, start - total):max(0, min(len(part), start + bound - total))])
        total = end
    if start > total:
        raise RolloutReadError("Local continuation text offset exceeds the selected field.")
    return "".join(selected), total


def read_page(*, thread_id: str, max_turns: int, max_chars: int, include_tool_output: bool, include_diffs: bool, page_token: str | None = None, hint: str | None = None, restarted: bool = False) -> dict[str, Any]:
    """Return the next bounded raw-record page; do not load whole strings/files."""
    options = {"tool_output": include_tool_output, "diffs": include_diffs}
    state = unpack(page_token, thread_id, options) if page_token else None
    path = find_rollout(thread_id, hint)
    # No writable mode, mmap, temp transcript file, subprocess or network here.
    with path.open("rb") as scan_handle, path.open("rb") as text_handle:
        snapshot = identity(scan_handle)
        if identity(text_handle) != snapshot:
            raise RolloutReadError("Rollout changed while opening it; retry after Codex is idle.")
        if state and snapshot != state["snapshot"]:
            raise RolloutReadError("Local rollout changed since the prior page. Restart without a token after Codex is idle; do not combine snapshots.")
        first = Scanner(scan_handle, snapshot["size"]).record()
        if first is None or small(first[2].get("type")) != "session_meta" or not isinstance(first[2].get("payload"), dict):
            raise RolloutReadError("Rollout does not begin with session_meta; refusing to guess session ownership.")
        meta = first[2]["payload"]
        if small(meta.get("id")).lower() != thread_id.lower():
            raise RolloutReadError("Rollout session_meta ID does not match the requested thread.")
        # Do not silently omit inherited prefix references. Physical copies of
        # ancestor records are supported; out-of-file referenced history is not.
        if any(meta.get(key) is not None for key in ("forked_from_rollout", "forked_from_rollout_id", "forked_from_ordinal", "forked_from_ordinal_exclusive", "rollout_reference", "history_base")):
            raise RolloutReadError("Referenced parent rollout history requires Codex materialization. This fallback will not read additional session files or claim a complete fork.")
        limit = state["limit"] if state else max_turns
        offset = state["record"] if state else first[1]
        field_start = state["field"] if state else 0
        text_start = state["text"] if state else 0
        stats = state["stats"] if state else empty_stats()
        if state is None:
            # Ownership/session metadata is examined once, never exposed.
            meta_kind, _, _, meta_info = project(first[2], include_tool_output=include_tool_output, include_diffs=include_diffs)
            account_record(stats, meta_kind, meta_info)
        if offset and offset != snapshot["size"]:
            scan_handle.seek(offset - 1)
            if scan_handle.read(1) != b"\n":
                raise RolloutReadError("Local continuation is not at a JSONL record boundary.")
        scanner = Scanner(scan_handle, snapshot["size"], offset)
        turns: list[dict[str, Any]] = []
        scanned = 0
        next_position: tuple[int, int, int] | None = None
        char_limited = False
        reached_end = False
        first_record = True
        while scanned < MAX_SCAN_RECORDS and len(turns) < limit:
            entry = scanner.record()
            if entry is None:
                reached_end = True
                break
            start, end, record = entry
            scanned += 1
            kind, role, fields, extra = project(record, include_tool_output=include_tool_output, include_diffs=include_diffs)
            current_field = field_start if first_record else 0
            current_text = text_start if first_record else 0
            first_record = False
            if not fields:
                if current_field or current_text:
                    raise RolloutReadError("Local continuation points into an excluded record.")
                account_record(stats, kind, extra)
                continue
            if current_field >= len(fields):
                raise RolloutReadError("Local continuation field no longer exists.")
            record_id = "rollout:" + str(start)
            row = {"id": record_id, "status": "stored_record", "turn_index": start, "items": [{"id": record_id, "type": kind, "item_index": start, "continued_from_previous_page": bool(current_field or current_text), "item_complete": False, "fragments": [], **extra}]}
            if role:
                row["items"][0]["role"] = role
            # One stored record is a synthetic group, not a claim about Codex turn IDs.
            for f in range(current_field, len(fields)):
                field = fields[f]
                x = current_text if f == current_field else 0
                available, total = _field_slice(field, text_handle, x, max_chars)
                frag = {"field": field.name, "encoding": field.encoding, "offset": x, "end_offset": x, "total_chars": total, "complete": False, "text": ""}
                def candidate(count: int) -> tuple[dict[str, Any], int]:
                    fragment = {**frag, "text": available[:count], "end_offset": x + count, "complete": x + count == total}
                    row["items"][0]["fragments"].append(fragment)
                    row["items"][0]["item_complete"] = f == len(fields) - 1 and fragment["complete"]
                    size = len(_wire(turns + [row]))
                    row["items"][0]["fragments"].pop()
                    return fragment, size
                fragment, size = candidate(len(available))
                if size > max_chars:
                    lo, hi = 0, len(available)
                    while lo < hi:
                        mid = (lo + hi + 1) // 2
                        _, n = candidate(mid)
                        if n <= max_chars:
                            lo = mid
                        else:
                            hi = mid - 1
                    fragment, size = candidate(lo)
                if size > max_chars or (total > x and not fragment["text"]):
                    next_position = (start, f, x)
                    char_limited = True
                    break
                row["items"][0]["fragments"].append(fragment)
                row["items"][0]["item_complete"] = f == len(fields) - 1 and fragment["complete"]
                if not fragment["complete"]:
                    next_position = (start, f, fragment["end_offset"])
                    char_limited = True
                    break
            if row["items"][0]["fragments"]:
                # Candidate probes may set a flag for a rejected empty fragment.
                row["items"][0]["item_complete"] = not (next_position and next_position[0] == start)
                turns.append(row)
                if current_field == 0 and current_text == 0:
                    account_record(stats, kind, extra)
            if next_position:
                if not turns:
                    raise RolloutReadError("max_chars leaves no space for recovery metadata/content; increase it.")
                break
        if next_position is None and not reached_end:
            scanner.spaces(lines=True)
            if scanner.peek() is None:
                reached_end = True
            else:
                # Use the previous record's newline boundary (not leading-space start).
                next_position = (end if scanned else offset, 0, 0)
        if identity(scan_handle) != snapshot or identity(text_handle) != snapshot or stat_identity(path.stat()) != snapshot:
            raise RolloutReadError("Rollout changed during the page read; no continuation or completion claim returned.")
        next_token = None
        if next_position:
            next_token = pack({"v": 2, "thread_id": thread_id, "snapshot": snapshot, "options": options, "record": next_position[0], "field": next_position[1], "text": next_position[2], "limit": limit, "stats": stats})
        payload_chars = len(_wire(turns))
        content_chars = sum(len(f["text"]) for t in turns for i in t["items"] for f in i["fragments"])
        report = coverage_report(stats, reached_end)
        unsupported = report["unknown_records"]
        rollback = bool(stats["rollback_records"])
        coverage_complete = report["projection_coverage_complete"] and not rollback
        warnings = ["Recovery is a chronological raw-record projection, not the Codex UI. Message/event representations may repeat; do not count duplicates as separate actions. Synthetic IDs identify byte positions, not native turn IDs."]
        if restarted:
            warnings.append("SOURCE RESTART: discard previously collected App Server pages for this session when assembling this recovery transcript. Recovery restarted from the local file beginning.")
        if unsupported:
            warnings.append("Unrecognized record/content variants remain. See coverage_report.unknown_type_counts; their payloads were not exposed and projection coverage is incomplete.")
        if rollback:
            warnings.append("Rollback markers are present but not applied. Do not treat earlier raw actions as the current active branch without verification.")
        if stats["compaction_checkpoints"]:
            warnings.append("Compaction checkpoints are labelled model-input snapshots, not new actions. Public typed replacement fields are included under the chosen privacy flags; private/opaque summaries are excluded. Effective model context is not replayed.")
        if stats["gap_counts"]:
            warnings.append("Known projection gaps remain; see coverage_report.projection_gaps. End of file does not close those gaps.")
        return {
            "output_format": "field_fragments_v1", "thread": {"id": thread_id}, "turns": turns,
            "history_source": "local_rollout_fallback", "source_order": "chronological_records",
            "page_complete": not char_limited, "character_truncated": char_limited,
            "has_older_turns": False, "has_more_content": next_token is not None,
            "next_page_token": next_token, "newer_page_token": None,
            "continuation_reason": "same_rollout_record" if char_limited else "next_rollout_records" if next_token else None,
            "turns_returned": len(turns), "items_returned": len(turns),
            "fragments_returned": sum(len(t["items"][0]["fragments"]) for t in turns),
            "chars_returned": content_chars, "history_restart_required": restarted,
            "coverage_complete": coverage_complete,
            "traversal_complete": reached_end,
            "projection_coverage_complete": report["projection_coverage_complete"],
            "record_support_complete": report["record_support_complete"],
            "semantic_coverage_complete": None,
            "raw_fidelity_complete": False,
            "coverage_report": report,
            "output_info": {"max_chars": max_chars, "max_turns": limit, "payload_chars": payload_chars, "content_chars": content_chars, "truncated": char_limited},
            "fallback_info": {"read_only": True, "records_scanned_this_call": scanned, "record_scan_limit": MAX_SCAN_RECORDS, "source_file_bytes": snapshot["size"], "end_of_local_file": reached_end, "unsupported_records_seen": unsupported, "rollback_seen": rollback, "synthetic_record_groups": True, "binary_data_omitted": True},
            "warnings": warnings,
            "privacy_note": "Only allowlisted fields are projected. Reasoning, system/developer messages, opaque compaction summaries and typed image/binary bytes remain excluded. Typed public replacement-history text is labelled as checkpoint content. Collaboration prompts/status payloads and other tool data require include_tool_output; file diffs require include_diffs. This is not a general-purpose secret detector.",
            "pagination_note": "Use next_page_token whenever has_more_content=true. Recovery proceeds chronologically through this ONE JSONL file. max_turns bounds synthetic record groups, not native turns. Assemble fragments by offsets. If history_restart_required=true, discard prior App Server pages for this session rather than combining the two projections. End of file does not establish current repository state or full recovery of unsupported records.",
        }
