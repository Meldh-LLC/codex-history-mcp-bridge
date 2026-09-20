"""Synthetic session resolution/identity tests. No real user identifiers."""
from __future__ import annotations
import argparse
import asyncio
import contextlib
import copy
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import codex_bridge as b
import session_resolver as r
import verify_resolution as verify
from test_more_cases import import_server_with_sdk_stub

PROJECT = 'E:/projects/writing'
OTHER = 'E:/projects/design'


def thread(tid='thread-one', name='Website', cwd=PROJECT, **kw):
    return {'id':tid,'name':name,'cwd':cwd,'sessionId':tid,'parentThreadId':None,
            'forkedFromId':None,'historyMode':'paginated','createdAt':1000,'updatedAt':2000,
            'source':'vscode','threadSource':'user','ephemeral':False,'path':None, **kw}


class Client:
    def __init__(self, rows=None, archive=None, meta=None):
        self.rows=rows if rows is not None else [thread()]
        self.archive=archive or []
        self.meta={x['id']:copy.deepcopy(x) for x in self.rows+self.archive}
        if meta: self.meta.update(meta)
        self.calls=[]
        self.errors=set()
    def __call__(self): return self
    async def __aenter__(self): return self
    async def __aexit__(self,*args): pass
    async def request(self,method,params,**kw):
        self.calls.append((method,copy.deepcopy(params)))
        if method=='thread/list':
            rows=self.archive if params['archived'] else self.rows
            if params.get('cwd'):
                rows=[x for x in rows if r.canonical_path(x['cwd'])==r.canonical_path(params['cwd'])]
            start=int(params.get('cursor','0')); end=start+params['limit']
            return {'data':copy.deepcopy(rows[start:end]),'nextCursor':str(end) if end<len(rows) else None}
        if method=='thread/read':
            tid=params['threadId']
            if tid in self.errors or tid not in self.meta:
                raise b.CodexAppServerError('thread not loaded synthetic',code=-32600)
            return {'thread':copy.deepcopy(self.meta[tid])}
        if method=='thread/turns/list':
            return {'data':[{'id':'turn-1','status':'completed','itemsView':'notLoaded'}], 'nextCursor':None}
        if method=='thread/items/list':
            return {'data':[{'turnId':'turn-1','item':{'type':'agentMessage','id':'message-1','text':'safe message '*1000}}], 'nextCursor':None}
        raise AssertionError(method)


class ResolutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        r._SESSIONS.clear();r._CATALOGS.clear();r._PAGES.clear()
        self.c=Client()
        self.p=patch.object(b,'CodexAppServerClient',self.c);self.p.start();self.addCleanup(self.p.stop)
    async def resolve(self, **kw):
        return await r.resolve_project_sessions(project=kw.pop('project',PROJECT),**kw)
    async def ref(self):
        return (await self.resolve())['sessions'][0]['session_ref']
    async def test_state_only_metadata_only_and_no_previews(self):
        self.c.rows[0]['preview']='PRIVATE PREVIEW'; self.c.meta['thread-one']['preview']='PRIVATE PREVIEW'
        a=await self.resolve()
        self.assertTrue(a['inventory_complete']);self.assertFalse(a['session_contents_read'])
        self.assertEqual(a['sessions'][0]['validation_status'],'metadata_verified')
        self.assertEqual(a['sessions'][0]['history_readability'],'not_probed')
        self.assertNotIn('thread-one',json.dumps(a)); self.assertNotIn('PRIVATE PREVIEW',json.dumps(a))
        self.assertTrue(all(p['useStateDbOnly'] for m,p in self.c.calls if m=='thread/list'))
        self.assertTrue(all(p['includeTurns'] is False for m,p in self.c.calls if m=='thread/read'))
        self.assertEqual({m for m,p in self.c.calls},{'thread/list','thread/read'})
    async def test_invalid_candidate_does_not_abandon_others(self):
        self.c.rows=[thread('invalid-id'),thread('valid-id','Website 2')]
        self.c.meta={'valid-id':self.c.rows[1]}
        a=await self.resolve()
        self.assertEqual(len(a['rejected_candidates']),1);self.assertEqual(len(a['sessions']),1)
        self.assertTrue(a['inventory_complete'])
        self.assertEqual(a['sessions'][0]['display_name'],'Website 2')
    async def test_default_excludes_subagents_even_same_family(self):
        self.c.rows=[thread('root'),thread('child',parentThreadId='root',sessionId='root'),thread('review',threadSource='guardian_review')]
        self.c.meta={t['id']:t for t in self.c.rows}
        a=await self.resolve()
        self.assertEqual(len(a['sessions']),1);self.assertEqual(len(a['excluded_candidates']),2)
        self.assertEqual(r._SESSIONS.get(a['sessions'][0]['session_ref'])['identity']['thread_id'],'root')
    async def test_opt_in_subagents_keep_distinct_thread_identity(self):
        self.c.rows=[thread('root'),thread('child',parentThreadId='root',sessionId='root')]
        self.c.meta={t['id']:t for t in self.c.rows}
        a=await self.resolve(include_background=True,diagnostics=True)
        self.assertEqual(len(a['sessions']),2)
        self.assertEqual({x['diagnostics']['thread_id'] for x in a['sessions']},{'root','child'})
        self.assertEqual({x['diagnostics']['session_id'] for x in a['sessions']},{'root'})
    async def test_user_fork_is_not_a_subagent(self):
        self.c.rows=[thread('fork',sessionId='root',forkedFromId='root')];self.c.meta={'fork':self.c.rows[0]}
        a=await self.resolve();self.assertEqual(len(a['sessions']),1);self.assertTrue(a['sessions'][0]['is_fork'])
    async def test_tagged_subagent_source_is_excluded(self):
        self.c.meta['thread-one']['source']={'subagent':{'other':'review'}}
        a=await self.resolve();self.assertEqual(a['sessions'],[])
    async def test_unknown_source_is_not_silently_user_facing(self):
        self.c.meta['thread-one']['source']='newBackgroundMode'
        a=await self.resolve();self.assertEqual(a['sessions'],[])
    async def test_duplicate_titles_are_not_collapsed(self):
        self.c.rows=[thread('a','Same'),thread('b','Same')];self.c.meta={t['id']:t for t in self.c.rows}
        a=await self.resolve();self.assertEqual(len(a['sessions']),2)
        self.assertNotEqual(a['sessions'][0]['session_ref'],a['sessions'][1]['session_ref'])
    async def test_archive_is_explicit_and_separate(self):
        self.c.archive=[thread('old','Website')];self.c.meta['old']=self.c.archive[0]
        a=await self.resolve();self.assertEqual(len(a['sessions']),1)
        all_=await self.resolve(include_archived=True)
        self.assertEqual(len(all_['sessions']),2);self.assertEqual([x['archived'] for x in all_['sessions']],[False,True])
    async def test_active_archive_same_id_is_flagged_not_duplicated(self):
        self.c.archive=[thread()]
        a=await self.resolve(include_archived=True)
        self.assertEqual(len(a['sessions']),1)
        self.assertEqual(a['excluded_candidates'][0]['reason'],'active_archive_collision')
    async def test_duplicate_id_across_pages_is_flagged(self):
        self.c.rows=[thread(),thread()]
        a=await self.resolve(limit=1);bb=await self.resolve(limit=1,page_token=a['next_page_token'])
        self.assertEqual(bb['sessions'],[]);self.assertEqual(bb['excluded_candidates'][0]['reason'],'duplicate_thread_id')
    async def test_wrong_metadata_id_is_rejected(self):
        self.c.meta['thread-one']['id']='different'
        a=await self.resolve();self.assertFalse(a['sessions']);self.assertEqual(len(a['rejected_candidates']),1)
    async def test_wrong_metadata_cwd_is_rejected(self):
        self.c.meta['thread-one']['cwd']=OTHER
        a=await self.resolve();self.assertFalse(a['sessions'])
    async def test_changed_title_is_rejected_at_selection(self):
        self.c.meta['thread-one']['name']='renamed'
        a=await self.resolve();self.assertFalse(a['sessions'])
    async def test_changed_tree_identity_is_rejected(self):
        self.c.meta['thread-one']['sessionId']='another-root'
        a=await self.resolve();self.assertFalse(a['sessions'])
    async def test_changed_rollout_path_is_rejected(self):
        self.c.rows[0]['path']='E:/history/a.jsonl';self.c.meta['thread-one']['path']='E:/history/b.jsonl'
        a=await self.resolve();self.assertFalse(a['sessions'])
    async def test_ephemeral_thread_is_rejected(self):
        self.c.meta['thread-one']['ephemeral']=True
        a=await self.resolve();self.assertFalse(a['sessions'])
    async def test_unknown_history_mode_is_rejected_not_guessed(self):
        self.c.meta['thread-one']['historyMode']='future'
        a=await self.resolve();self.assertFalse(a['sessions'])
    async def test_missing_creation_time_is_rejected(self):
        self.c.meta['thread-one'].pop('createdAt')
        a=await self.resolve();self.assertFalse(a['sessions'])
    async def test_name_lookup_returns_choices_without_session_reads(self):
        self.c.rows += [thread('x',cwd='E:/other/writing')]
        a=await self.resolve(project='writing')
        self.assertEqual(a['status'],'choose_project');self.assertEqual(len(a['project_choices']),2)
        self.assertFalse(a['sessions']);self.assertTrue(all(m=='thread/list' for m,_ in self.c.calls))
    async def test_unique_name_match_still_requires_confirmation(self):
        a=await self.resolve(project='writing')
        self.assertEqual(a['status'],'choose_project');self.assertFalse(a['sessions'])
    async def test_project_name_not_found_is_scoped(self):
        a=await self.resolve(project='missing')
        self.assertEqual(a['status'],'project_not_found_in_inventory');self.assertFalse(a['sessions'])
    async def test_catalog_short_cursor_and_full_discovery(self):
        self.c.rows=[thread(str(i)) for i in range(7)];self.c.meta={t['id']:t for t in self.c.rows}
        refs=[];token=None
        while True:
            a=await self.resolve(limit=2,page_token=token)
            refs+=a['sessions'];token=a['next_page_token']
            if not token:break
            self.assertRegex(token,r'^cbq1_[a-f0-9]{40}$')
        self.assertEqual(len(refs),7);self.assertEqual(a['inventory_rows_accounted_for'],7)
    async def test_catalog_no_transcript_preview_cache(self):
        self.c.rows=[thread(str(i),preview='SECRET_PREVIEW',turns=['SECRET_TEXT']) for i in range(3)]
        self.c.meta={t['id']:t for t in self.c.rows}
        a=await self.resolve(limit=1)
        cached=r._CATALOGS.get(a['next_page_token'])
        self.assertNotIn('SECRET',json.dumps(cached))
    async def test_catalog_rejects_changed_project_flags_home(self):
        self.c.rows=[thread('a'),thread('b')];self.c.meta={t['id']:t for t in self.c.rows}
        a=await self.resolve(limit=1);tok=a['next_page_token']
        for kw in ({'project':OTHER},{'include_archived':True},{'include_background':True},{'diagnostics':True}):
            with self.subTest(kw=kw),self.assertRaises(r.ResolutionError):await self.resolve(page_token=tok,**kw)
        with patch.dict('os.environ',{'CODEX_HOME':'/different-home'}),self.assertRaises(r.ResolutionError):
            await self.resolve(page_token=tok)
    async def test_initial_read_revalidates_before_reader(self):
        ref=await self.ref();self.c.calls.clear()
        with patch.object(b,'read_session',side_effect=AssertionError('reader should not run')):
            self.c.meta['thread-one']['name']='Renamed'
            a=await r.read_session_ref(session_ref=ref)
        self.assertEqual(a['status'],'stale_session_reference');self.assertTrue(a['history_restart_required'])
    async def test_stale_id_refresh_suggests_without_redirecting(self):
        ref=await self.ref()
        self.c.rows=[thread('replacement')];self.c.meta={'replacement':thread('replacement')}
        with patch.object(b,'read_session',side_effect=AssertionError('no automatic redirect')):
            a=await r.read_session_ref(session_ref=ref)
        self.assertTrue(a['replacement_confirmation_required']);self.assertEqual(len(a['replacement_candidates']),1)
        self.assertTrue(a['independent_sessions_may_continue'])
    async def test_ambiguous_replacement_keeps_both_choices(self):
        ref=await self.ref()
        self.c.rows=[thread('a'),thread('b')];self.c.meta={t['id']:t for t in self.c.rows}
        a=await r.read_session_ref(session_ref=ref)
        self.assertEqual(len(a['replacement_candidates']),2)
    async def test_updated_timestamp_does_not_break_same_identity(self):
        ref=await self.ref();self.c.meta['thread-one']['updatedAt']=9000
        a=await r.read_session_ref(session_ref=ref,max_chars=4000)
        self.assertNotEqual(a.get('status'),'stale_session_reference')
    async def test_ref_rejects_raw_id(self):
        with self.assertRaises(r.ResolutionError):await r.read_session_ref(session_ref='thread-one')
        self.assertEqual(self.c.calls,[])
    async def test_ref_does_not_cross_codex_home(self):
        ref=await self.ref();self.c.calls.clear()
        with patch.dict('os.environ',{'CODEX_HOME':'/changed-home'}),self.assertRaises(r.ResolutionError):
            await r.read_session_ref(session_ref=ref)
        self.assertEqual(self.c.calls,[])
    async def test_wrapped_native_continuations_preserve_reader_text(self):
        ref=await self.ref();token=None;text='';n=0
        while True:
            a=await r.read_session_ref(session_ref=ref,page_token=token,max_chars=4000)
            text+=''.join(f['text'] for t in a['turns'] for i in t['items'] for f in i['fragments'] if f['field']=='text')
            n+=1;token=a['next_page_token']
            if not token:break
            self.assertRegex(token,r'^cbc1_[a-f0-9]{40}$')
            self.assertEqual(len(token),45);self.assertNotIn('cb2_',json.dumps(a))
        self.assertEqual(text,'safe message '*1000);self.assertGreater(n,1)
    async def test_token_is_bound_to_reference_and_flags(self):
        a=await self.resolve();ref=a['sessions'][0]['session_ref']
        page=await r.read_session_ref(session_ref=ref,max_chars=2000)
        other=await self.ref();tok=page['next_page_token']
        with self.assertRaises(r.ResolutionError):await r.read_session_ref(session_ref=other,page_token=tok)
        with self.assertRaises(r.ResolutionError):await r.read_session_ref(session_ref=ref,page_token=tok,include_tool_output=True)
    async def test_same_reference_token_replay_deterministic(self):
        ref=await self.ref();first=await r.read_session_ref(session_ref=ref,max_chars=2000)
        a=await r.read_session_ref(session_ref=ref,page_token=first['next_page_token'],max_chars=3000)
        bb=await r.read_session_ref(session_ref=ref,page_token=first['next_page_token'],max_chars=3000)
        self.assertEqual(a['turns'],bb['turns'])
    async def test_process_restart_expires_refs_without_io(self):
        ref=await self.ref();r._SESSIONS.clear();self.c.calls.clear()
        with self.assertRaisesRegex(r.ResolutionError,'expired'):await r.read_session_ref(session_ref=ref)
        self.assertEqual(self.c.calls,[])
    async def test_read_error_does_not_invalidate_other_reference(self):
        self.c.rows=[thread('a'),thread('b')];self.c.meta={t['id']:t for t in self.c.rows}
        catalog=await self.resolve()
        async def reader(**kw):
            if kw['thread_id']=='a':raise b.CodexBridgeError('synthetic failure')
            return {'thread':{'id':'b'},'has_more_content':False,'next_page_token':None,'turns':[]}
        with patch.object(b,'read_session',reader):
            with self.assertRaises(b.CodexBridgeError):await r.read_session_ref(session_ref=catalog['sessions'][0]['session_ref'])
            a=await r.read_session_ref(session_ref=catalog['sessions'][1]['session_ref'])
        self.assertFalse(a['has_more_content'])
    async def test_source_restart_marker_is_preserved(self):
        ref=await self.ref()
        async def reader(**kw):return {'thread':{'id':'thread-one'},'has_more_content':True,'next_page_token':'cbr4_internal',
                                      'history_source':'local_rollout_fallback','history_restart_required':True,'turns':[]}
        with patch.object(b,'read_session',reader):a=await r.read_session_ref(session_ref=ref)
        self.assertTrue(a['history_restart_required']);self.assertRegex(a['next_page_token'],r'^cbc1_')
        self.assertNotIn('cbr4_internal',json.dumps(a))
    async def test_reader_changed_project_not_returned(self):
        ref=await self.ref()
        async def reader(**kw):return {'thread':{'id':'thread-one','cwd':OTHER},'has_more_content':False,'next_page_token':None,'turns':[]}
        with patch.object(b,'read_session',reader),self.assertRaises(r.ResolutionError):await r.read_session_ref(session_ref=ref)

    async def test_actual_fallback_pages_through_reference_wrapper(self):
        import tempfile
        import os
        tid='77777777-8888-4999-8aaa-bbbbbbbbbbbb'
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'CODEX_HOME':tmp}):
            path=Path(tmp)/'sessions'/('rollout-test-'+tid+'.jsonl');path.parent.mkdir()
            rows=[{'type':'session_meta','payload':{'id':tid,'history_mode':'paginated'}},
                  {'type':'response_item','payload':{'type':'message','role':'assistant','content':[{'type':'output_text','text':'fallback text '*1800}]}}]
            path.write_text('\n'.join(json.dumps(x) for x in rows)+'\n',encoding='utf-8')
            self.c.rows=[thread(tid)];self.c.meta={tid:self.c.rows[0]}
            orig=self.c.request
            async def request(method,params,**kw):
                if method=='thread/turns/list':raise b.CodexAppServerError('Codex app-server exited or disconnected (synthetic transport fixture).')
                return await orig(method,params,**kw)
            with patch.object(self.c,'request',request):
                ref=await self.ref();token=None;recovered='';pages=0
                while True:
                    a=await r.read_session_ref(session_ref=ref,page_token=token,max_chars=4000)
                    self.assertEqual(a['history_source'],'local_rollout_fallback')
                    recovered+=''.join(f['text'] for t in a['turns'] for i in t['items'] for f in i['fragments'] if f['field']=='text')
                    pages+=1;token=a['next_page_token']
                    if not token:break
                    self.assertRegex(token,r'^cbc1_[a-f0-9]{40}$')
                self.assertEqual(recovered,'fallback text '*1800)
                self.assertTrue(a['projection_coverage_complete']);self.assertGreater(pages,1)

    async def test_reader_wrong_id_not_returned(self):
        ref=await self.ref()
        async def reader(**kw):return {'thread':{'id':'other'},'turns':['SENSITIVE']}
        with patch.object(b,'read_session',reader),self.assertRaises(r.ResolutionError):await r.read_session_ref(session_ref=ref)
    async def test_bad_flag_types_fail_closed(self):
        for kw in ({'include_archived':'false'},{'include_background':1},{'limit':0},{'limit':True}):
            with self.subTest(kw=kw),self.assertRaises(r.ResolutionError):await self.resolve(**kw)
        self.assertEqual(self.c.calls,[])
    async def test_relative_path_not_resolved_against_arbitrary_process_cwd(self):
        for v in ('.\\writing','E:writing','~/writing'):
            with self.subTest(v=v),self.assertRaises(r.ResolutionError):await self.resolve(project=v)
    async def test_inventory_malformed_not_empty_success(self):
        async def malformed(*a,**kw):return {'data':None}
        with patch.object(self.c,'request',malformed),self.assertRaises(r.ResolutionError):await self.resolve()
    async def test_catalog_repeated_cursor_fails_explicitly(self):
        async def repeat(method,params,**kw):
            if method=='thread/list':return {'data':[],'nextCursor':'repeat'}
        with patch.object(self.c,'request',repeat),self.assertRaisesRegex(r.ResolutionError,'repeated'):await self.resolve()
    async def test_no_supported_inventory_does_not_trigger_repair(self):
        async def fail(*a,**kw):raise b.CodexAppServerError('unsupported state only')
        with patch.object(self.c,'request',fail),self.assertRaises(b.CodexAppServerError):await self.resolve()
    async def test_verify_helper_is_payload_and_token_free(self):
        output=io.StringIO()
        args=argparse.Namespace(project=PROJECT,include_archived=False,pages_per_session=1,max_catalog_pages=10)
        with contextlib.redirect_stdout(output):code=await verify.run(args)
        self.assertEqual(code,0)
        for forbidden in ('safe message','cbs1_','cbc1_','cbq1_'):self.assertNotIn(forbidden,output.getvalue())
        self.assertIn('PASS_RESOLUTION_AND_BOUNDED_READS',output.getvalue())


class CacheAndPathTests(unittest.TestCase):
    def test_windows_extended_paths_match_without_io(self):
        self.assertEqual(r.canonical_path('E:/Projects/Writing'),r.canonical_path('\\\\?\\E:\\projects\\writing\\'))
    def test_unc_extended_paths_match(self):
        self.assertEqual(r.canonical_path('\\\\server\\share\\p'),r.canonical_path('\\\\?\\UNC\\server\\share\\p'))
    def test_file_uri_windows_matches(self):
        self.assertEqual(r.canonical_path('file:///E:/projects/writing'),r.canonical_path(PROJECT))
    def test_posix_case_not_folded(self):
        self.assertNotEqual(r.canonical_path('/projects/P'),r.canonical_path('/projects/p'))
    def test_no_underscore_or_directory_alias_guessing(self):
        self.assertNotEqual(r.canonical_path('E:/projects/Example_Writing'),r.canonical_path('E:/projects/Example/_Writing'))
    def test_ttl_and_eviction_fail_not_empty(self):
        cache=r.HandleCache('test_',limit=2,ttl=10)
        with patch.object(r.time,'monotonic',return_value=1):one=cache.put({'a':1});two=cache.put({'b':2})
        with patch.object(r.time,'monotonic',return_value=2):cache.put({'c':3})
        with patch.object(r.time,'monotonic',return_value=3),self.assertRaises(r.ResolutionError):cache.get(one)
        with patch.object(r.time,'monotonic',return_value=20),self.assertRaises(r.ResolutionError):cache.get(two)
    def test_cache_copy_isolation(self):
        cache=r.HandleCache('test_');state={'x':['initial']};tok=cache.put(state)
        state['x'][0]='mutated';a=cache.get(tok);a['x'][0]='again'
        self.assertEqual(cache.get(tok)['x'],['initial'])
    def test_cache_tokens_contain_no_state(self):
        cache=r.HandleCache('test_');tok=cache.put({'path':'E:/private','thread_id':'private-thread'})
        self.assertRegex(tok,r'^test_[a-f0-9]{40}$')
    def test_guide_is_document_first_not_fake_writer(self):
        guide=r.report_guide();self.assertEqual(guide['preferred_format'],'docx')
        self.assertIn('Never invent a file link',guide['instructions'])
        self.assertIn('does not write reports',guide['scope_note'])


class ResolverMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_read_only_tools_registered(self):
        s=import_server_with_sdk_stub()
        for name in ('codex_resolve_project_sessions','codex_read_session_ref','codex_report_guide'):
            fn,kw=s.mcp.tools[name];self.assertTrue(kw['annotations']['read_only_hint'])
        guide=await s.codex_report_guide();self.assertEqual(guide['preferred_format'],'docx')
    async def test_session_error_instruction_is_scoped(self):
        s=import_server_with_sdk_stub()
        async def fail(**kw):raise b.CodexBridgeError('no candidate')
        result=await s._call_history(fail)
        self.assertIn('affected session',result['instruction'])


class ResolverSubprocessTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_selection_and_short_cursor_read(self):
        fixture=Path(__file__).with_name('mock_resolver_app_server.py')
        r._SESSIONS.clear();r._CATALOGS.clear();r._PAGES.clear()
        with patch.object(b,'build_codex_command',return_value=[sys.executable,str(fixture)]):
            resolved=await r.resolve_project_sessions(project='/synthetic/project')
            self.assertEqual(len(resolved['sessions']),1);self.assertEqual(len(resolved['rejected_candidates']),1)
            ref=resolved['sessions'][0]['session_ref']
            a=await r.read_session_ref(session_ref=ref,max_chars=2000)
            self.assertTrue(a['has_more_content']);self.assertRegex(a['next_page_token'],r'^cbc1_')
            bb=await r.read_session_ref(session_ref=ref,page_token=a['next_page_token'],max_chars=120000)
            self.assertFalse(bb['has_more_content'])

if __name__=='__main__':unittest.main()
