"""Offline regression tests; all history below is synthetic, not user data."""
from __future__ import annotations
import asyncio
import copy
import json
import random
import re
import unittest
from unittest.mock import patch
import codex_bridge as bridge

THREAD = 'synthetic-test-session'


def message(turn_id, text, *, role='agentMessage', item_id=None):
    item = {'id': item_id or 'i-' + turn_id, 'type': role}
    if role == 'userMessage':
        item['content'] = [{'type': 'text', 'text': text}]
    else:
        item['text'] = text
    return {'id': turn_id, 'status': 'completed', 'items': [item], 'error': None}


class FakeClient:
    """Implements the documented methods, not the bridge's rendering algorithm."""
    def __init__(self, turns, mode='paginated', turns_supported=True, item_page_size=100):
        self.turns = copy.deepcopy(turns)
        self.mode = mode
        self.turns_supported = turns_supported
        self.item_page_size = item_page_size
        self.calls = []
        self.entered = 0
        self.exited = 0
    async def __aenter__(self):
        self.entered += 1
        return self
    async def __aexit__(self, *args):
        self.exited += 1
    def __call__(self):
        return self
    async def request(self, method, params=None, **kwargs):
        params = params or {}
        self.calls.append((method, copy.deepcopy(params)))
        if method == 'thread/read':
            meta = {'id': THREAD, 'historyMode': self.mode, 'cwd': 'E:/synthetic', 'status': {'type': 'notLoaded'}}
            if params.get('includeTurns'):
                if self.mode == 'paginated':
                    raise AssertionError('Whole-history read on paginated thread is forbidden')
                meta['turns'] = copy.deepcopy(self.turns)
            return {'thread': meta}
        if method == 'thread/turns/list':
            if not self.turns_supported:
                raise bridge.CodexAppServerError('method not found', code=-32601)
            self.assert_params(params)
            # JSON-looking opaque cursor deliberately exercises cb2 wrapping.
            end = json.loads(params['cursor'])['end'] if params.get('cursor') else len(self.turns)
            start = max(0, end-params['limit'])
            page = list(reversed(copy.deepcopy(self.turns[start:end])))
            if params['itemsView'] == 'notLoaded':
                for t in page:
                    t['items'] = []
                    t['itemsView'] = 'notLoaded'
            else:
                for t in page:
                    t['itemsView'] = 'full'
            return {'data': page, 'nextCursor': json.dumps({'end': start, 'opaque': 'a\\b"c'}) if start else None, 'backwardsCursor': None}
        if method == 'thread/items/list':
            if self.mode != 'paginated':
                raise AssertionError('Legacy item API was used')
            assert params['sortDirection'] == 'asc'
            source = next(t for t in self.turns if t['id'] == params['turnId'])['items']
            offset = int(params.get('cursor', '0'))
            limit = min(params['limit'], self.item_page_size)
            end = min(len(source), offset+limit)
            return {'data': [{'turnId': params['turnId'], 'item': copy.deepcopy(i)} for i in source[offset:end]], 'nextCursor': str(end) if end < len(source) else None}
        raise AssertionError(f'Unexpected/writing RPC: {method}')
    def assert_params(self, params):
        assert params['threadId'] == THREAD
        assert params['sortDirection'] == 'desc'
        assert 1 <= params['limit'] <= 100
        if self.mode == 'paginated':
            assert params['itemsView'] == 'notLoaded'
        else:
            assert params['itemsView'] == 'full'


class Assembler:
    """Independent offset/length validator, reconstructing every emitted field."""
    def __init__(self):
        self.fields = {}
        self.contexts = {}
        self.totals = {}
        self.completed = set()
        self.turn_ids = set()
    def add(self, result):
        for turn in result['turns']:
            self.turn_ids.add(turn['id'])
            for item in turn['items']:
                key = (turn['id'], item['id'], item['item_index'])
                self.contexts[key] = {k: item[k] for k in ('type','id','role','phase') if k in item}
                for frag in item['fragments']:
                    field_key = key + (frag['field'],)
                    current = self.fields.get(field_key, '')
                    assert field_key not in self.completed, f'duplicate complete field {field_key}'
                    assert frag['offset'] == len(current), f'gap or overlap {field_key}'
                    assert frag['end_offset'] == frag['offset'] + len(frag['text'])
                    assert frag['end_offset'] <= frag['total_chars']
                    assert self.totals.get(field_key, frag['total_chars']) == frag['total_chars']
                    self.totals[field_key] = frag['total_chars']
                    self.fields[field_key] = current + frag['text']
                    if frag['complete']:
                        assert frag['end_offset'] == frag['total_chars']
                        self.completed.add(field_key)
    def assert_complete(self):
        assert set(self.fields) == self.completed, 'unfinished field at end of history'
    def text(self, turn_id, item_id, field='text', item_index=0):
        return self.fields[(turn_id, item_id, item_index, field)]
    def items(self):
        result = copy.deepcopy(self.contexts)
        for key, text in self.fields.items():
            result[key[:3]][key[3]] = text
        return result


async def collect(fake, *, max_chars=2000, max_turns=100, **options):
    pages = []
    seen = set()
    token = None
    assembly = Assembler()
    with patch.object(bridge, 'CodexAppServerClient', fake):
        for _ in range(3000):
            result = await bridge.read_session(thread_id=THREAD, page_token=token, max_chars=max_chars, max_turns=max_turns, **options)
            pages.append(result)
            assembly.add(result)
            assert result['output_info']['payload_chars'] <= max(2000, min(max_chars,120000))
            assert result['output_info']['payload_chars'] == len(bridge._json_text(result['turns']))
            assert result['chars_returned'] == sum(len(f['text']) for t in result['turns'] for i in t['items'] for f in i['fragments'])
            assert result['has_more_content'] == (result['next_page_token'] is not None)
            assert result['page_complete'] == (not result['character_truncated'])
            if result['character_truncated']:
                assert result['next_page_token'], 'stranded content regression'
                assert result['continuation_reason'] == 'same_turn_page'
            token = result['next_page_token']
            if token is None:
                assembly.assert_complete()
                assert pages[-1]['has_older_turns'] is False
                return pages, assembly
            assert re.fullmatch(r'cb2_[A-Za-z0-9_-]+\.[0-9a-f]{24}', token)
            assert token not in seen, 'pagination made no progress'
            seen.add(token)
    raise AssertionError('pagination did not finish')


class ReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_reported_case_no_older_turns_large_single_message(self):
        text = 'BEGIN' + ('x'*350000) + 'THE_REAL_END'
        fake = FakeClient([message('t1',text)])
        pages, got = await collect(fake,max_chars=120000)
        self.assertGreater(len(pages),2)
        self.assertTrue(pages[0]['character_truncated'])
        self.assertFalse(pages[0]['has_older_turns'])
        self.assertIsNotNone(pages[0]['next_page_token'])
        self.assertEqual(got.text('t1','i-t1'), text)
        self.assertTrue(pages[-1]['page_complete'])
    async def test_message_no_old_12000_character_clip(self):
        text = 'x'*16000+'SENTINEL'
        pages, got = await collect(FakeClient([message('t',text)]),max_chars=120000)
        self.assertEqual(len(pages),1)
        self.assertEqual(got.text('t','i-t'),text)
    async def test_user_message_no_old_8000_character_clip(self):
        text='u'*28000+'END_USER'
        pages, got = await collect(FakeClient([message('t',text,role='userMessage')]),max_chars=9000)
        self.assertEqual(got.text('t','i-t'),text)
    async def test_multipart_user_text(self):
        turn=message('t','',role='userMessage')
        turn['items'][0]['content']=[{'type':'text','text':'a'*13000},{'type':'text','text':'b'*9000}]
        _,got=await collect(FakeClient([turn]),max_chars=8000)
        self.assertEqual(got.text('t','i-t'),'a'*13000+'\n'+'b'*9000)
    async def test_older_turn_windows_and_inner_pages_no_gaps(self):
        originals={f't{i}':f'TURN{i}'+str(i)*2300+'END' for i in range(11)}
        pages,got=await collect(FakeClient([message(k,v) for k,v in originals.items()]),max_turns=3,max_chars=4000)
        self.assertTrue(any(p['continuation_reason']=='older_turns' for p in pages))
        self.assertTrue(any(p['continuation_reason']=='same_turn_page' for p in pages))
        self.assertEqual(got.turn_ids,set(originals))
        for k,v in originals.items(): self.assertEqual(got.text(k,'i-'+k),v)
    async def test_many_short_items_exhaust_metadata_budget(self):
        t=message('t','')
        t['items']=[{'id':str(i),'type':'agentMessage','text':'x'} for i in range(170)]
        pages,got=await collect(FakeClient([t],item_page_size=17),max_chars=2000)
        self.assertGreater(len(pages),10)
        self.assertEqual(len(got.fields),170)
        for i in range(170): self.assertEqual(got.text('t',str(i),item_index=i),'x')
    async def test_unicode_escaping_and_empty_text(self):
        data='"\\\n\r\t中文🙂e\u0301\u0000'*1800
        t=message('unicode',data)
        t['items'] += [{'id':'empty','type':'agentMessage','text':''}]
        _,got=await collect(FakeClient([t]),max_chars=3001)
        self.assertEqual(got.text('unicode','i-unicode'),data)
        self.assertEqual(got.text('unicode','empty',item_index=1),'')
    async def test_large_tool_fields_and_json_compound_fields(self):
        args={'nested':['a'*28000,{'q':'\\"\n🙂'*3000}]}
        output='OUTPUT'*8000
        diff='@@ PATCH @@\n'*4000
        turn={'id':'t','status':'completed','items':[
            {'type':'mcpToolCall','id':'m','server':'test','tool':'inspect','status':'completed','arguments':args,'result':{'output':output},'error':None},
            {'type':'fileChange','id':'f','status':'completed','changes':[{'path':'E:/p/a.py','kind':{'type':'update'},'diff':diff}]}
        ]}
        _,got=await collect(FakeClient([turn]),max_chars=10000,include_tool_output=True,include_diffs=True)
        self.assertEqual(json.loads(got.text('t','m',field='arguments')),args)
        self.assertEqual(json.loads(got.text('t','m',field='result')),{'output':output})
        self.assertEqual(json.loads(got.text('t','f',field='changes',item_index=1))[0]['diff'],diff)
    async def test_default_privacy_and_reasoning_always_excluded(self):
        secret='DO_NOT_SURFACE_739'
        turn={'id':'t','items':[
          {'type':'reasoning','id':'r','text':secret,'summary':[secret]},
          {'type':'commandExecution','id':'c','command':'pytest','aggregatedOutput':secret},
          {'type':'mcpToolCall','id':'m','tool':'x','arguments':secret,'result':secret},
          {'type':'fileChange','id':'f','changes':[{'path':'a','diff':secret}]},
          {'type':'unknownFuture','id':'u','text':secret},
        ]}
        pages,_=await collect(FakeClient([turn]),max_chars=10000)
        self.assertNotIn(secret,json.dumps(pages))
        all_pages,_=await collect(FakeClient([{'id':'t','items':[turn['items'][0]]}]),include_tool_output=True,include_diffs=True)
        self.assertNotIn(secret,json.dumps(all_pages))
    async def test_turn_error_is_pageable(self):
        t=message('t','ok')
        t['error']={'message':'error '*10000}
        _,got=await collect(FakeClient([t]),max_chars=8000)
        self.assertEqual(json.loads(got.text('t',None,field='error')),t['error'])
        self.assertEqual(got.text('t','i-t',item_index=1),'ok')
    async def test_legacy_turn_api_continues_older_and_chars(self):
        turns=[message(f't{i}',str(i)*4500) for i in range(7)]
        fake=FakeClient(turns,mode='legacy')
        pages,got=await collect(fake,max_turns=2,max_chars=4000)
        self.assertEqual(got.turn_ids,{t['id'] for t in turns})
        self.assertTrue(all('legacy full items' in p['history_source'] for p in pages))
        self.assertFalse(any(m=='thread/read' and p.get('includeTurns') for m,p in fake.calls))
        for t in turns: self.assertEqual(got.text(t['id'],'i-'+t['id']),t['items'][0]['text'])
    async def test_legacy_whole_read_fallback_paginates_both_limits(self):
        turns=[message(f't{i}',str(i)*4500) for i in range(6)]
        fake=FakeClient(turns,mode='legacy',turns_supported=False)
        pages,got=await collect(fake,max_turns=2,max_chars=4000)
        self.assertEqual(got.turn_ids,{t['id'] for t in turns})
        self.assertTrue(all('whole history loaded' in p['history_source'] for p in pages))
        for t in turns: self.assertEqual(got.text(t['id'],'i-'+t['id']),t['items'][0]['text'])
    async def test_empty_session_and_reasoning_only_turns_finish(self):
        for turns in ([],[{'id':'t','items':[]}],[{'id':'t','items':[{'type':'reasoning','text':'private'}]}]):
            pages,_=await collect(FakeClient(turns))
            self.assertEqual(len(pages),1)
            self.assertFalse(pages[0]['has_more_content'])
    async def test_resume_replay_is_identical(self):
        fake=FakeClient([message('t','abc'*5000)])
        with patch.object(bridge,'CodexAppServerClient',fake):
            first=await bridge.read_session(thread_id=THREAD,max_chars=3000)
            a=await bridge.read_session(thread_id=THREAD,max_chars=3000,page_token=first['next_page_token'])
            b=await bridge.read_session(thread_id=THREAD,max_chars=3000,page_token=first['next_page_token'])
        self.assertEqual(a,b)
    async def test_change_character_budget_mid_read(self):
        fake=FakeClient([message('t','y'*17000)])
        got=Assembler()
        with patch.object(bridge,'CodexAppServerClient',fake):
            first=await bridge.read_session(thread_id=THREAD,max_chars=2000,max_turns=7)
            got.add(first)
            second=await bridge.read_session(thread_id=THREAD,max_chars=120000,max_turns=100,page_token=first['next_page_token'])
            got.add(second)
        self.assertEqual(second['output_info']['max_turns'],7)
        self.assertFalse(second['has_more_content'])
        self.assertEqual(got.text('t','i-t'),'y'*17000)
    async def test_content_mutation_rejected(self):
        fake=FakeClient([message('t','x'*10000)])
        with patch.object(bridge,'CodexAppServerClient',fake):
            first=await bridge.read_session(thread_id=THREAD,max_chars=2000)
            fake.turns[0]['items'][0]['text']='CHANGED'+fake.turns[0]['items'][0]['text']
            with self.assertRaisesRegex(bridge.CodexBridgeError,'changed'):
                await bridge.read_session(thread_id=THREAD,page_token=first['next_page_token'])
    async def test_new_turn_shift_rejected_in_paged_first_window(self):
        fake=FakeClient([message('t','x'*10000)])
        with patch.object(bridge,'CodexAppServerClient',fake):
            first=await bridge.read_session(thread_id=THREAD,max_chars=2000)
            fake.turns.append(message('new','later'))
            with self.assertRaisesRegex(bridge.CodexBridgeError,'changed'):
                await bridge.read_session(thread_id=THREAD,page_token=first['next_page_token'])
    async def test_broken_paged_api_does_not_fall_back_to_full_history(self):
        fake=FakeClient([message('t','x')],turns_supported=False)
        with patch.object(bridge,'CodexAppServerClient',fake):
            with self.assertRaises(bridge.CodexAppServerError): await bridge._read_session_app_server(thread_id=THREAD)
        self.assertFalse(any(m=='thread/read' and p.get('includeTurns') for m,p in fake.calls))
    async def test_wrong_session_token_rejected_before_access(self):
        fake=FakeClient([message('t','x'*10000)])
        with patch.object(bridge,'CodexAppServerClient',fake):
            first=await bridge.read_session(thread_id=THREAD,max_chars=2000)
            count=len(fake.calls)
            with self.assertRaisesRegex(ValueError,'different Codex session'):
                await bridge.read_session(thread_id='other',page_token=first['next_page_token'])
            self.assertEqual(len(fake.calls),count)
    async def test_changed_privacy_options_rejected(self):
        fake=FakeClient([message('t','x'*10000)])
        with patch.object(bridge,'CodexAppServerClient',fake):
            first=await bridge.read_session(thread_id=THREAD,max_chars=2000)
            with self.assertRaisesRegex(ValueError,'unchanged'):
                await bridge.read_session(thread_id=THREAD,include_tool_output=True,page_token=first['next_page_token'])
    async def test_token_does_not_contain_visible_transcript(self):
        text='PRIVATE_VISIBLE_TEST_SENTINEL'*1500
        fake=FakeClient([message('t',text)])
        with patch.object(bridge,'CodexAppServerClient',fake):
            first=await bridge.read_session(thread_id=THREAD,max_chars=2000)
        decoded=bridge._unpack_read_token(first['next_page_token'],THREAD,False,False)
        self.assertNotIn('PRIVATE_VISIBLE_TEST_SENTINEL',json.dumps(decoded))
    async def test_varied_boundaries_deterministic_random_fixture(self):
        rng=random.Random(237)
        turns=[]
        originals={}
        for i in range(28):
            t={'id':f't{i}','items':[],'status':'completed'}
            for j in range(rng.randrange(1,5)):
                txt=''.join(rng.choices('ab\\\n\t"中🙂',k=rng.randrange(0,6000)))
                t['items'].append({'id':f'i{j}','type':'agentMessage','text':txt})
                originals[(t['id'],f'i{j}',j)]=txt
            turns.append(t)
        for budget in (2000,4093,19001,120000):
            with self.subTest(max_chars=budget):
                _,got=await collect(FakeClient(turns,item_page_size=2),max_chars=budget,max_turns=5)
                self.assertEqual(len(got.fields),len(originals))
                for (t,i,j),txt in originals.items(): self.assertEqual(got.text(t,i,item_index=j),txt)
    async def test_non_array_turn_response_fails_not_empty(self):
        class Bad(FakeClient):
            async def request(self,method,params=None,**kw):
                if method=='thread/turns/list': return {'unexpected':[]}
                return await super().request(method,params,**kw)
        with patch.object(bridge,'CodexAppServerClient',Bad([message('t','x')])):
            with self.assertRaisesRegex(bridge.CodexBridgeError,'data array'):
                await bridge.read_session(thread_id=THREAD)
    async def test_empty_item_page_with_cursor_not_skipped(self):
        class Empty(FakeClient):
            async def request(self,method,params=None,**kw):
                if method=='thread/items/list':
                    if 'cursor' not in params: return {'data':[],'nextCursor':'1'}
                    return {'data':[{'turnId':'t','item':message('t','after empty')['items'][0]}],'nextCursor':None}
                return await super().request(method,params,**kw)
        _,got=await collect(Empty([message('t','ignored')]))
        self.assertEqual(got.text('t','i-t'),'after empty')
    async def test_repeated_item_cursor_errors(self):
        class Loop(FakeClient):
            async def request(self,method,params=None,**kw):
                if method=='thread/items/list': return {'data':[],'nextCursor':'repeat'}
                return await super().request(method,params,**kw)
        with patch.object(bridge,'CodexAppServerClient',Loop([message('t','x')])):
            with self.assertRaisesRegex(bridge.CodexBridgeError,'repeated an item cursor'):
                await bridge.read_session(thread_id=THREAD)
    async def test_missing_turn_id_errors(self):
        fake=FakeClient([{'status':'completed','items':[]}])
        with patch.object(bridge,'CodexAppServerClient',fake):
            with self.assertRaisesRegex(bridge.CodexBridgeError,'lacks an ID'):
                await bridge.read_session(thread_id=THREAD)


class TokenTests(unittest.TestCase):
    def state(self):
        return {'v':2,'kind':'read','thread_id':THREAD,'backend':'paginated','limit':100,'cursor':'{"opaque":"a\\\\b"}', 'legacy_end':None,'position':bridge._position(),'fingerprint':None,'options':{'tool_output':False,'diffs':False}}
    def test_roundtrip_json_looking_cursor(self):
        s=self.state(); token=bridge._pack_read_token(s)
        self.assertEqual(bridge._unpack_read_token(token,THREAD,False,False),s)
    def test_old_token_rejected_clearly(self):
        with self.assertRaisesRegex(ValueError,'pre-0.2.3'): bridge._unpack_read_token('cb1_abc',THREAD,False,False)
    def test_corrupt_and_wrong_type_token_rejected(self):
        token=bridge._pack_read_token(self.state())
        for wrong in (None,{},7,'junk','cb2_foo.baz',token[:-1]+('0' if token[-1]!='0' else '1'),'cb2_'+'x'*40000):
            with self.subTest(wrong_type=type(wrong).__name__):
                with self.assertRaises(ValueError): bridge._unpack_read_token(wrong,THREAD,False,False)
    def test_negative_and_boolean_offsets_rejected(self):
        for n in (-1,True,3.5,'4'):
            s=self.state(); s['position']['item']=n
            with self.assertRaises(ValueError): bridge._unpack_read_token(bridge._pack_read_token(s),THREAD,False,False)
    def test_nonzero_offset_needs_fingerprint(self):
        s=self.state(); s['position']['text']=1
        with self.assertRaises(ValueError): bridge._unpack_read_token(bridge._pack_read_token(s),THREAD,False,False)
    def test_reject_unexpected_backend(self):
        s=self.state(); s['backend']='execute'
        with self.assertRaises(ValueError): bridge._unpack_read_token(bridge._pack_read_token(s),THREAD,False,False)
    def test_renderer_boundary_inside_one_field(self):
        visible=[{'id':'t','status':None,'items':[{'type':'agentMessage','id':'i','text':'x'*10000}]}]
        output,nextpos=bridge._render_visible_page(visible,max_chars=2000,position=bridge._position())
        self.assertIsNotNone(nextpos)
        self.assertEqual(nextpos['text'],len(output[0]['items'][0]['fragments'][0]['text']))
    def test_nontext_attachments_are_markers_not_inline_data(self):
        s=bridge._visible_user_content([{'type':'image','url':'data:image/png;base64,PRIVATE_PIXELS'},{'type':'localImage','path':'E:/a.png'},{'type':'text','text':'caption'}])
        self.assertNotIn('PRIVATE_PIXELS',s)
        self.assertIn('E:/a.png',s)
        self.assertIn('caption',s)

if __name__=='__main__': unittest.main()
