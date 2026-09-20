import argparse
import asyncio
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import random
import sys
import tempfile
import tracemalloc
import unittest
from unittest.mock import AsyncMock, patch

import codex_bridge as bridge
import rollout_fallback as rf
import verify_pagination as verify

TID = '11111111-2222-4333-8444-555555555555'
OTHER = '66666666-7777-4888-8999-aaaaaaaaaaaa'


def record(top, **payload):
    return {'timestamp': '2026-09-16T12:00:00Z', 'type': top, 'payload': payload}


def message(text, role='assistant', **extras):
    return record('response_item', type='message', role=role,
                  content=[{'type': 'input_text' if role == 'user' else 'output_text', 'text': text}], **extras)


class Fixture(unittest.TestCase):
    def setUp(self):
        rf.clear_token_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {'CODEX_HOME': str(self.root)})
        self.env.start()
        self.path = self.root / 'sessions' / '2026' / '09' / '16' / ('rollout-2026-09-16T00-00-00-' + TID + '.jsonl')
        self.path.parent.mkdir(parents=True)

    def tearDown(self):
        rf.clear_token_cache()
        self.env.stop()
        self.tmp.cleanup()

    def write(self, rows, *, bom=False, newline=True, meta=None):
        rows = [record('session_meta', id=TID, history_mode='paginated', **(meta or {})), *rows]
        data = '\n'.join(json.dumps(x, ensure_ascii=False) for x in rows) + ('\n' if newline else '')
        self.path.write_bytes((b'\xef\xbb\xbf' if bom else b'') + data.encode('utf-8'))

    def read(self, **kw):
        params = dict(thread_id=TID, max_turns=100, max_chars=120000, include_tool_output=False, include_diffs=False)
        params.update(kw)
        return rf.read_page(**params)

    def collect(self, **kw):
        pages = []
        fields = {}
        token = None
        seen = set()
        for _ in range(2000):
            r = self.read(page_token=token, **kw)
            pages.append(r)
            self.assertEqual(r['has_more_content'], bool(r['next_page_token']))
            self.assertLessEqual(r['output_info']['payload_chars'], kw.get('max_chars', 120000))
            self.assertEqual(r['output_info']['payload_chars'], len(rf._wire(r['turns'])))
            if r['character_truncated']:
                self.assertTrue(r['has_more_content'])
            for t in r['turns']:
                for i in t['items']:
                    for f in i['fragments']:
                        key = (t['id'], i['id'], f['field'])
                        old = fields.get(key, '')
                        self.assertEqual(f['offset'], len(old))
                        self.assertEqual(f['end_offset'], f['offset'] + len(f['text']))
                        self.assertEqual(f['complete'], f['end_offset'] == f['total_chars'])
                        fields[key] = old + f['text']
            token = r['next_page_token']
            if token is None:
                break
            self.assertNotIn(token, seen)
            seen.add(token)
        self.assertIsNone(token)
        return pages, fields


class RolloutTests(Fixture):
    def test_large_message_within_single_record_continues_exactly(self):
        text = 'BEGIN' + 'abc☃😀e\u0301"\\\n'*8000 + 'END'
        self.write([message(text)])
        pages, fields = self.collect(max_chars=5000)
        self.assertGreater(len(pages), 5)
        self.assertEqual(list(fields.values()), [text])
        self.assertTrue(pages[-1]['coverage_complete'])
        self.assertEqual(pages[0]['continuation_reason'], 'same_rollout_record')
        self.assertFalse(pages[0]['has_older_turns'])

    def test_multiple_records_cross_character_and_group_budgets(self):
        texts = ['start-'+str(i)+' '+('data' * 200)+'-end' for i in range(23)]
        self.write([message(t) for t in texts])
        pages, fields = self.collect(max_chars=4000, max_turns=2)
        self.assertEqual(list(fields.values()), texts)
        self.assertGreater(len(pages), 10)

    def test_record_support_complete_only_decides_at_eof(self):
        self.write([message('x' * 10000)])
        first = self.read(max_chars=2000)
        self.assertIsNone(first['record_support_complete'])
        self.assertIsNone(first['coverage_report']['record_support_complete'])
        pages, _ = self.collect(max_chars=2000)
        self.assertTrue(pages[-1]['record_support_complete'])
        self.assertTrue(pages[-1]['coverage_report']['record_support_complete'])

    def test_record_support_false_at_eof_for_unknown_record(self):
        self.write([record('event_msg', type='future_record')])
        result = self.read()
        self.assertFalse(result['record_support_complete'])
        self.assertFalse(result['coverage_report']['record_support_complete'])

    def test_native_completed_items_pascal_case(self):
        self.write([
            record('event_msg', type='item_completed', turn_id='t1', item={'type':'UserMessage','id':'u','content':[{'type':'text','text':'question'}]}),
            record('event_msg', type='item_completed', turn_id='t1', item={'type':'AgentMessage','id':'a','content':[{'type':'Text','text':'answer'}]}),
        ])
        pages, fields = self.collect()
        self.assertEqual(list(fields.values()), ['question', 'answer'])
        self.assertTrue(pages[0]['turns'][0]['items'][0]['possible_duplicate_representation'])
        self.assertEqual(pages[0]['turns'][1]['items'][0]['source_turn_id'], 't1')

    def test_legacy_duplicate_representations_are_labelled_not_silently_removed(self):
        self.write([message('repeat'), record('event_msg', type='agent_message', message='repeat')])
        pages, fields = self.collect()
        self.assertEqual(list(fields.values()), ['repeat','repeat'])
        self.assertTrue(pages[0]['turns'][1]['items'][0]['possible_duplicate_representation'])
        self.assertIn('repeat', pages[0]['warnings'][0])

    def test_reasoning_private_context_and_roles_never_returned(self):
        secret = 'PRIVATE_REASONING_CANARY'
        self.write([
            record('response_item', type='reasoning', summary=[{'text':secret}], encrypted_content=secret),
            record('event_msg', type='agent_reasoning', text=secret),
            record('event_msg', type='item_completed', item={'type':'Reasoning','summary_text':[secret],'raw_content':[secret]}),
            message(secret, role='developer'), message(secret, role='system'),
            message(secret, channel='analysis'), message(secret, phase='reasoning'),
            record('compacted', message=secret, replacement_history=[message(secret)]),
            record('turn_context', developer_instructions=secret),
            message('VISIBLE'),
        ])
        pages, _ = self.collect(include_tool_output=True, include_diffs=True)
        self.assertNotIn(secret, json.dumps(pages))
        self.assertIn('VISIBLE', json.dumps(pages))

    def test_tool_output_and_patch_payloads_opt_in_independently(self):
        self.write([
            record('response_item', type='function_call', name='exec_command', arguments='TOOL_CANARY'),
            record('response_item', type='custom_tool_call', name='apply_patch', input='DIFF_CANARY'),
            record('event_msg', type='dynamic_tool_call_request', tool='apply_patch', arguments='DIFF3_CANARY'),
            record('event_msg', type='item_completed', item={'type':'FileChange','changes':{'x':{'diff':'DIFF2_CANARY'}}}),
        ])
        data = json.dumps(self.collect()[0])
        for text in ('TOOL_CANARY', 'DIFF_CANARY', 'DIFF2_CANARY', 'DIFF3_CANARY'):
            self.assertNotIn(text, data)
        data = json.dumps(self.collect(include_tool_output=True)[0])
        self.assertIn('TOOL_CANARY', data)
        self.assertNotIn('DIFF_CANARY', data)
        self.assertNotIn('DIFF2_CANARY', data)
        self.assertNotIn('DIFF3_CANARY', data)
        data = json.dumps(self.collect(include_diffs=True)[0])
        self.assertNotIn('TOOL_CANARY', data)
        self.assertIn('DIFF_CANARY', data)
        self.assertIn('DIFF2_CANARY', data)
        self.assertIn('DIFF3_CANARY', data)

    def test_opted_in_nested_binary_and_reasoning_are_filtered(self):
        self.write([record('event_msg', type='item_completed', item={'type':'McpToolCall','tool':'test',
            'result': {'content':[{'type':'image','data':'BINARY_CANARY'}, {'type':'text','text':'public'}],
                       'reasoning': 'HIDDEN_CANARY','nested': {'b64_json':'BINARY2_CANARY','data':'Q'*20000}, 'other': {'type':'reasoning_summary','text':'HIDDEN2_CANARY'}}})])
        data = json.dumps(self.collect(include_tool_output=True)[0])
        self.assertIn('public', data)
        for word in ('BINARY_CANARY', 'BINARY2_CANARY', 'HIDDEN_CANARY', 'HIDDEN2_CANARY','Q'*1000):
            self.assertNotIn(word,data)

    def test_embedded_base64_uri_crosses_chunk_boundaries(self):
        prefix = 'text ' + 'q'*(rf.CHUNK-20)
        text = prefix + ' data:image/png;base64,' + 'ABCD'*20000 + '! done'
        self.write([message(text)])
        _, fields = self.collect(max_chars=20000)
        self.assertEqual(list(fields.values()), [prefix+' '+rf.BINARY_MARKER+'! done'])

    def test_empty_text_is_valid_complete_field(self):
        self.write([message('')])
        pages, fields = self.collect(max_chars=2000)
        self.assertEqual(list(fields.values()), [''])
        self.assertTrue(pages[-1]['coverage_complete'])

    def test_bom_crlf_no_final_newline(self):
        self.write([message('hello')], bom=True, newline=False)
        self.path.write_bytes(self.path.read_bytes().replace(b'\n', b'\r\n'))
        self.assertEqual(list(self.collect()[1].values()), ['hello'])

    def test_malformed_tail_returns_error_not_eof_success(self):
        self.write([message('before')])
        with self.path.open('ab') as f:
            f.write(b'{"type":"response_item","payload":{"secret":"PRIVATE_CANARY')
        with self.assertRaises(rf.RolloutReadError) as raised:
            self.collect()
        self.assertNotIn('PRIVATE_CANARY', str(raised.exception))
        self.assertIn('Unterminated',str(raised.exception))

    def test_unknown_record_coverage_flag_persists_across_pages(self):
        self.write([record('new_future_record', type='future', secret='NOT_VISIBLE'), message('known')])
        pages, _ = self.collect(max_turns=1)
        self.assertFalse(pages[-1]['coverage_complete'])
        self.assertEqual(pages[-1]['fallback_info']['unsupported_records_seen'],1)
        self.assertNotIn('NOT_VISIBLE',json.dumps(pages))

    def test_rollback_cannot_be_misreported_as_active_history(self):
        self.write([message('old'), record('event_msg', type='thread_rolled_back', num_turns=1), message('new')])
        pages, _ = self.collect(max_turns=1)
        self.assertFalse(pages[-1]['coverage_complete'])
        self.assertTrue(pages[-1]['fallback_info']['rollback_seen'])

    def test_completion_marker_preserves_stored_failure(self):
        self.write([record('event_msg', type='task_complete', last_agent_message='stored final', error={'message':'command failed'})])
        _, fields = self.collect()
        self.assertIn('stored final',fields.values())
        self.assertIn('command failed',fields.values())

    def test_file_change_between_pages_rejected(self):
        self.write([message('x'*10000)])
        token = self.read(max_chars=2000)['next_page_token']
        with self.path.open('ab') as f:
            f.write(b'\n')
        with self.assertRaisesRegex(rf.RolloutReadError,'changed'):
            self.read(page_token=token,max_chars=2000)

    def test_file_replacement_with_same_size_and_mtime_rejected(self):
        self.write([message('x'*10000)])
        before = self.path.stat()
        token = self.read(max_chars=2000)['next_page_token']
        replacement = self.path.with_suffix('.tmp')
        replacement.write_bytes(self.path.read_bytes().replace(b'xxx',b'yyy'))
        os.utime(replacement, ns=(before.st_atime_ns,before.st_mtime_ns))
        replacement.replace(self.path)
        with self.assertRaisesRegex(rf.RolloutReadError,'changed'):
            self.read(page_token=token)

    def test_token_replay_after_restart_and_changed_page_budget(self):
        self.write([message('hello'*10000)])
        first = self.read(max_chars=2000)
        second = self.read(page_token=first['next_page_token'],max_chars=7000)
        replay = self.read(page_token=first['next_page_token'],max_chars=7000)
        self.assertEqual(second, replay)
        self.assertEqual(second['turns'][0]['items'][0]['fragments'][0]['offset'], first['turns'][0]['items'][0]['fragments'][0]['end_offset'])
        self.assertEqual(second['output_info']['max_turns'],100)

    def test_token_is_short_safe_ascii_and_contains_no_text(self):
        self.write([message('PRIVATE_CANARY'*1000)])
        token = self.read(max_chars=2000)['next_page_token']
        self.assertRegex(token,r'^cbr4_[0-9a-f]{40}$')
        self.assertLessEqual(len(token), 48)
        self.assertNotIn('PRIVATE_CANARY',token)
        self.assertNotIn(str(self.root),token)

    def test_process_restart_or_expiry_requires_clean_restart(self):
        self.write([message('x'*10000)])
        token = self.read(max_chars=2000)['next_page_token']
        rf.clear_token_cache()
        with self.assertRaisesRegex(rf.RolloutReadError,'expired|restarted'):
            self.read(page_token=token)

    def test_incidental_outer_whitespace_is_ignored(self):
        self.write([message('x'*10000)])
        first = self.read(max_chars=2000)
        second = self.read(max_chars=2000, page_token='  ' + first['next_page_token'] + '  ')
        self.assertTrue(second['turns'])

    def test_bad_token_session_flags_checksum_and_offsets(self):
        self.write([message('x'*10000)])
        token = self.read(max_chars=2000)['next_page_token']
        with self.assertRaises(rf.RolloutReadError):
            self.read(page_token=token[:-1] + ('0' if token[-1]!='0' else '1'))
        with self.assertRaises(rf.RolloutReadError):
            self.read(page_token=token,include_tool_output=True)
        with self.assertRaises(rf.RolloutReadError):
            self.read(page_token=token,thread_id=OTHER)
        state = rf.unpack(token,TID,{'tool_output':False,'diffs':False})
        state['record'] += 1
        with self.assertRaisesRegex(rf.RolloutReadError,'boundary'):
            self.read(page_token=rf.pack(state))
        state = rf.unpack(token,TID,{'tool_output':False,'diffs':False})
        state['text'] = 1000000000
        with self.assertRaisesRegex(rf.RolloutReadError,'offset'):
            self.read(page_token=rf.pack(state))

    def test_session_id_header_mismatch(self):
        self.write([message('wrong')])
        self.path.write_text(self.path.read_text().replace(TID,OTHER),encoding='utf-8')
        with self.assertRaisesRegex(rf.RolloutReadError,'does not match'):
            self.read()

    def test_directory_traversal_not_accepted(self):
        with self.assertRaisesRegex(rf.RolloutReadError,'UUID'):
            self.read(thread_id='../../auth')

    def test_symlinked_rollout_file_rejected(self):
        self.write([message('forbidden')])
        outside = self.root / 'outside.jsonl'
        self.path.rename(outside)
        try:
            self.path.symlink_to(outside)
        except OSError:
            self.skipTest('Symlink creation unavailable')
        with self.assertRaises(rf.RolloutReadError):
            self.read()

    def test_symlinked_session_directory_rejected(self):
        self.write([message('forbidden')])
        old = self.root/'sessions'
        outside = self.root/'outside_sessions'
        old.rename(outside)
        try:
            old.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('Symlink creation unavailable')
        with self.assertRaises(rf.RolloutReadError):
            self.read()

    def test_archived_rollout_supported(self):
        self.write([message('archived')])
        target = self.root/'archived_sessions'/self.path.name
        target.parent.mkdir()
        self.path.rename(target)
        self.assertEqual(list(self.collect()[1].values()),['archived'])

    def test_two_matching_rollouts_rejected(self):
        self.write([message('first')])
        other = self.root/'archived_sessions'/self.path.name
        other.parent.mkdir()
        other.write_bytes(self.path.read_bytes())
        with self.assertRaisesRegex(rf.RolloutReadError,'unique'):
            self.read()

    def test_compressed_only_rollout_explicit_error(self):
        self.write([message('not decompressed')])
        self.path.rename(self.path.with_suffix('.jsonl.zst'))
        with self.assertRaisesRegex(rf.RolloutReadError,'compressed'):
            self.read()

    def test_referenced_parent_not_silently_omitted_or_followed(self):
        self.write([message('child')],meta={'forked_from_rollout_id':OTHER})
        with self.assertRaisesRegex(rf.RolloutReadError,'parent'):
            self.read()

    def test_bounded_metadata_only_scan_continues(self):
        self.write([record('turn_context', model='irrelevant') for _ in range(10)] + [message('found')])
        with patch.object(rf,'MAX_SCAN_RECORDS',3):
            pages, fields = self.collect()
        self.assertGreater(len(pages),3)
        self.assertEqual(list(fields.values()),['found'])
        self.assertTrue(pages[0]['has_more_content'])

    def test_no_history_file_writes_or_cache(self):
        self.write([message('x'*6000)])
        before = {p.relative_to(self.root):(p.stat().st_size,p.stat().st_mtime_ns,hashlib.sha256(p.read_bytes()).hexdigest()) for p in self.root.rglob('*') if p.is_file()}
        self.collect(max_chars=2000)
        after = {p.relative_to(self.root):(p.stat().st_size,p.stat().st_mtime_ns,hashlib.sha256(p.read_bytes()).hexdigest()) for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before,after)

    def test_24mib_inline_image_read_has_bounded_python_memory(self):
        # Build the oversized record without materializing its 24 MiB string.
        with self.path.open('wb') as f:
            f.write((json.dumps(record('session_meta',id=TID))+'\n').encode())
            f.write(b'{"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"visible before"},{"type":"input_image","image_url":"data:image/png;base64,')
            for _ in range(384):
                f.write(b'A'*65536)
            f.write(b'"},{"type":"input_text","text":"visible after"}]}}\n')
        tracemalloc.start()
        try:
            page = self.read()
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        data = json.dumps(page)
        self.assertIn('visible before',data)
        self.assertIn('visible after',data)
        self.assertNotIn('A'*1000,data)
        self.assertLess(peak, 6*1024*1024)
        self.assertTrue(page['coverage_complete'])
        print(f'OVERSIZED_IMAGE_PYTHON_PEAK_BYTES={peak}', file=sys.stderr)

    def test_indented_records_continue_at_real_line_boundaries(self):
        self.write([message('x'*10000),message('tail')])
        lines=self.path.read_bytes().splitlines()
        self.path.write_bytes(b'\n'.join([lines[0],b'  '+lines[1],b'',b'\t'+lines[2]])+b'\n')
        _,fields=self.collect(max_chars=2000)
        self.assertEqual(list(fields.values()),['x'*10000,'tail'])

    def test_unknown_content_block_qualifies_coverage(self):
        self.write([record('response_item',type='message',role='assistant',content=[{'type':'future_text','text':'NOT_READ'}])])
        pages,_=self.collect()
        self.assertFalse(pages[-1]['coverage_complete'])
        self.assertNotIn('NOT_READ',json.dumps(pages))

    def test_local_multimillion_character_message_exact_reassembly(self):
        original='HEAD'+('abcdefg'*360000)+'TAIL'
        self.write([message(original)])
        pages,fields=self.collect()
        self.assertGreater(len(pages),20)
        self.assertEqual(list(fields.values()),[original])

    def test_json_opt_in_field_fragment_reconstruction(self):
        value = {'hello': '\"\\\n\u2603'*3000,'number':1.5,'bool':True,'null':None}
        self.write([record('event_msg',type='item_completed',item={'type':'McpToolCall','tool':'demo','result':value})])
        _, fields = self.collect(max_chars=4000,include_tool_output=True)
        got = next(value for key,value in fields.items() if key[2]=='result')
        self.assertEqual(json.loads(got),value)



class ParserTests(unittest.TestCase):
    def parse(self,data,chunk=7):
        with patch.object(rf,'CHUNK',chunk):
            f=io.BytesIO(data)
            node=rf.Scanner(f,len(data)).record()[2]
            return json.loads(''.join(rf.json_chunks(node,io.BytesIO(data))))

    def test_randomized_json_and_tiny_buffers(self):
        rng=random.Random(1234)
        alphabet='xyz☃😀\u0301"\\\n\r\t/'
        for _ in range(100):
            value={'text':''.join(rng.choices(alphabet,k=200)),'array':[True,None,1,-2,1.5]}
            raw=(json.dumps(value,ensure_ascii=rng.choice([True,False]))+'\n').encode()
            self.assertEqual(self.parse(raw),value)

    def test_json_invalid_syntax_and_unicode_never_succeeds(self):
        bad=[b'{"x":"\xff"}\n',b'{"x":"\\uD800"}\n',b'{"x":"\\uDC00"}\n',b'{"x":"\\q"}\n',b'{"x":01}\n',b'{"x":truefalse}\n',b'{"x":1,}\n',b'{"x":1,"x":2}\n',b'{"x":"a\nb"}\n',b'{"x":1}garbage\n']
        for raw in bad:
            with self.subTest(raw=raw):
                with self.assertRaises(rf.RolloutReadError):
                    self.parse(raw)

    def test_depth_and_node_bounds_fail_explicitly(self):
        with patch.object(rf,'MAX_DEPTH',4):
            with self.assertRaises(rf.RolloutReadError):
                self.parse(b'{"x":[[[[[[]]]]]]}\n')
        with patch.object(rf,'MAX_RECORD_NODES',3):
            with self.assertRaises(rf.RolloutReadError):
                self.parse(b'{"x":[1,2,3,4]}\n')


class IntegrationTests(Fixture, unittest.IsolatedAsyncioTestCase):
    async def test_fallback_only_after_native_failure_then_tokens_bypass_native(self):
        self.write([message('fallback'*4000)])
        native=AsyncMock(side_effect=bridge.CodexAppServerError('Codex response exceeded the 16 MiB stream-line limit.'))
        with patch.object(bridge,'_read_session_app_server',native):
            first=await bridge.read_session(thread_id=TID,max_chars=2000)
            second=await bridge.read_session(thread_id=TID,max_chars=2000,page_token=first['next_page_token'])
        self.assertEqual(native.await_count,1)
        self.assertEqual(first['fallback_info']['trigger'],'stream_limit')
        self.assertEqual(second['history_source'],'local_rollout_fallback')
        self.assertEqual(first['bridge_version'],bridge.BRIDGE_VERSION)
        self.assertFalse(first['history_restart_required'])

    async def test_real_stdio_oversize_error_routes_to_local_recovery(self):
        self.write([message('real subprocess recovered '*500)])
        fixture=Path(__file__).with_name('mock_app_server.py')
        with patch.object(bridge,'build_codex_command',lambda *args:[sys.executable,str(fixture),'oversized']):
            first=await bridge.read_session(thread_id=TID,max_chars=5000)
        self.assertEqual(first['fallback_info']['trigger'],'stream_limit')
        self.assertTrue(first['has_more_content'])
        with patch.object(bridge,'build_codex_command',side_effect=AssertionError('No new App Server should be launched')):
            second=await bridge.read_session(thread_id=TID,max_chars=5000,page_token=first['next_page_token'])
        self.assertEqual(second['history_source'],'local_rollout_fallback')

    async def test_bridge_reports_expired_process_local_handle_as_restart(self):
        self.write([message('fallback' * 4000)])
        with patch.object(bridge,'_read_session_app_server',AsyncMock(side_effect=bridge.CodexAppServerError('Codex app-server exited or disconnected (return code: 1).'))):
            first = await bridge.read_session(thread_id=TID, max_chars=2000)
        rf.clear_token_cache()
        with self.assertRaisesRegex(bridge.CodexBridgeError, 'continuation failed.*expired|continuation failed.*restarted'):
            await bridge.read_session(thread_id=TID, max_chars=2000, page_token=first['next_page_token'])

    async def test_healthy_native_never_opens_rollout(self):
        with patch.object(bridge,'_read_session_app_server',AsyncMock(return_value={'sentinel':True})), patch.object(rf,'read_page') as local:
            result=await bridge.read_session(thread_id=TID)
        self.assertEqual(result,{'sentinel':True})
        local.assert_not_called()

    async def test_native_continuation_failure_explicitly_restarts(self):
        self.write([message('from beginning')])
        with patch.object(bridge,'_read_session_app_server',AsyncMock(side_effect=bridge.CodexAppServerError('timed out'))):
            result=await bridge.read_session(thread_id=TID,page_token='native token stub')
        self.assertTrue(result['history_restart_required'])
        self.assertTrue(any('discard' in x for x in result['warnings']))

    async def test_permission_denial_is_not_bypassed(self):
        for reason in ('Permission denied', 'Unauthorized', 'Forbidden', 'authentication required',
                       'Access is denied', 'not allowed', 'insufficient privileges'):
            with self.subTest(reason=reason):
                with patch.object(bridge,'_read_session_app_server',AsyncMock(side_effect=bridge.CodexAppServerError(reason))), patch.object(rf,'read_page') as local:
                    with self.assertRaises(bridge.CodexAppServerError):
                        await bridge.read_session(thread_id=TID)
                    local.assert_not_called()

    async def test_coded_or_unclassified_app_server_error_is_not_bypassed(self):
        errors = (
            bridge.CodexAppServerError('request refused', code=-32001),
            bridge.CodexAppServerError('unclassified server failure'),
        )
        for error in errors:
            with self.subTest(error=str(error)):
                with patch.object(bridge,'_read_session_app_server',AsyncMock(side_effect=error)), patch.object(rf,'read_page') as local:
                    with self.assertRaises(bridge.CodexAppServerError):
                        await bridge.read_session(thread_id=TID)
                    local.assert_not_called()

    async def test_environment_opt_out(self):
        with patch.dict(os.environ,{'CODEX_HISTORY_ROLLOUT_FALLBACK':'0'}), patch.object(bridge,'_read_session_app_server',AsyncMock(side_effect=bridge.CodexAppServerError('error'))), patch.object(rf,'read_page') as local:
            with self.assertRaises(bridge.CodexAppServerError):
                await bridge.read_session(thread_id=TID)
            local.assert_not_called()

    async def test_missing_rollout_error_does_not_leak_original_tool_error(self):
        with patch.object(bridge,'_read_session_app_server',AsyncMock(side_effect=bridge.CodexAppServerError('Codex app-server exited or disconnected: PRIVATE_RAW_CANARY'))):
            with self.assertRaises(bridge.CodexBridgeError) as raised:
                await bridge.read_session(thread_id=TID)
        self.assertNotIn('PRIVATE_RAW_CANARY',str(raised.exception))

    async def test_verifier_output_contains_no_text_or_tokens_and_labels_source(self):
        self.write([message('SECRET_TRANSCRIPT_CANARY'*2000)])
        output=io.StringIO()
        with patch.object(bridge,'_read_session_app_server',AsyncMock(side_effect=bridge.CodexAppServerError('Codex app-server exited or disconnected (return code: 1).'))), contextlib.redirect_stdout(output):
            code=await verify.run(argparse.Namespace(thread_id=TID,max_pages=100,max_turns=100,max_chars=12000))
        self.assertEqual(code,0)
        self.assertIn('PASS_HISTORY_READ_FINISHED',output.getvalue())
        self.assertIn('local_rollout_fallback',output.getvalue())
        self.assertNotIn('SECRET_TRANSCRIPT_CANARY',output.getvalue())
        self.assertNotIn('cbr3_',output.getvalue())

    async def test_require_fallback_does_not_mistake_native_success_for_recovery(self):
        stub={'turns': [], 'has_more_content':False, 'next_page_token':None, 'character_truncated':False,
              'page_complete':True, 'has_older_turns':False,'continuation_reason':None,'turns_returned':0,
              'items_returned':0, 'chars_returned':0, 'output_info':{'payload_chars':2}, 'history_source':'App Server'}
        with patch.object(verify,'read_session',AsyncMock(return_value=stub)), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(bridge.CodexBridgeError,'FALLBACK_NOT_EXERCISED'):
                await verify.run(argparse.Namespace(thread_id=TID,max_pages=1,max_turns=100,max_chars=120000,require_fallback=True))

    async def test_verifier_does_not_call_unsupported_record_coverage_complete(self):
        self.write([record('unknown',type='unknown')])
        output=io.StringIO()
        with patch.object(bridge,'_read_session_app_server',AsyncMock(side_effect=bridge.CodexAppServerError('Codex app-server exited or disconnected (return code: 1).'))), contextlib.redirect_stdout(output):
            code=await verify.run(argparse.Namespace(thread_id=TID,max_pages=10,max_turns=100,max_chars=12000))
        self.assertEqual(code,0)
        self.assertIn('PASS_HISTORY_READ_FINISHED_WITH_COVERAGE_WARNINGS',output.getvalue())

if __name__ == '__main__':
    unittest.main()
