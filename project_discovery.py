"""Verified visible-history search and explicit multi-directory project scopes.

No model calls, native search index, legacy scan-and-repair, disk index, or
persistent transcript cache. Search consumes the existing reader internally;
only bounded matching excerpts leave this module. References are process-local
bookmarks, not authorization. Completeness is limited to the observed inventory
and selected visible-message projection, not conceptual project membership.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import secrets
import threading
import time
from collections import OrderedDict
from typing import Any

import codex_bridge as bridge
import session_resolver as resolver

MAX_ROOTS = 32
MAX_SESSIONS = 1000
MAX_ROWS = 5000
MAX_INVENTORY_PAGES = 500
MAX_STATE_BYTES = 4 * 1024 * 1024
CATALOG_BATCH = 20
CALL_SECONDS = 25.0
READ_SECONDS = 25.0
MAX_QUERY_CHARS = 256
EXCERPTS_PER_SESSION = 3
EXCERPT_CONTEXT = 80
SEARCH_TURNS = 100
SEARCH_CHARS = 120000
SEARCH_IDLE_SECONDS = 72 * 60 * 60
SEARCH_ABSOLUTE_SECONDS = 30 * 24 * 60 * 60


class SearchHandleCache:
    """Bounded process-local search chains with transactional sliding expiry.

    Lookup never refreshes a handle. A caller must finish a valid continuation
    and commit its new state before the idle clock moves. The stable random
    handle is only a bookmark; lifecycle metadata remains process-local.
    """

    def __init__(self, prefix: str, *, limit: int, idle_ttl: float, absolute_ttl: float):
        if not 0 < idle_ttl < absolute_ttl:
            raise ValueError('Search handle TTLs must satisfy 0 < idle < absolute.')
        self.prefix, self.limit = prefix, limit
        self.idle_ttl, self.absolute_ttl = idle_ttl, absolute_ttl
        self._data: OrderedDict[str, tuple[float, float, int, dict[str, Any]]] = OrderedDict()
        self._lock = threading.RLock()

    def _validate_token(self, token: str) -> None:
        if not isinstance(token, str) or not re.fullmatch(re.escape(self.prefix) + r'[0-9a-f]{40}', token):
            raise resolver.ResolutionError('Invalid search reference format. Copy next_page_token unchanged; do not edit or combine search chains.')

    def _ages(self, created: float, accessed: float, now: float, *, active: bool = True) -> dict[str, Any]:
        return {'active': active, 'idle_age_seconds': max(0.0, now - accessed),
                'idle_ttl_seconds': self.idle_ttl,
                'absolute_age_seconds': max(0.0, now - created),
                'absolute_ttl_seconds': self.absolute_ttl}

    def _expired(self, created: float, accessed: float, now: float) -> bool:
        return now - accessed >= self.idle_ttl or now - created >= self.absolute_ttl

    def _expired_error(self, created: float, accessed: float, now: float) -> resolver.ResolutionError:
        ages = self._ages(created, accessed, now, active=False)
        return resolver.ResolutionError(
            'Search reference expired. Restart the search from page 1 and do not combine the abandoned chain with a replacement chain. '
            f"Idle age {ages['idle_age_seconds']:.3f}s / TTL {ages['idle_ttl_seconds']:.3f}s; "
            f"absolute age {ages['absolute_age_seconds']:.3f}s / TTL {ages['absolute_ttl_seconds']:.3f}s.")

    def _prune(self, now: float) -> None:
        for token, (created, accessed, _, _) in list(self._data.items()):
            if self._expired(created, accessed, now):
                self._data.pop(token, None)

    def put(self, state: dict[str, Any]) -> str:
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            token = self.prefix + secrets.token_hex(20)
            self._data[token] = (now, now, 0, copy.deepcopy(state))
            while len(self._data) > self.limit:
                self._data.popitem(last=False)
            return token

    def begin(self, token: str) -> tuple[dict[str, Any], int]:
        self._validate_token(token)
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(token)
            if entry is None:
                raise resolver.ResolutionError('Search reference was evicted or belongs to another bridge process. Restart the search from page 1 and do not combine abandoned chains.')
            created, accessed, generation, state = entry
            if self._expired(created, accessed, now):
                self._data.pop(token, None)
                raise self._expired_error(created, accessed, now)
            return copy.deepcopy(state), generation

    def get(self, token: str) -> dict[str, Any]:
        """Non-refreshing compatibility/diagnostic lookup."""
        state, _ = self.begin(token)
        return state

    def commit(self, token: str, generation: int, state: dict[str, Any], *, keep: bool = True) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(token)
            if entry is None:
                raise resolver.ResolutionError('Search reference was evicted or belongs to another bridge process. Restart the search from page 1 and do not combine abandoned chains.')
            created, accessed, current_generation, _ = entry
            if self._expired(created, accessed, now):
                self._data.pop(token, None)
                raise self._expired_error(created, accessed, now)
            if current_generation != generation:
                raise resolver.ResolutionError('Search continuation was already advanced by another call. Use only one sequential chain; restart from page 1 if its pages can no longer be trusted.')
            diagnostics = self._ages(created, now, now, active=keep)
            if keep:
                self._data[token] = (created, now, generation + 1, copy.deepcopy(state))
                self._data.move_to_end(token)
            else:
                self._data.pop(token, None)
            return diagnostics

    def diagnostics(self, token: str) -> dict[str, Any]:
        self._validate_token(token)
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(token)
            if entry is None:
                raise resolver.ResolutionError('Search reference is no longer active in this bridge process.')
            created, accessed, _, _ = entry
            if self._expired(created, accessed, now):
                self._data.pop(token, None)
                raise self._expired_error(created, accessed, now)
            return self._ages(created, accessed, now)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

# Large search/catalog state is deliberately NOT held in the 4096-entry caches.
_SEARCH = SearchHandleCache('cbh1_', limit=16, idle_ttl=SEARCH_IDLE_SECONDS,
                            absolute_ttl=SEARCH_ABSOLUTE_SECONDS)
_SCOPES = resolver.HandleCache('cbp1_', limit=64)
_SCOPE_PAGES = resolver.HandleCache('cbg1_', limit=16)


class DiscoveryError(resolver.ResolutionError):
    pass


def _check_state_size(state: dict[str, Any]) -> None:
    if len(json.dumps(state, ensure_ascii=True).encode('ascii')) > MAX_STATE_BYTES:
        raise DiscoveryError('Discovery state reached its memory bound. Refine the scope; no complete search or catalog is claimed.')


def _put(cache: Any, state: dict[str, Any]) -> str:
    _check_state_size(state)
    return cache.put(state)


def _roots(roots: list[str] | None, *, required: bool = False) -> list[str] | None:
    if roots is None and not required:
        return None
    if not isinstance(roots, list) or not 1 <= len(roots) <= MAX_ROOTS:
        raise DiscoveryError('roots must contain 1..32 explicitly selected absolute directories.')
    unique: dict[str, str] = {}
    for path in roots:
        clean = resolver._text(path, 'root')
        unique.setdefault(resolver.canonical_path(clean), clean)
    return list(unique.values())


def _error_reason(exc: Exception) -> str:
    # Classify known diagnostic wording without returning arbitrary payloads.
    if isinstance(exc, asyncio.TimeoutError):
        return 'timeout'
    text = str(exc).casefold()
    categories = (
        ('reference_expired_or_evicted', ('expired', 'evicted', 'another bridge process')),
        ('permission_or_authentication', ('permission denied', 'access denied', 'forbidden', 'unauthorized', 'authentication')),
        ('rollout_storage_unavailable', ('no unique permitted', 'rollout could not', 'referenced parent')),
        ('source_or_content_changed', ('changed', 'restart')),
        ('repeated_or_invalid_continuation', ('repeated', 'mismatch', 'offset', 'cursor', 'prefix')),
        ('safety_bound_reached', ('bound reached', 'exceeds', 'safety bound')),
        ('metadata_unresolved', ('thread not loaded', 'metadata', 'candidate')),
    )
    return next((name for name, terms in categories if any(term in text for term in terms)), 'read_or_validation_error')


def _safe_failure(name: str, cwd: str | None, archived: bool, stage: str, exc: Exception) -> dict[str, Any]:
    # Never echo an App Server error, history text, credential, or guessed UUID.
    return {'display_name': name, 'cwd': cwd, 'archived': archived,
            'stage': stage, 'error_type': type(exc).__name__, 'failure_reason': _error_reason(exc),
            'detail': 'This candidate/session was not fully searched. Other independent sessions may continue.',
            'history_complete': False}


async def _validate(client: Any, row: dict[str, Any], archived: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    """Same canonical identity rules as the exact-directory resolver."""
    tid = resolver._text(row.get('id'), 'candidate thread ID', max_len=160)
    cwd = resolver._text(row.get('cwd'), 'working directory')
    identity = resolver._identity(await resolver._metadata(client, tid), expected_id=tid, cwd=cwd)
    for a, b in (('name', 'name'), ('createdAt', 'created_at'), ('sessionId', 'session_id'),
                 ('parentThreadId', 'parent_thread_id'), ('forkedFromId', 'forked_from_id'), ('historyMode', 'history_mode')):
        if row.get(a) is not None and row[a] != identity[b]:
            raise DiscoveryError('Candidate metadata changed during validation. No reference issued.')
    if row.get('path') and resolver.canonical_path(row['path']) != identity['rollout_path_key']:
        raise DiscoveryError('Candidate storage identity changed during validation.')
    entry = {'identity': identity, 'home': resolver._home_key(), 'archived': archived,
             'include_background': False, 'diagnostics': False}
    if identity['background']:
        return entry, {}
    ref = resolver._SESSIONS.put(entry)
    return entry, resolver._descriptor(ref, entry)


def _new_inventory() -> dict[str, Any]:
    return {'phase': 0, 'cursor': None, 'loaded': False, 'pending': [], 'seen_ids': {},
            'seen_cursors': [], 'rows': 0, 'pages': 0, 'done': False, 'blocked': False}


async def _inventory_step(state: dict[str, Any], deadline: float, events: list[dict[str, Any]]) -> None:
    inv = state['inventory']
    processed = 0
    try:
        async with bridge.CodexAppServerClient() as client:
            while not inv['done'] and processed < CATALOG_BATCH and time.monotonic() < deadline:
                if not inv['pending']:
                    if inv['loaded'] and inv['cursor'] is None:
                        if state['include_archived'] and inv['phase'] == 0:
                            inv['phase'] = 1
                            inv['loaded'] = False
                        else:
                            inv['done'] = True
                            break
                    if inv['pages'] >= MAX_INVENTORY_PAGES or inv['rows'] >= MAX_ROWS:
                        raise DiscoveryError('Inventory safety bound reached.')
                    data, cursor = await resolver._list(client, cwd=None, archived=bool(inv['phase']),
                                                       background=False, cursor=inv['cursor'], limit=25)
                    inv['pages'] += 1
                    if cursor is not None:
                        marker = [inv['phase'], cursor]
                        if marker in inv['seen_cursors']:
                            raise DiscoveryError('Inventory cursor repeated.')
                        inv['seen_cursors'].append(marker)
                    inv['pending'] = [resolver._candidate_row(t) for t in data]
                    inv['cursor'], inv['loaded'] = cursor, True
                    if not data:
                        continue
                row = inv['pending'][0]
                if inv['rows'] >= MAX_ROWS:
                    raise DiscoveryError('Inventory row bound reached.')
                if len(state['sessions']) >= MAX_SESSIONS:
                    raise DiscoveryError('Verified session bound reached; refine roots.')
                inv['pending'].pop(0)
                inv['rows'] += 1
                processed += 1
                name = resolver._label(row.get('name'))
                cwd = row.get('cwd')
                archived = bool(inv['phase'])
                try:
                    key = resolver.canonical_path(cwd)
                    if state['root_keys'] is not None and key not in state['root_keys']:
                        state['counts']['outside_selected_roots'] += 1
                        continue
                    tid = resolver._text(row.get('id'), 'candidate ID', max_len=160)
                    if tid in inv['seen_ids']:
                        previous = inv['seen_ids'][tid]
                        if previous != [inv['phase'], key]:
                            raise DiscoveryError('Identity appeared in conflicting roots or archive states.')
                        state['counts']['duplicate_inventory_rows'] += 1
                        continue
                    inv['seen_ids'][tid] = [inv['phase'], key]
                    state['counts']['sessions_considered'] += 1
                    entry, descriptor = await _validate(client, row, archived)
                    if not descriptor:
                        state['counts']['background_or_unclassified_excluded'] += 1
                        events.append({'display_name': name, 'cwd': cwd, 'archived': archived,
                                       'stage': 'excluded_background_or_unclassified', 'history_complete': False})
                        continue
                    state['sessions'].append({'entry': entry, 'descriptor': descriptor})
                    state['counts']['sessions_verified'] += 1
                except (bridge.CodexBridgeError, ValueError, TypeError, OSError, asyncio.TimeoutError) as exc:
                    state['counts']['candidate_failures'] += 1
                    events.append(_safe_failure(name, cwd if isinstance(cwd, str) else None, archived, 'metadata_validation', exc))
    except (bridge.CodexBridgeError, ValueError, OSError, asyncio.TimeoutError) as exc:
        inv['done'], inv['blocked'] = True, True
        state['counts']['inventory_failures'] += 1
        events.append(_safe_failure('Inventory', None, bool(inv['phase']), 'state_inventory', exc))
    # When the last candidate exactly filled this call, recognize EOF immediately.
    if not inv['pending'] and inv['loaded'] and inv['cursor'] is None and (not state['include_archived'] or inv['phase'] == 1):
        inv['done'] = True


def _current_session(descriptor: dict[str, Any]) -> dict[str, Any]:
    return {'descriptor': copy.deepcopy(descriptor), 'token': None, 'source': None,
            'pages': 0, 'restarts': 0, 'chars': 0, 'fields': 0,
            'occurrences': 0, 'excerpts': [], 'tail': None, 'native_unsupported': False,
            'unsupported_native_item_types': [], 'coverage': None, 'seen_positions': []}


def _literal_matches(pattern: re.Pattern[str], text: str):
    start = 0
    while True:
        match = pattern.search(text, start)
        if match is None:
            return
        yield match
        start = match.start() + 1


def _scan_page(current: dict[str, Any], result: dict[str, Any], pattern: re.Pattern[str], query_len: int) -> None:
    """Stream literal matches across fragment boundaries, never across messages.

    Only direct user/assistant message text is searched. Checkpoint snapshots,
    plans, collaboration payloads, errors, commands and media are not searched.
    Offsets are character offsets in the reader's projected text field.
    """
    turns = result.get('turns')
    if not isinstance(turns, list):
        raise DiscoveryError('Missing history turns; no successful search is claimed.')
    for turn in turns:
        if not isinstance(turn, dict) or not isinstance(turn.get('items'), list):
            raise DiscoveryError('Malformed visible-history page.')
        for item in turn['items']:
            if not isinstance(item, dict) or not isinstance(item.get('fragments'), list):
                raise DiscoveryError('Malformed history item.')
            unsupported_note = any(
                f.get('field') == 'note' and isinstance(f.get('text'), str) and f['text'].startswith('Unsupported item type:')
                for f in item['fragments'] if isinstance(f, dict))
            item_type = item.get('type')
            if result.get('history_source') != 'local_rollout_fallback' and (item_type not in bridge.NATIVE_THREAD_ITEM_TYPES or unsupported_note):
                current['native_unsupported'] = True
                label = item_type if isinstance(item_type, str) and item_type else 'missing_type'
                if label not in current['unsupported_native_item_types']:
                    current['unsupported_native_item_types'].append(label)
            elif item_type == 'unsupportedRecord':
                current['native_unsupported'] = True
                if 'unsupportedRecord' not in current['unsupported_native_item_types']:
                    current['unsupported_native_item_types'].append('unsupportedRecord')
            if item.get('type') not in {'userMessage', 'agentMessage'} or item.get('role') not in {'user', 'assistant'}:
                continue
            if item.get('phase') in {'analysis', 'reasoning'}:
                continue
            for fragment in item['fragments']:
                if not isinstance(fragment, dict):
                    raise DiscoveryError('Malformed field fragment.')
                if fragment.get('field') != 'text' or fragment.get('encoding') != 'text':
                    continue
                text = fragment.get('text')
                start, end, total = (fragment.get(k) for k in ('offset', 'end_offset', 'total_chars'))
                if not isinstance(text, str) or any(type(x) is not int for x in (start, end, total)) or not 0 <= start <= end <= total or len(text) != end - start or type(fragment.get('complete')) is not bool or fragment['complete'] != (end == total):
                    raise DiscoveryError('Visible text offsets are inconsistent.')
                key = [turn.get('id'), item.get('id'), item.get('item_index'), 'text']
                previous = current['tail']
                if previous is not None:
                    if previous['key'] != key or previous['end'] != start or previous['total'] != total:
                        raise DiscoveryError('Visible message continuation has a gap or changed identity.')
                    combined = previous['text'] + text
                    base = start - len(previous['text'])
                else:
                    if start != 0:
                        raise DiscoveryError('Visible message begins at a nonzero offset without its prefix.')
                    combined, base = text, 0
                    current['fields'] += 1
                current['chars'] += len(text)
                for match in _literal_matches(pattern, combined):
                    absolute_end = base + match.end()
                    if previous is not None and absolute_end <= start:
                        continue  # overlapping suffix is context, not another occurrence
                    current['occurrences'] += 1
                    if len(current['excerpts']) < EXCERPTS_PER_SESSION:
                        a, z = max(0, match.start() - EXCERPT_CONTEXT), min(len(combined), match.end() + EXCERPT_CONTEXT)
                        current['excerpts'].append({'role': item['role'], 'excerpt': combined[a:z],
                            'turn_reference': turn.get('id'), 'item_reference': item.get('id'),
                            'field': 'text', 'match_offset': base + match.start(),
                            'context_may_be_partial': a > 0 or z < len(combined) or not fragment['complete']})
                if fragment['complete']:
                    current['tail'] = None
                else:
                    keep = query_len - 1 + EXCERPT_CONTEXT
                    current['tail'] = {'key': key, 'text': combined[-keep:], 'end': end, 'total': total}


def _searchable_completion(current: dict[str, Any], result: dict[str, Any]) -> bool:
    report = result.get('coverage_report')
    current['coverage'] = report
    if current['tail'] is not None:
        raise DiscoveryError('History ended inside a visible message.')
    if current['native_unsupported']:
        return False
    if result.get('history_source') == 'local_rollout_fallback':
        return bool(isinstance(report, dict) and report.get('projection_coverage_complete') is True
                    and report.get('record_support_complete') is True and not report.get('projection_gaps')
                    and report.get('unknown_records') == 0 and not report.get('rollback_records'))
    return True  # native visible-message projection only; no raw-record audit claimed


def _finish_session(state: dict[str, Any], current: dict[str, Any], result: dict[str, Any] | None,
                    matches: list[dict[str, Any]], events: list[dict[str, Any]], *, error: Exception | None = None) -> None:
    s = current['descriptor']
    complete = False if error else _searchable_completion(current, result)
    if complete:
        state['counts']['sessions_searched_successfully'] += 1
    else:
        state['counts']['sessions_failed_or_partial'] += 1
    state['counts']['history_pages_accepted'] += current['pages']
    state['counts']['visible_chars_searched'] += current['chars']
    state['counts']['message_fields_searched'] += current['fields']
    state['counts']['source_restarts'] += current['restarts']
    state['counts']['occurrences_observed'] += current['occurrences']
    if current['occurrences']:
        state['counts']['matched_sessions'] += 1
        match = {**s, 'history_source': current['source'], 'message_occurrences_observed': current['occurrences'],
                 'sample_excerpts': current['excerpts'], 'excerpts_limited': current['occurrences'] > len(current['excerpts']),
                 'session_search_complete': complete, 'history_pages_accepted': current['pages'],
                 'unsupported_native_item_types': sorted(current['unsupported_native_item_types']),
                 'lead_only': True}
        matches.append(match)
        key = resolver.canonical_path(s['cwd'])
        group = state['directory_counts'].setdefault(key, {'cwd': s['cwd'], 'matching_sessions': 0})
        group['matching_sessions'] += 1
        if state['control_id'] == state['sessions'][state['index']]['entry']['identity']['thread_id']:
            state['control_matched'] = True
    events.append({**{k: s[k] for k in ('display_name', 'cwd', 'archived')},
        'stage': 'session_finished' if error is None else 'history_read_failed',
        'session_search_complete': complete, 'history_source': current['source'],
        'history_pages_accepted': current['pages'], 'visible_chars_searched': current['chars'],
        'unknown_records': (current['coverage'] or {}).get('unknown_records'),
        'projection_gaps': (current['coverage'] or {}).get('projection_gaps'),
        'unsupported_native_item_types': sorted(current['unsupported_native_item_types']),
        'error_type': type(error).__name__ if error else None, 'failure_reason': _error_reason(error) if error else None})
    state['index'] += 1
    state['current'] = None


async def search_visible_history(*, query: str, roots: list[str] | None = None,
                                 include_archived: bool = True, case_sensitive: bool = False,
                                 page_token: str | None = None, max_history_pages: int = 4,
                                 positive_control_session_ref: str | None = None) -> dict[str, Any]:
    """Search every verified session's allowed text, not native-index snippets.

    A bounded call can return zero hits AND a continuation. Only a terminal,
    successful nonempty inventory scan permits a scoped negative conclusion.
    Search hits are directory/session leads; they are not full reconstructions.
    """
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS or any(ord(c) < 32 for c in query):
        raise DiscoveryError('query must be a nonblank literal string of at most 256 characters, without control characters.')
    resolver._bool(include_archived, 'include_archived')
    resolver._bool(case_sensitive, 'case_sensitive')
    if type(max_history_pages) is not int or not 1 <= max_history_pages <= 16:
        raise DiscoveryError('max_history_pages must be 1..16.')
    selected_roots = _roots(roots)
    keys = sorted(resolver.canonical_path(p) for p in selected_roots) if selected_roots else None
    options = [query, keys, include_archived, case_sensitive, positive_control_session_ref]
    continuation_generation = None
    if page_token is not None:
        state, continuation_generation = _SEARCH.begin(page_token)
        if state.get('bridge_version') != bridge.BRIDGE_VERSION or state.get('lifecycle_version') != 2:
            raise DiscoveryError('Search continuation belongs to another bridge version. Restart from page 1 and do not combine abandoned chains.')
        if state['home'] != resolver._home_key() or state['options'] != options:
            raise DiscoveryError('Search continuation belongs to another query, root selection, flags, control or Codex home.')
    else:
        control_id = None
        if positive_control_session_ref is not None:
            control = resolver._SESSIONS.get(positive_control_session_ref)
            if control['home'] != resolver._home_key() or control['identity']['background'] or (not include_archived and control['archived']) or (keys is not None and control['identity']['cwd_key'] not in keys):
                raise DiscoveryError('Positive control is not in the selected visible-history scope.')
            control_id = control['identity']['thread_id']
        state = {'bridge_version': bridge.BRIDGE_VERSION, 'lifecycle_version': 2,
                 'home': resolver._home_key(), 'options': options, 'roots': selected_roots, 'root_keys': keys,
                 'include_archived': include_archived, 'inventory': _new_inventory(), 'sessions': [], 'index': 0,
                 'current': None, 'directory_counts': {}, 'control_id': control_id, 'control_matched': False,
                 'counts': {name: 0 for name in ('sessions_considered', 'sessions_verified', 'candidate_failures',
                     'inventory_failures', 'outside_selected_roots', 'duplicate_inventory_rows',
                     'background_or_unclassified_excluded', 'sessions_searched_successfully', 'sessions_failed_or_partial',
                     'matched_sessions', 'history_pages_accepted', 'visible_chars_searched', 'message_fields_searched',
                     'source_restarts', 'occurrences_observed')}}
    events: list[dict[str, Any]] = []
    matches: list[dict[str, Any]] = []
    deadline = time.monotonic() + CALL_SECONDS
    if not state['inventory']['done']:
        await _inventory_step(state, deadline, events)
    used_pages = 0
    pattern = re.compile(re.escape(query), 0 if case_sensitive else re.IGNORECASE)
    while state['inventory']['done'] and state['index'] < len(state['sessions']) and used_pages < max_history_pages and time.monotonic() < deadline:
        selected = state['sessions'][state['index']]
        current = state['current']
        if current is None:
            # Reissue a short ref from the verified snapshot, then the reader
            # revalidates it. No title-based automatic replacement is possible.
            ref = resolver._SESSIONS.put(selected['entry'])
            selected['descriptor'] = resolver._descriptor(ref, selected['entry'])
            current = _current_session(selected['descriptor'])
            state['current'] = current
        used_pages += 1
        try:
            result = await asyncio.wait_for(resolver.read_session_ref(session_ref=current['descriptor']['session_ref'],
                page_token=current['token'], max_turns=SEARCH_TURNS, max_chars=SEARCH_CHARS,
                include_tool_output=False, include_diffs=False), timeout=READ_SECONDS)
            if not isinstance(result, dict) or result.get('ok') is False or result.get('is_error'):
                raise DiscoveryError('Verified history read failed; no automatic replacement selected.')
            more = result.get('has_more_content')
            token = result.get('next_page_token')
            if type(more) is not bool or more != bool(token) or (token is not None and not isinstance(token, str)):
                raise DiscoveryError('History continuation flag/token mismatch.')
            source = result.get('history_source')
            if not isinstance(source, str) or not source:
                raise DiscoveryError('History source missing.')
            changed = current['source'] is not None and current['source'] != source
            if changed or result.get('history_restart_required') is True:
                if source != 'local_rollout_fallback' or result.get('history_restart_required') is not True:
                    raise DiscoveryError('History source changed without a confirmed recovery restart.')
                restarts = current['restarts'] + 1
                if restarts > 2:
                    raise DiscoveryError('Repeated history source restarts; search left partial.')
                current = _current_session(current['descriptor'])
                current['restarts'] = restarts
                state['current'] = current
            if token is not None:
                # The verified reader wraps its cursor each time; compare the
                # underlying position too, not only the fresh outer handle.
                position = resolver._PAGES.get(token)['reader_token'] if token.startswith(resolver.CONTINUATION_PREFIX) else token
                mark = hashlib.sha256(position.encode('utf-8')).hexdigest()
                if mark in current['seen_positions'] or len(current['seen_positions']) >= 4096:
                    raise DiscoveryError('History position repeated or per-session page bound reached.')
                current['seen_positions'].append(mark)
            current['source'] = source
            _scan_page(current, result, pattern, len(query))
            current['pages'] += 1
            current['token'] = token
            if not more:
                _finish_session(state, current, result, matches, events)
        except (bridge.CodexBridgeError, OSError, ValueError, TypeError, asyncio.TimeoutError) as exc:
            # No raw exception strings, guessed IDs or broad retries. A future
            # explicit fresh search can revisit this candidate.
            _finish_session(state, current, None, matches, events, error=exc)
    terminal = state['inventory']['done'] and state['index'] == len(state['sessions'])
    counts = state['counts']
    inventory_complete = state['inventory']['done'] and not state['inventory']['blocked']
    control_ok = state['control_id'] is None or state['control_matched']
    complete = bool(terminal and inventory_complete and not counts['candidate_failures'] and
                    not counts['sessions_failed_or_partial'] and counts['sessions_verified'] > 0 and control_ok)
    if page_token is None:
        token = None if terminal else _put(_SEARCH, state)
        lifecycle = ({'active': False, 'idle_age_seconds': 0.0, 'idle_ttl_seconds': SEARCH_IDLE_SECONDS,
                      'absolute_age_seconds': 0.0, 'absolute_ttl_seconds': SEARCH_ABSOLUTE_SECONDS}
                     if terminal else _SEARCH.diagnostics(token))
    else:
        token = None if terminal else page_token
        _check_state_size(state)
        lifecycle = _SEARCH.commit(page_token, continuation_generation, state, keep=not terminal)
    active = state['current']
    progress = {**counts, 'inventory_rows_examined': state['inventory']['rows'],
                'sessions_remaining': len(state['sessions']) - state['index'],
                'history_pages_accepted': counts['history_pages_accepted'] + (active['pages'] if active else 0),
                'visible_chars_searched': counts['visible_chars_searched'] + (active['chars'] if active else 0),
                'message_fields_searched': counts['message_fields_searched'] + (active['fields'] if active else 0)}
    return {'ok': True, 'bridge_version': bridge.BRIDGE_VERSION,
        'status': 'search_finished' if complete else 'search_incomplete' if terminal else 'search_in_progress',
        'search_backend': 'bridge_verified_message_scan', 'native_search_index_used': False,
        'query': query, 'case_sensitive': case_sensitive, 'roots': selected_roots,
        'include_archived': include_archived, 'include_background': False, 'scan_legacy_logs': False,
        'inventory_source': 'codex_state_database', 'inventory_complete': inventory_complete,
        'search_traversal_finished': terminal, 'search_complete': complete,
        'negative_result_valid': complete and counts['matched_sessions'] == 0,
        'has_more_search': not terminal, 'next_page_token': token,
        'search_handle_lifecycle': lifecycle,
        'progress': progress, 'matching_sessions': matches, 'session_events': events,
        'directory_leads': list(state['directory_counts'].values()),
        'current_session': {'display_name': active['descriptor']['display_name'], 'cwd': active['descriptor']['cwd'],
                            'pages_read': active['pages']} if active else None,
        'positive_control': {'required': state['control_id'] is not None, 'matched': state['control_matched'] if state['control_id'] else None},
        'coverage_scope': 'Direct user/assistant message text in the observed state-database inventory; not all possible project evidence.',
        'privacy_note': 'Existing privacy filters apply. No tool bodies, diffs, private reasoning, system/developer context, checkpoint snapshots or linked-agent messages are searched. Native textual attachment markers may be present. Visible text can itself contain secrets; snippets leave this computer when called from ChatGPT.',
        'instruction': 'Follow next_page_token while has_more_search=true with the same query/roots/flags/control. Copy the opaque token unchanged; one active search keeps the same token while valid continuations refresh its idle lifetime. Accumulate matching_sessions across calls; directory_leads and progress are cumulative. Zero hits on an intermediate/incomplete/empty-inventory search is not a negative finding. A matched session may itself be partial; use its session_search_complete. Confirm one or more roots with the user before creating a project scope. Search reads text locally but is NOT full-session content delivery to the chat or conceptual-scope proof. Keep the process running; idle/absolute expiry or restart requires a new page-1 search whose pages must not be combined with the abandoned chain.'}


async def create_project_scope(*, project_name: str, roots: list[str], confirmed: bool = False,
                               include_archived: bool = True) -> dict[str, Any]:
    """Store only explicitly confirmed exact roots; no search or recursive walk."""
    name = resolver._text(project_name, 'project_name', max_len=240)
    selected = _roots(roots, required=True)
    resolver._bool(confirmed, 'confirmed'); resolver._bool(include_archived, 'include_archived')
    if not confirmed:
        return {'ok': True, 'status': 'confirmation_required', 'project_name': name, 'roots': selected,
                'project_scope_ref': None, 'session_contents_read': False,
                'instruction': 'Ask the user to confirm this exact root set and archive setting. Do not treat inferred directory leads as authorized scope.'}
    ref = _put(_SCOPES, {'home': resolver._home_key(), 'project_name': name, 'roots': selected,
                        'include_archived': include_archived})
    return {'ok': True, 'bridge_version': bridge.BRIDGE_VERSION, 'status': 'scope_created',
            'project_scope_ref': ref, 'project_name': name, 'roots': selected,
            'include_archived': include_archived, 'include_background': False, 'scan_legacy_logs': False,
            'inventory_complete': False, 'session_contents_read': False,
            'scope_note': 'Confirmed exact recorded directories, not a recursive filesystem scan or proof of complete conceptual membership. A single root can contain many independent sessions.',
            'instruction': 'Catalog this scope using codex_catalog_project_scope; follow every catalog page, then read verified session_ref values independently. Recreate the scope after process restart/expiry.'}


async def catalog_project_scope(*, project_scope_ref: str, limit: int = 20,
                                page_token: str | None = None) -> dict[str, Any]:
    scope = _SCOPES.get(project_scope_ref)
    if scope['home'] != resolver._home_key():
        raise DiscoveryError('Codex home changed. Recreate the scope; no inventory read attempted.')
    if type(limit) is not int or not 1 <= limit <= 50:
        raise DiscoveryError('limit must be 1..50.')
    if page_token is None:
        state = {'scope_ref': project_scope_ref, 'home': scope['home'], 'root_index': 0,
                 'root_token': None, 'seen': {}, 'root_reports': [
                     {'cwd': path, 'status': 'pending', 'verified_sessions': 0, 'rejected_candidates': 0,
                      'excluded_candidates': 0, 'inventory_rows_accounted_for': 0, 'inventory_complete': False}
                     for path in scope['roots']]}
    else:
        state = _SCOPE_PAGES.get(page_token)
        if state['scope_ref'] != project_scope_ref or state['home'] != scope['home']:
            raise DiscoveryError('Scope catalog token belongs to a different scope or Codex home.')
    sessions, rejected, excluded = [], [], []
    # One existing resolver call per scope call: its own time/candidate bounds
    # remain effective, and failures do not abandon another root's catalog.
    index = state['root_index']
    if index < len(scope['roots']):
        root = scope['roots'][index]
        report = state['root_reports'][index]
        try:
            page = await resolver.resolve_project_sessions(project=root, include_archived=scope['include_archived'],
                include_background=False, limit=limit, page_token=state['root_token'], diagnostics=False)
            if page.get('status') != 'resolved' or type(page.get('has_more_candidates')) is not bool or page['has_more_candidates'] != bool(page.get('next_page_token')):
                raise DiscoveryError('Root catalog did not return a verified pagination result.')
            for s in page['sessions']:
                entry = resolver._SESSIONS.get(s['session_ref'])
                tid = entry['identity']['thread_id']
                if resolver.canonical_path(s['cwd']) != resolver.canonical_path(root):
                    raise DiscoveryError('A scope candidate escaped its selected root.')
                if tid in state['seen']:
                    prior = state['seen'][tid]
                    # Same title or session-tree ID is NEVER a deduplication key.
                    problem = {'display_name': s['display_name'], 'cwd': root, 'selectable': False,
                               'reason': 'duplicate_canonical_thread' if prior == entry['identity']['cwd_key'] else 'conflicting_canonical_thread'}
                    (excluded if prior == entry['identity']['cwd_key'] else rejected).append(problem)
                    continue
                if len(state['seen']) >= MAX_SESSIONS:
                    raise DiscoveryError('Scope exceeds the bounded verified-session catalog.')
                state['seen'][tid] = entry['identity']['cwd_key']
                sessions.append({**s, 'scope_root': root, 'project_scope_ref': project_scope_ref})
            rejected += [{**r, 'cwd': root} for r in page['rejected_candidates']]
            excluded += [{**r, 'cwd': root} for r in page['excluded_candidates']]
            report['verified_sessions'] += len(sessions)
            report['rejected_candidates'] += len(rejected)
            report['excluded_candidates'] += len(excluded)
            report['inventory_rows_accounted_for'] = page['inventory_rows_accounted_for']
            report['inventory_complete'] = page['inventory_complete']
            report['status'] = 'complete' if page['inventory_complete'] else 'in_progress'
            state['root_token'] = page['next_page_token']
            if not state['root_token']:
                state['root_index'] += 1
        except (bridge.CodexBridgeError, OSError, ValueError, TypeError, asyncio.TimeoutError) as exc:
            report['status'], report['inventory_complete'] = 'failed', False
            report['error_type'] = type(exc).__name__
            rejected.append(_safe_failure('Root inventory', root, False, 'root_catalog', exc))
            report['rejected_candidates'] += 1
            state['root_index'] += 1
            state['root_token'] = None
    terminal = state['root_index'] >= len(scope['roots'])
    inventory_complete = terminal and all(r['inventory_complete'] for r in state['root_reports'])
    selection_complete = inventory_complete and not any(r['rejected_candidates'] for r in state['root_reports'])
    token = None if terminal else _put(_SCOPE_PAGES, state)
    return {'ok': True, 'bridge_version': bridge.BRIDGE_VERSION, 'status': 'scope_catalog',
            'project_name': scope['project_name'], 'project_scope_ref': project_scope_ref,
            'roots': scope['roots'], 'include_archived': scope['include_archived'],
            'include_background': False, 'scan_legacy_logs': False, 'inventory_source': 'codex_state_database',
            'catalog_traversal_complete': terminal, 'inventory_complete': inventory_complete,
            'selection_complete': selection_complete, 'has_more_candidates': not terminal,
            'next_page_token': token, 'sessions': sessions, 'rejected_candidates': rejected,
            'excluded_candidates': excluded, 'root_reports': state['root_reports'],
            'verified_sessions_total': len(state['seen']), 'session_contents_read': False,
            'instruction': 'Accumulate sessions across every catalog page. Preserve all distinct verified sessions even in the same directory or with identical titles. Finish each session_ref independently with codex_read_session_ref. An incomplete/failed root remains an explicit gap; continue others. This scope covers only the user-confirmed exact roots, not every conceptual association or linked agent.'}
