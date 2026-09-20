"""Cross-session search and confirmed-scope regressions. Synthetic data only."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import codex_bridge as b
import project_discovery as d
import rollout_fallback as rf
import session_resolver as r
import verify_discovery as v
from test_more_cases import import_server_with_sdk_stub
from test_resolution import Client, thread
from test_rollout_fallback import message, record, TID

A, B = 'E:/projects/primary', 'E:/projects/secondary'


def fragment(text, start=0, total=None, field='text'):
    end = start + len(text)
    total = end if total is None else total
    return {'field': field, 'encoding': 'text', 'offset': start, 'end_offset': end,
            'total_chars': total, 'complete': total == end, 'text': text}


def page(text='Target', token=None, *, start=0, total=None, kind='agentMessage', role='assistant',
         source='native', restart=False, gaps=False, field='text', iid='message', **extras):
    item = {'id': iid, 'type': kind, 'role': role, 'item_index': 0,
            'fragments': [fragment(text, start, total, field)], **extras}
    out = {'turns': [{'id': 'turn', 'items': [item]}], 'history_source': source,
           'has_more_content': bool(token), 'next_page_token': token,
           'history_restart_required': restart}
    if source == 'local_rollout_fallback':
        out['coverage_report'] = {'projection_coverage_complete': not token and not gaps,
            'record_support_complete': not token and not gaps, 'unknown_records': int(gaps),
            'projection_gaps': {'unknown': 1} if gaps else {}, 'rollback_records': 0}
    return out


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for cache in (r._SESSIONS, r._PAGES, r._CATALOGS, d._SEARCH, d._SCOPES, d._SCOPE_PAGES):
            cache.clear()
        self.client = Client([thread('one', 'First', A)])
        self.stub = patch.object(b, 'CodexAppServerClient', self.client)
        self.stub.start(); self.addCleanup(self.stub.stop)
        self.reader = AsyncMock(return_value=page())
        self.patch_read = patch.object(r, 'read_session_ref', self.reader)
        self.patch_read.start(); self.addCleanup(self.patch_read.stop)

    async def all_search(self, **kwargs):
        token = None; results = []; matches = []
        for _ in range(200):
            result = await d.search_visible_history(query=kwargs.pop('query', 'Target') if not results else query,
                page_token=token, **kwargs)
            query = result['query']
            results.append(result); matches.extend(result['matching_sessions'])
            token = result['next_page_token']
            if not token:
                return result, matches, results
        self.fail('search did not terminate')

    async def scope(self, roots=None, **kwargs):
        x = await d.create_project_scope(project_name='Example', roots=roots or [A], confirmed=True, **kwargs)
        return x['project_scope_ref']

    async def all_catalog(self, ref, limit=20):
        token = None; sessions = []; pages = []
        for _ in range(100):
            page_ = await d.catalog_project_scope(project_scope_ref=ref, page_token=token, limit=limit)
            sessions += page_['sessions']; pages.append(page_); token = page_['next_page_token']
            if token is None:
                return page_, sessions, pages
        self.fail('catalog did not terminate')

    def lifecycle_cache(self, clock, *, idle=10, absolute=1000):
        original = d._SEARCH
        d._SEARCH = d.SearchHandleCache('cbh1_', limit=16, idle_ttl=idle, absolute_ttl=absolute)
        self.addCleanup(setattr, d, '_SEARCH', original)
        monotonic = patch.object(d.time, 'monotonic', side_effect=lambda: clock[0])
        monotonic.start(); self.addCleanup(monotonic.stop)

    async def test_native_index_never_used_even_if_known_positive(self):
        with patch.object(b, 'search_history', side_effect=AssertionError('native index used')):
            result, hits, _ = await self.all_search()
        self.assertTrue(result['search_complete']); self.assertEqual(len(hits), 1)
        self.assertFalse(result['native_search_index_used'])
        self.assertTrue(all(p['useStateDbOnly'] for m,p in self.client.calls if m=='thread/list'))
        self.assertNotIn('thread/search', [m for m,p in self.client.calls])

    async def test_same_root_separate_sessions_and_titles_remain_distinct(self):
        self.client.rows = [thread('one','Same',A),thread('two','Same',A)]
        self.client.meta = {t['id']:t for t in self.client.rows}
        result, hits, _ = await self.all_search()
        self.assertEqual(len(hits), 2)
        self.assertNotEqual(hits[0]['session_ref'], hits[1]['session_ref'])
        self.assertEqual(result['directory_leads'][0]['matching_sessions'], 2)
        end, sessions, _ = await self.all_catalog(await self.scope())
        self.assertEqual(len(sessions), 2); self.assertTrue(end['selection_complete'])

    async def test_archived_included_by_default(self):
        self.client.archive = [thread('old','Archived',B)]; self.client.meta['old'] = self.client.archive[0]
        result, hits, _ = await self.all_search()
        self.assertEqual({h['archived'] for h in hits}, {False, True})
        self.assertEqual(len(result['directory_leads']),2)

    async def test_active_only_does_not_read_archived(self):
        self.client.archive = [thread('old','Archived',B)]; self.client.meta['old'] = self.client.archive[0]
        result, hits, _ = await self.all_search(include_archived=False)
        self.assertEqual(len(hits),1)
        self.assertTrue(all(not p['archived'] for m,p in self.client.calls if m=='thread/list'))

    async def test_multiple_roots_in_search_restrict_exact_cwd(self):
        self.client.rows += [thread('two','Other',B),thread('nested','Nested',A+'/child')]
        self.client.meta = {t['id']:t for t in self.client.rows}
        result, hits, _ = await self.all_search(roots=[A,B])
        self.assertEqual(len(hits),2); self.assertEqual(result['progress']['outside_selected_roots'],1)

    async def test_no_title_or_preview_substitution_for_message_search(self):
        self.client.rows[0]['name'] = 'Target'; self.client.meta['one']['name'] = 'Target'
        self.client.rows[0]['preview'] = 'Target private preview'
        self.reader.return_value = page('different text')
        result, hits, _ = await self.all_search()
        self.assertEqual(hits,[]); self.assertTrue(result['negative_result_valid'])

    async def test_match_hidden_beyond_metadata_found(self):
        self.reader.return_value = page('A long unrelated intro. Target is here.')
        _,hits,_ = await self.all_search()
        self.assertIn('Target',hits[0]['sample_excerpts'][0]['excerpt'])

    async def test_background_and_shared_session_family_excluded(self):
        child = thread('child','Background',A,sessionId='one',parentThreadId='one')
        self.client.rows += [child]; self.client.meta['child'] = child
        result,hits,_ = await self.all_search()
        self.assertEqual(len(hits),1)
        self.assertEqual(result['progress']['background_or_unclassified_excluded'],1)
        self.assertEqual(self.reader.await_count,1)

    async def test_distinct_user_fork_with_shared_family_not_collapsed(self):
        fork = thread('fork','First',A,sessionId='one',forkedFromId='one')
        self.client.rows += [fork]; self.client.meta['fork'] = fork
        result,hits,_ = await self.all_search()
        self.assertEqual(len(hits),2)

    async def test_metadata_error_is_not_no_match_and_others_continue(self):
        self.client.rows.insert(0,thread('missing','Invalid',A))
        result,hits,_ = await self.all_search()
        self.assertEqual(len(hits),1); self.assertFalse(result['search_complete'])
        self.assertEqual(result['progress']['candidate_failures'],1)

    async def test_history_error_isolated(self):
        self.client.rows += [thread('two','Second',B)]; self.client.meta['two'] = self.client.rows[-1]
        self.reader.side_effect = [b.CodexBridgeError('secret diagnostic'),page()]
        result,hits,_ = await self.all_search()
        self.assertEqual(len(hits),1); self.assertFalse(result['search_complete'])
        self.assertNotIn('secret diagnostic',json.dumps(result))
        self.assertEqual(result['progress']['sessions_failed_or_partial'],1)

    async def test_empty_inventory_not_valid_negative(self):
        self.client.rows=[]
        result,hits,_ = await self.all_search()
        self.assertFalse(result['search_complete']); self.assertFalse(result['negative_result_valid'])
        self.assertTrue(result['inventory_complete']); self.assertFalse(result['has_more_search'])

    async def test_inventory_error_and_known_verified_prefix_stays_partial(self):
        orig = self.client.request
        async def request(m,p,**kw):
            if m=='thread/list' and p['archived']: raise b.CodexAppServerError('private failure')
            return await orig(m,p,**kw)
        with patch.object(self.client,'request',request):
            result,hits,_ = await self.all_search()
        self.assertEqual(len(hits),1);self.assertFalse(result['inventory_complete']);self.assertFalse(result['search_complete'])

    async def test_repeated_inventory_cursor_stops_explicitly(self):
        orig = self.client.request
        async def request(m,p,**kw):
            if m=='thread/list': return {'data':[],'nextCursor':'repeat'}
            return await orig(m,p,**kw)
        with patch.object(self.client,'request',request): result,_,_=await self.all_search()
        self.assertFalse(result['inventory_complete']);self.assertFalse(result['negative_result_valid'])

    async def test_inventory_pagination_over_40_rows(self):
        self.client.rows=[thread(f'a{i}',f'Active {i}',A) for i in range(18)]
        self.client.archive=[thread(f'b{i}',f'Archive {i}',B) for i in range(22)]
        self.client.meta={t['id']:t for t in self.client.rows+self.client.archive}
        result,hits,pages=await self.all_search()
        self.assertEqual(result['progress']['sessions_searched_successfully'],40)
        self.assertEqual(len(hits),40);self.assertGreater(len(pages),2)
        self.assertTrue(result['search_complete'])

    async def test_source_restart_discards_unpublished_native_matches(self):
        self.reader.side_effect = [page('Target old',token='first'),
            page('replacement text', source='local_rollout_fallback', restart=True)]
        result,hits,_=await self.all_search(max_history_pages=1)
        self.assertEqual(hits,[]);self.assertTrue(result['negative_result_valid'])
        self.assertEqual(result['progress']['source_restarts'],1)
        self.assertEqual(result['progress']['history_pages_accepted'],1)

    async def test_unannounced_source_change_cannot_finish(self):
        self.reader.side_effect=[page(token='first'),page(source='local_rollout_fallback')]
        result,hits,_=await self.all_search()
        self.assertFalse(result['search_complete']);self.assertFalse(hits[0]['session_search_complete'])

    async def test_fallback_gap_blocks_negative(self):
        self.reader.return_value=page('no match',source='local_rollout_fallback',gaps=True)
        result,hits,_=await self.all_search()
        self.assertFalse(result['negative_result_valid']);self.assertFalse(result['search_complete'])

    async def test_current_native_nonmessage_types_do_not_block_complete_search(self):
        result_page = page('ordinary text')
        for kind in ('hookPrompt','collabAgentToolCall','subAgentActivity','sleep','imageGeneration'):
            result_page['turns'][0]['items'] += page('metadata only',kind=kind,role=None,field='note')['turns'][0]['items']
        self.reader.return_value = result_page
        result,hits,_=await self.all_search()
        self.assertTrue(result['search_complete'])
        self.assertEqual(hits,[])
        event=result['session_events'][0]
        self.assertEqual(event['unsupported_native_item_types'],[])

    async def test_unknown_native_projection_blocks_negative_and_reports_type(self):
        self.reader.return_value=page('payload withheld',kind='futureThing',role=None,field='note')
        result,_,_=await self.all_search()
        self.assertFalse(result['search_complete'])
        self.assertEqual(result['session_events'][0]['unsupported_native_item_types'],['futureThing'])

    async def test_only_user_assistant_text_is_searched(self):
        original=page('ordinary')
        for kind,role,field in [('reasoning','assistant','text'),('plan','plan','text'),
            ('contextCompaction',None,'checkpoint[0].text'),('collaborationEvent',None,'content'),
            ('commandExecution',None,'command'),('mcpToolCall',None,'result')]:
            original['turns'][0]['items'] += page('Target',kind=kind,role=role,field=field)['turns'][0]['items']
        self.reader.return_value=original
        _,hits,_=await self.all_search()
        self.assertEqual(hits,[])

    async def test_analysis_phase_not_searched(self):
        self.reader.return_value=page('Target',phase='analysis')
        _,hits,_=await self.all_search();self.assertEqual(hits,[])

    async def test_fragment_boundary_and_tail_resumption(self):
        self.reader.side_effect=[page('prefix Tar',token='p1',total=21),page('get suffix!',start=10,total=21)]
        result,hits,_=await self.all_search(max_history_pages=1)
        self.assertTrue(result['search_complete']);self.assertEqual(hits[0]['message_occurrences_observed'],1)
        self.assertEqual(hits[0]['sample_excerpts'][0]['match_offset'],7)

    async def test_do_not_join_different_messages(self):
        p=page('Tar');p['turns'][0]['items']+=page('get',iid='other')['turns'][0]['items']
        self.reader.return_value=p
        _,hits,_=await self.all_search();self.assertEqual(hits,[])

    async def test_broken_fragment_offsets_fail_not_skip(self):
        self.reader.side_effect=[page('Tar',token='p1',total=6),page('get',start=4,total=7)]
        result,hits,_=await self.all_search()
        self.assertFalse(result['search_complete'])

    async def test_missing_prefix_at_eof_is_failure(self):
        self.reader.return_value=page('Target',start=5,total=11)
        result,_,_=await self.all_search();self.assertFalse(result['search_complete'])

    async def test_character_limited_page_with_no_older_turns_continues(self):
        a=page('Tar',token='p1',total=6);a['has_older_turns']=False;a['character_truncated']=True
        self.reader.side_effect=[a,page('get',start=3,total=6)]
        result,hits,_=await self.all_search(max_history_pages=1)
        self.assertTrue(result['search_complete']);self.assertEqual(len(hits),1)

    async def test_repeated_history_position_is_failure(self):
        self.reader.return_value=page('ordinary',token='loop')
        result,_,_=await self.all_search(max_history_pages=1)
        self.assertFalse(result['search_complete']);self.assertEqual(self.reader.await_count,2)

    async def test_literal_query_not_regex(self):
        self.reader.return_value=page('Target .* text')
        result,hits,_=await self.all_search(query='.*')
        self.assertEqual(hits[0]['message_occurrences_observed'],1)

    async def test_case_sensitive_and_default_case_insensitive(self):
        self.reader.return_value=page('tArGeT')
        _,hits,_=await self.all_search();self.assertEqual(len(hits),1)
        _,hits,_=await self.all_search(case_sensitive=True);self.assertEqual(hits,[])

    async def test_positive_control_blocks_false_success(self):
        cat=await r.resolve_project_sessions(project=A)
        control=cat['sessions'][0]['session_ref']
        self.reader.return_value=page('different')
        result,_,_=await self.all_search(positive_control_session_ref=control)
        self.assertFalse(result['search_complete']);self.assertFalse(result['positive_control']['matched'])

    async def test_positive_control_passes_known_body(self):
        cat=await r.resolve_project_sessions(project=A)
        result,_,_=await self.all_search(positive_control_session_ref=cat['sessions'][0]['session_ref'])
        self.assertTrue(result['search_complete']);self.assertTrue(result['positive_control']['matched'])

    async def test_no_long_tokens_or_raw_ids_in_search_metadata(self):
        self.reader.side_effect=[page('ordinary',token='next'),page('Target')]
        first=await d.search_visible_history(query='Target',max_history_pages=1)
        self.assertRegex(first['next_page_token'],r'^cbh1_[0-9a-f]{40}$')
        self.assertFalse(first['search_complete']);self.assertEqual(first['matching_sessions'],[])
        cached=d._SEARCH.get(first['next_page_token'])
        self.assertNotIn('ordinary',json.dumps(cached))
        self.assertNotIn('one',json.dumps(first['matching_sessions']))

    async def test_search_token_query_flags_and_home_bound(self):
        self.reader.return_value=page('ordinary',token='next')
        first=await d.search_visible_history(query='Target',max_history_pages=1)
        for kw in ({'query':'different'},{'query':'Target','include_archived':False},{'query':'Target','roots':[B]}):
            with self.subTest(kw=kw),self.assertRaises(d.DiscoveryError):
                await d.search_visible_history(page_token=first['next_page_token'],**kw)
        with patch.dict('os.environ',{'CODEX_HOME':'/different'}),self.assertRaises(d.DiscoveryError):
            await d.search_visible_history(query='Target',page_token=first['next_page_token'])

    async def test_search_tokens_expire_without_silent_restart(self):
        self.reader.return_value=page('ordinary',token='next')
        first=await d.search_visible_history(query='Target',max_history_pages=1)
        d._SEARCH.clear()
        with self.assertRaises(r.ResolutionError):await d.search_visible_history(query='Target',page_token=first['next_page_token'])

    async def test_105_delayed_continuations_refresh_idle_ttl_and_keep_copy_safe_token(self):
        clock = [0.0]
        self.lifecycle_cache(clock, idle=10, absolute=1000)
        calls = 0
        async def long_reader(**kwargs):
            nonlocal calls
            calls += 1
            return page('ordinary', token=f'position-{calls}' if calls < 105 else None)
        self.reader.side_effect = long_reader
        token = None
        for index in range(105):
            result = await d.search_visible_history(query='Target', page_token=token, max_history_pages=1)
            if index == 0:
                token = result['next_page_token']
                self.assertRegex(token, r'^cbh1_[0-9a-f]{40}$')
            elif index < 104:
                self.assertEqual(result['next_page_token'], token)
            self.assertEqual(result['search_handle_lifecycle']['idle_age_seconds'], 0.0)
            self.assertNotIn(token or '', json.dumps(result['search_handle_lifecycle']))
            clock[0] += 5.0
        self.assertEqual(calls, 105)
        self.assertTrue(result['search_complete'])
        self.assertIsNone(result['next_page_token'])
        self.assertFalse(result['search_handle_lifecycle']['active'])

    async def test_invalid_continuation_does_not_refresh_idle_lifetime(self):
        clock = [0.0]
        self.lifecycle_cache(clock, idle=10, absolute=1000)
        self.reader.return_value = page('ordinary', token='next')
        first = await d.search_visible_history(query='Target', max_history_pages=1)
        clock[0] = 9.0
        with self.assertRaises(d.DiscoveryError):
            await d.search_visible_history(query='different', page_token=first['next_page_token'], max_history_pages=1)
        clock[0] = 10.0
        with self.assertRaisesRegex(r.ResolutionError, 'Idle age 10.000s / TTL 10.000s'):
            await d.search_visible_history(query='Target', page_token=first['next_page_token'], max_history_pages=1)

    async def test_absolute_expiry_wins_despite_successful_idle_refreshes(self):
        clock = [0.0]
        self.lifecycle_cache(clock, idle=10, absolute=20)
        self.reader.side_effect = lambda **kwargs: page('ordinary', token=f"position-{clock[0]}")
        first = await d.search_visible_history(query='Target', max_history_pages=1)
        token = first['next_page_token']
        for moment in (5.0, 10.0, 15.0):
            clock[0] = moment
            continued = await d.search_visible_history(query='Target', page_token=token, max_history_pages=1)
            self.assertEqual(continued['next_page_token'], token)
        clock[0] = 20.0
        with self.assertRaisesRegex(r.ResolutionError, 'absolute age 20.000s / TTL 20.000s'):
            await d.search_visible_history(query='Target', page_token=token, max_history_pages=1)

    async def test_process_and_version_invalidation_require_page_one_restart(self):
        self.reader.return_value = page('ordinary', token='next')
        first = await d.search_visible_history(query='Target', max_history_pages=1)
        token = first['next_page_token']
        with patch.object(b, 'BRIDGE_VERSION', '0.3.1'), self.assertRaisesRegex(d.DiscoveryError, 'another bridge version'):
            await d.search_visible_history(query='Target', page_token=token, max_history_pages=1)
        self.assertEqual(d._SEARCH.get(token)['bridge_version'], b.BRIDGE_VERSION)
        d._SEARCH.clear()
        with self.assertRaisesRegex(r.ResolutionError, 'another bridge process'):
            await d.search_visible_history(query='Target', page_token=token, max_history_pages=1)

    async def test_search_snippets_bounded_but_scan_not_cut_off(self):
        self.reader.return_value=page(('Target '+ 'x'*200)*20)
        result,hits,_=await self.all_search()
        self.assertEqual(hits[0]['message_occurrences_observed'],20)
        self.assertEqual(len(hits[0]['sample_excerpts']),3);self.assertTrue(hits[0]['excerpts_limited'])
        self.assertTrue(result['search_complete'])

    async def test_inventory_session_limit_is_partial(self):
        self.client.rows+=[thread('two','Second',B)];self.client.meta['two']=self.client.rows[-1]
        with patch.object(d,'MAX_SESSIONS',1):result,_,_=await self.all_search()
        self.assertFalse(result['search_complete']);self.assertFalse(result['inventory_complete'])

    async def test_conflicting_archive_identity_not_silently_merged(self):
        self.client.archive=copy.deepcopy(self.client.rows)
        result,_,_=await self.all_search()
        self.assertFalse(result['search_complete']);self.assertEqual(result['progress']['candidate_failures'],1)

    async def test_create_scope_requires_confirmation_no_reads(self):
        x=await d.create_project_scope(project_name='Example',roots=[A,B])
        self.assertEqual(x['status'],'confirmation_required');self.assertIsNone(x['project_scope_ref'])
        self.assertEqual(self.client.calls,[]);self.reader.assert_not_called()

    async def test_scope_paths_are_exact_and_normalized_not_recursively_expanded(self):
        x=await d.create_project_scope(project_name='Example',roots=[A,'e:\\projects\\PRIMARY',A+'/child'],confirmed=True)
        self.assertEqual(len(x['roots']),2);self.assertRegex(x['project_scope_ref'],r'^cbp1_[0-9a-f]{40}$')
        self.assertEqual(self.client.calls,[])

    async def test_scope_two_roots_catalogs_all_sessions(self):
        self.client.rows += [thread('two','Second',A),thread('three','Third',B)]
        self.client.meta={t['id']:t for t in self.client.rows}
        result,sessions,pages=await self.all_catalog(await self.scope([A,B]),limit=1)
        self.assertEqual(len(sessions),3);self.assertTrue(result['selection_complete'])
        self.assertEqual([z['verified_sessions'] for z in result['root_reports']],[2,1])
        self.reader.assert_not_called()

    async def test_scope_archives_included_and_background_excluded(self):
        self.client.archive=[thread('old','Old',A)];self.client.meta['old']=self.client.archive[0]
        self.client.rows += [thread('child','Child',A,parentThreadId='one',sessionId='one')]
        self.client.meta['child']=self.client.rows[-1]
        result,sessions,_=await self.all_catalog(await self.scope())
        self.assertEqual(len(sessions),2);self.assertEqual({s['archived'] for s in sessions},{True,False})
        self.assertEqual(result['root_reports'][0]['excluded_candidates'],1)

    async def test_failed_root_does_not_abandon_another(self):
        self.client.rows += [thread('two','Second',B)];self.client.meta['two']=self.client.rows[-1]
        orig=r.resolve_project_sessions
        async def resolve(**kw):
            if kw['project']==A:raise r.ResolutionError('failed root')
            return await orig(**kw)
        with patch.object(r,'resolve_project_sessions',resolve):
            result,sessions,pages=await self.all_catalog(await self.scope([A,B]))
        self.assertTrue(result['catalog_traversal_complete']);self.assertFalse(result['inventory_complete'])
        self.assertFalse(result['selection_complete']);self.assertEqual(len(sessions),1)
        self.assertEqual(result['root_reports'][0]['status'],'failed')

    async def test_rejected_session_preserves_root_failure_counts(self):
        self.client.rows.insert(0,thread('missing','Missing',A))
        result,sessions,_=await self.all_catalog(await self.scope())
        self.assertTrue(result['inventory_complete']);self.assertFalse(result['selection_complete'])
        self.assertEqual(len(sessions),1);self.assertEqual(result['root_reports'][0]['rejected_candidates'],1)

    async def test_scope_tokens_cannot_cross_scope_or_home(self):
        first=await self.scope([A,B]);second=await self.scope([A,B])
        p=await d.catalog_project_scope(project_scope_ref=first)
        with self.assertRaises(d.DiscoveryError):await d.catalog_project_scope(project_scope_ref=second,page_token=p['next_page_token'])
        with patch.dict('os.environ',{'CODEX_HOME':'/different'}),self.assertRaises(d.DiscoveryError):
            await d.catalog_project_scope(project_scope_ref=first)

    async def test_existing_resolver_reference_still_readable_after_scope_catalog(self):
        self.patch_read.stop()
        _,sessions,_=await self.all_catalog(await self.scope())
        result=await r.read_session_ref(session_ref=sessions[0]['session_ref'],max_chars=2000)
        self.assertTrue(result['has_more_content'])

    async def test_invalid_arguments_rejected_before_access(self):
        for q in ('', ' '*3, 'x'*257, 'line\nline'):
            with self.assertRaises(d.DiscoveryError):await d.search_visible_history(query=q)
        for roots in ([], 'E:/path', ['relative'], [A]*33):
            with self.assertRaises((d.DiscoveryError,r.ResolutionError)):await d.create_project_scope(project_name='Example',roots=roots,confirmed=True)
        with self.assertRaises(d.DiscoveryError):await d.search_visible_history(query='Target',max_history_pages=True)
        self.assertEqual(self.client.calls,[])

    async def test_verifier_suppresses_transcript_tokens_and_raw_id(self):
        args=argparse.Namespace(query='Target',root=None,active_only=False,case_sensitive=False,
                               max_history_pages=4,max_calls=100,expect_root=[A],expect_session_name=[])
        output=io.StringIO()
        self.reader.return_value=page('Target PRIVATE_TEXT_NOT_TO_PRINT')
        with contextlib.redirect_stdout(output):code=await v.search(args)
        self.assertEqual(code,0);self.assertNotIn('PRIVATE_TEXT_NOT_TO_PRINT',output.getvalue())
        for prefix in ('cbs1_','cbc1_','cbh1_'):self.assertNotIn(prefix,output.getvalue())
        self.assertIn('PASS_SEARCH_FINISHED',output.getvalue())

    async def test_verifier_rejects_missing_positive_root(self):
        args=argparse.Namespace(query='Target',root=None,active_only=False,case_sensitive=False,
                               max_history_pages=4,max_calls=100,expect_root=[B],expect_session_name=[])
        output=io.StringIO()
        with contextlib.redirect_stdout(output):code=await v.search(args)
        self.assertEqual(code,2);self.assertIn('FAIL_EXPECTED_MATCH_NOT_FOUND',output.getvalue())

    async def test_real_reader_native_projection_is_searched(self):
        self.patch_read.stop()
        result,hits,_=await self.all_search(query='safe message')
        self.assertTrue(result['search_complete']);self.assertEqual(len(hits),1)
        self.assertTrue(hits[0]['sample_excerpts'][0]['excerpt'].startswith('safe'))

    async def test_real_local_fallback_reader_search(self):
        self.patch_read.stop()
        with tempfile.TemporaryDirectory() as tmp,patch.dict('os.environ',{'CODEX_HOME':tmp}):
            path=Path(tmp)/'sessions'/('rollout-fixture-'+TID+'.jsonl');path.parent.mkdir()
            path.write_text('\n'.join(json.dumps(x) for x in [record('session_meta',id=TID,cwd=A),
                message('Target visible'),message('Target hidden',channel='analysis'),
                record('response_item',type='function_call_output',call_id='tool',output='Target tool')])+'\n')
            self.client.rows=[thread(TID,'Fallback',A)];self.client.meta={TID:self.client.rows[0]}
            with patch.object(b,'_read_session_app_server',side_effect=b.CodexAppServerError('16 MiB limit')):
                result,hits,_=await self.all_search()
            self.assertTrue(result['search_complete']);self.assertEqual(len(hits),1)
            self.assertEqual(hits[0]['history_source'],'local_rollout_fallback')
            self.assertEqual(hits[0]['message_occurrences_observed'],1)
            self.assertNotIn('hidden',json.dumps(hits))


class FragmentTests(unittest.TestCase):
    def test_every_boundary_of_literal_with_unicode(self):
        text='αβ before Target after 😀';query='Target'
        for cut in range(len(text)+1):
            current=d._current_session({})
            d._scan_page(current,page(text[:cut],start=0,total=len(text)),re.compile(query,re.I),len(query))
            if cut<len(text):d._scan_page(current,page(text[cut:],start=cut,total=len(text)),re.compile(query,re.I),len(query))
            self.assertEqual(current['occurrences'],1,cut)
            self.assertIsNone(current['tail'])

    def test_overlapping_matches_counted_consistently_across_many_pages(self):
        text='a'*500;query='aaa';current=d._current_session({})
        for start in range(0,len(text),13):
            d._scan_page(current,page(text[start:start+13],start=start,total=len(text)),re.compile(query),len(query))
        self.assertEqual(current['occurrences'],498)
        self.assertEqual(len(current['excerpts']),3)

    def test_memory_state_bound_fails_explicitly(self):
        with patch.object(d,'MAX_STATE_BYTES',20),self.assertRaises(d.DiscoveryError):
            d._put(d._SEARCH,{'large':'x'*30})

    def test_new_tools_registered_read_only(self):
        server=import_server_with_sdk_stub()
        for name in ('codex_search_visible_history','codex_create_project_scope','codex_catalog_project_scope'):
            fn,meta=server.mcp.tools[name]
            self.assertTrue(meta['annotations']['read_only_hint'])
            self.assertIn('READ-ONLY',fn.__doc__)

    def test_help_and_flags_available(self):
        output=io.StringIO()
        with contextlib.redirect_stdout(output),self.assertRaises(SystemExit) as exc:v.main(['search','--help'])
        self.assertEqual(exc.exception.code,0)
        self.assertIn('--expect-root',output.getvalue())


if __name__=='__main__':unittest.main()


class DiscoveryTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_empty_index_and_multi_root_scope(self):
        for cache in (r._SESSIONS, r._PAGES, r._CATALOGS, d._SEARCH, d._SCOPES, d._SCOPE_PAGES):cache.clear()
        mock = str(Path(__file__).with_name('mock_discovery_app_server.py'))
        with patch.object(b, 'build_codex_command', return_value=[sys.executable, mock]),patch.object(d,'SEARCH_CHARS',2000):
            token=None; hits=[]
            for _ in range(100):
                reply=await d.search_visible_history(query='ProjectSignal',page_token=token,max_history_pages=2)
                hits+=reply['matching_sessions'];token=reply['next_page_token']
                if not token:break
            self.assertTrue(reply['search_complete'], reply);self.assertEqual(len(hits),3)
            self.assertEqual({h['cwd'] for h in hits},{'/synthetic/primary','/synthetic/secondary'})
            self.assertEqual([h['message_occurrences_observed'] for h in hits],[1,1,1])
            created=await d.create_project_scope(project_name='Example',roots=['/synthetic/primary','/synthetic/secondary'],confirmed=True)
            token=None;sessions=[]
            for _ in range(20):
                catalog=await d.catalog_project_scope(project_scope_ref=created['project_scope_ref'],page_token=token,limit=1)
                sessions+=catalog['sessions'];token=catalog['next_page_token']
                if not token:break
            self.assertTrue(catalog['selection_complete']);self.assertEqual(len(sessions),3)
            result=await r.read_session_ref(session_ref=sessions[0]['session_ref'],max_chars=4000)
            self.assertIn('turns',result)

    async def test_wrapped_repeated_upstream_cursor_is_detected(self):
        # Each wrapper token can differ while the underlying position is stuck.
        c=Client([thread('one','Notes',A)])
        for cache in (r._SESSIONS,r._PAGES,d._SEARCH):cache.clear()
        async def fake_read(**kw):
            token=r._PAGES.put({'reader_token':'same-upstream-position'})
            return page('ordinary',token=token)
        with patch.object(b,'CodexAppServerClient',c),patch.object(r,'read_session_ref',fake_read):
            first=await d.search_visible_history(query='Target',max_history_pages=1)
            second=await d.search_visible_history(query='Target',page_token=first['next_page_token'],max_history_pages=1)
        self.assertFalse(second['search_complete']);self.assertFalse(second['has_more_search'])

    async def test_native_index_diagnostic_can_never_claim_negative(self):
        server=import_server_with_sdk_stub()
        with patch.object(server,'search_history',AsyncMock(return_value={'matches':[]})):
            out=await server.codex_search_history(query='Target')
        self.assertFalse(out['negative_result_valid']);self.assertFalse(out['search_complete'])
