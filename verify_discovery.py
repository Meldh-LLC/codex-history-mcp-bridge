"""Local metadata/search checks. Never print excerpts, tokens, or raw Codex IDs.

Search reads allowed history on the local machine. It does not call a language
model or write to history. All continuations stay in one process.
Catalog mode is metadata-only; --root can be repeated for multiple exact roots.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

import codex_bridge as bridge
import project_discovery as discovery
import session_resolver as resolver


def emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=True), flush=True)


async def search(args: argparse.Namespace) -> int:
    token = None
    found_roots: set[str] = set()
    found_names: set[str] = set()
    emit({'bridge_version': bridge.BRIDGE_VERSION, 'mode': 'visible_history_search',
          'include_archived': not args.active_only, 'roots': args.root,
          'max_calls': args.max_calls, 'transcript_or_tokens_printed': False})
    for n in range(args.max_calls):
        page = await discovery.search_visible_history(query=args.query, roots=args.root,
            include_archived=not args.active_only, case_sensitive=args.case_sensitive,
            max_history_pages=args.max_history_pages, page_token=token)
        matches = []
        for hit in page['matching_sessions']:
            found_roots.add(resolver.canonical_path(hit['cwd']))
            found_names.add(hit['display_name'])
            matches.append({k: hit[k] for k in ('display_name', 'cwd', 'archived',
                'session_search_complete', 'message_occurrences_observed', 'history_source')})
        emit({'call': n + 1, 'status': page['status'], 'inventory_complete': page['inventory_complete'],
              'search_complete': page['search_complete'], 'progress': page['progress'],
              'matching_sessions': matches, 'session_events': page['session_events'],
              'current_session': page['current_session'], 'has_more_search': page['has_more_search'],
              'next_token_present': bool(page['next_page_token'])})
        token = page['next_page_token']
        if not page['has_more_search']:
            expected = all(resolver.canonical_path(root) in found_roots for root in args.expect_root)
            expected = expected and all(name in found_names for name in args.expect_session_name)
            label = 'PASS_SEARCH_FINISHED' if page['search_complete'] else 'PARTIAL_SEARCH'
            if not expected:
                label = 'FAIL_EXPECTED_MATCH_NOT_FOUND'
            emit({'result': label, 'calls': n + 1, 'search_complete': page['search_complete'],
                  'inventory_complete': page['inventory_complete'], 'negative_result_valid': page['negative_result_valid'] and expected,
                  'expected_matches_found': expected, 'progress': page['progress'],
                  'directory_leads': page['directory_leads'],
                  'note': 'Matches are literal-text directory/session leads, not proof of conceptual project completeness. No history transcript was printed. Confirm scope before a report.'})
            return 0 if label.startswith('PASS') else 2
    emit({'result': 'PARTIAL_MAX_CALLS', 'calls': args.max_calls, 'continuation_remaining': bool(token),
          'note': 'The verifier stopped at its configured bound, not EOF. Its process-local bookmark will expire when this process exits. A later run starts a new search.'})
    return 2


async def catalog(args: argparse.Namespace) -> int:
    created = await discovery.create_project_scope(project_name=args.name, roots=args.root,
        confirmed=True, include_archived=not args.active_only)
    scope_ref = created['project_scope_ref']
    emit({'bridge_version': bridge.BRIDGE_VERSION, 'mode': 'confirmed_scope_catalog',
          'roots': created['roots'], 'include_archived': created['include_archived'],
          'session_contents_read': False})
    token = None
    for n in range(args.max_calls):
        page = await discovery.catalog_project_scope(project_scope_ref=scope_ref, page_token=token)
        emit({'catalog_page': n + 1, 'sessions': [
            {k: s[k] for k in ('display_name', 'cwd', 'archived', 'validation_status', 'history_mode')}
            for s in page['sessions']], 'root_reports': page['root_reports'],
            'rejected_candidates': page['rejected_candidates'], 'excluded_candidates': page['excluded_candidates'],
            'has_more_candidates': page['has_more_candidates']})
        token = page['next_page_token']
        if not page['has_more_candidates']:
            ok = page['selection_complete'] and page['verified_sessions_total'] > 0
            emit({'result': 'PASS_SCOPE_CATALOG' if ok else 'PARTIAL_OR_EMPTY_SCOPE_CATALOG',
                  'catalog_traversal_complete': page['catalog_traversal_complete'],
                  'inventory_complete': page['inventory_complete'], 'selection_complete': page['selection_complete'],
                  'verified_sessions': page['verified_sessions_total'],
                  'note': 'Metadata-only test for the explicitly provided roots. No transcript recovery or conceptual membership guarantee.'})
            return 0 if ok else 2
    emit({'result': 'PARTIAL_MAX_CALLS', 'note': 'Scope catalog stopped at its configured bound.'})
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='mode', required=True)
    s = commands.add_parser('search', help='Read and search visible message text; print no excerpts.')
    s.add_argument('--query', required=True, help='Literal substring; case-insensitive by default.')
    s.add_argument('--root', action='append', help='Optional exact recorded cwd; repeat for multiple roots. Omit for global inventory.')
    s.add_argument('--expect-root', action='append', default=[], help='Require at least one message match from this exact cwd.')
    s.add_argument('--expect-session-name', action='append', default=[], help='Require a message match in a session with this exact display name.')
    s.add_argument('--case-sensitive', action='store_true')
    s.add_argument('--max-history-pages', type=int, default=4)
    c = commands.add_parser('catalog', help='Validate all sessions across explicit roots, metadata only.')
    c.add_argument('--name', required=True, help='User label, not a lookup heuristic.')
    c.add_argument('--root', action='append', required=True, help='Confirmed exact cwd; repeat for each selected directory.')
    for p in (s, c):
        p.add_argument('--active-only', action='store_true', help='Exclude archives; default includes active and archived.')
        p.add_argument('--max-calls', type=int, default=1000)
    args = parser.parse_args(argv)
    if not 1 <= args.max_calls <= 2000:
        parser.error('--max-calls must be 1..2000')
    if args.mode == 'search' and not 1 <= args.max_history_pages <= 16:
        parser.error('--max-history-pages must be 1..16')
    try:
        return asyncio.run(search(args) if args.mode == 'search' else catalog(args))
    except (bridge.CodexBridgeError, OSError, ValueError, TypeError) as exc:
        emit({'result': 'FAIL', 'error_type': type(exc).__name__,
              'message': 'Discovery did not finish. No transcript, token, or raw exception payload printed. Preserve this output; do not treat it as no matches.'})
        return 1
    except KeyboardInterrupt:
        emit({'result': 'INTERRUPTED_PARTIAL', 'message': 'Traversal interrupted; no completion claim.'})
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
