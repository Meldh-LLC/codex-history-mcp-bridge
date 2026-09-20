from __future__ import annotations
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock
import unittest

import rollout_fallback as rf
import codex_bridge as cb
import audit_rollout
from test_rollout_fallback import Fixture, record, message, TID


class Projection025Tests(Fixture):
    def text(self, **kwargs):
        pages, fields = self.collect(**kwargs)
        return pages, '\n'.join(fields.values())

    def test_71_known_collaboration_events_are_projected_not_relabelled_unknown(self):
        kinds = sorted(rf.COLLAB_TYPES)
        self.write([record('event_msg', type=kinds[i % len(kinds)], call_id=f'call-{i}', sender_thread_id=TID, receiver_thread_id=TID, status='running') for i in range(71)])
        pages, fields = self.collect(max_turns=7, max_chars=4000)
        self.assertGreater(len(pages), 10)
        end = pages[-1]
        self.assertEqual(end['coverage_report']['unknown_records'], 0)
        self.assertEqual(end['coverage_report']['records_accounted_for'], 72)
        self.assertTrue(end['projection_coverage_complete'])
        self.assertIsNone(end['semantic_coverage_complete'])
        self.assertEqual(sum(1 for key in fields if key[-1] == 'sender_thread_id'), 71)

    def test_collaboration_prompt_and_completed_answer_are_opt_in(self):
        self.write([record('event_msg', type='collab_agent_spawn_end', new_thread_id=TID, prompt='PROMPT_SECRET', status={'completed':'AGENT_ANSWER'})])
        _, default = self.text()
        self.assertIn(TID, default)
        self.assertNotIn('PROMPT_SECRET', default)
        self.assertNotIn('AGENT_ANSWER', default)
        self.assertIn('completed', default)
        _, detailed = self.text(include_tool_output=True)
        self.assertIn('PROMPT_SECRET', detailed)
        self.assertIn('AGENT_ANSWER', detailed)

    def test_status_maps_and_agent_status_lists_are_sanitized_by_flag(self):
        self.write([record('event_msg', type='collab_waiting_end', statuses={TID:{'completed':'DONE_SECRET'}}, agent_statuses=[{'thread_id':TID,'agent_nickname':'reviewer','status':{'errored':'ERROR_SECRET'}}])])
        _, default = self.text()
        self.assertNotIn('DONE_SECRET', default)
        self.assertNotIn('ERROR_SECRET', default)
        self.assertIn('reviewer', default)
        _, detailed = self.text(include_tool_output=True)
        self.assertIn('DONE_SECRET', detailed)
        self.assertIn('ERROR_SECRET', detailed)

    def test_inter_agent_communication_does_not_become_a_human_message(self):
        self.write([record('inter_agent_communication', author='/root/a', recipient='/root', content='AGENT_CONTENT', encrypted_content='NEVER_EXPOSE')])
        pages, text = self.text()
        self.assertEqual(pages[0]['turns'][0]['items'][0]['type'], 'collaborationEvent')
        self.assertNotIn('AGENT_CONTENT', text)
        _, text = self.text(include_tool_output=True)
        self.assertIn('AGENT_CONTENT', text)
        self.assertNotIn('NEVER_EXPOSE', text)
        self.assertFalse(pages[-1]['coverage_report']['linked_sessions_read'])

    def test_response_agent_message_with_recipient_is_inter_agent(self):
        self.write([record('response_item', type='agent_message', author='/root/a',recipient='/root',content=[{'type':'input_text','text':'AGENT_ONLY'}])])
        pages, text = self.text()
        self.assertEqual(pages[0]['turns'][0]['items'][0]['type'], 'collaborationEvent')
        self.assertNotIn('AGENT_ONLY', text)
        self.assertIn('AGENT_ONLY', self.text(include_tool_output=True)[1])

    def test_image_begin_and_end_preserve_status_path_failure_no_pixels(self):
        self.write([
            record('event_msg', type='image_generation_begin', call_id='image-1'),
            record('event_msg', type='image_generation_end', call_id='image-1', status='failed', saved_path='E:/output.png', revised_prompt='DRAWING_PROMPT', result='PIXEL_CANARY', failure={'code':'interrupted','message':'user stopped request','raw_response':'HIDDEN_RESPONSE'}),
        ])
        pages, text = self.text()
        self.assertEqual(pages[-1]['coverage_report']['unknown_records'],0)
        for expected in ('failed','E:/output.png','interrupted','user stopped request'):
            self.assertIn(expected,text)
        for secret in ('DRAWING_PROMPT','PIXEL_CANARY','HIDDEN_RESPONSE'):
            self.assertNotIn(secret,text)
        _, detailed = self.text(include_tool_output=True)
        self.assertIn('DRAWING_PROMPT', detailed)
        self.assertNotIn('PIXEL_CANARY', detailed)

    def test_paginated_image_extension_is_known_and_binary_result_stays_hidden(self):
        self.write([record('event_msg', type='item_completed', turn_id='t-image', item={
            'type':'Extension', 'id':'img-1', 'kind':'image_gen.generation',
            'status':'completed', 'result':'BASE64_PIXEL_CANARY',
            'revisedPrompt':'DRAW_A_BLUE_SQUARE', 'savedPath':'E:/generated.png',
            'transparentBackground':True,
        })])
        pages, text = self.text()
        report = pages[-1]['coverage_report']
        self.assertEqual(report['unknown_records'], 0)
        self.assertTrue(report['projection_coverage_complete'])
        self.assertIn('image_gen.generation', text)
        self.assertIn('completed', text)
        self.assertIn('E:/generated.png', text)
        self.assertIn('true', text.lower())
        self.assertNotIn('BASE64_PIXEL_CANARY', text)
        self.assertNotIn('DRAW_A_BLUE_SQUARE', text)
        self.assertIn('image_audio_binary', report['intentionally_omitted_data'])
        _, detailed = self.text(include_tool_output=True)
        self.assertIn('DRAW_A_BLUE_SQUARE', detailed)
        self.assertNotIn('BASE64_PIXEL_CANARY', detailed)

    def test_future_extension_kind_stays_unknown(self):
        self.write([record('event_msg', type='item_completed', item={
            'type':'Extension', 'kind':'future.extension', 'status':'completed',
            'result':'NO_LEAK',
        })])
        pages, text = self.text(include_tool_output=True)
        self.assertFalse(pages[-1]['projection_coverage_complete'])
        self.assertIn('event_msg.item_completed.Extension', pages[-1]['coverage_report']['unknown_type_counts'])
        self.assertNotIn('NO_LEAK', text)

    def test_future_image_subtype_stays_unknown(self):
        self.write([record('event_msg',type='image_generation_future', text='NOT_AUTO_WHITELISTED')])
        pages, text = self.text()
        self.assertFalse(pages[-1]['projection_coverage_complete'])
        self.assertIn('event_msg.image_generation_future', pages[-1]['coverage_report']['unknown_type_counts'])
        self.assertNotIn('NOT_AUTO_WHITELISTED',text)

    def test_future_collab_subtype_stays_unknown(self):
        self.write([record('event_msg',type='collab_agent_future',prompt='NOT_KNOWN')])
        pages,_=self.text()
        self.assertEqual(pages[-1]['coverage_report']['unknown_records'],1)

    def test_unknown_family_with_known_subtype_is_not_whitelisted(self):
        self.write([record('new_family',type='image_generation_end',saved_path='NOT_AUTHORIZED')])
        pages,text=self.text()
        self.assertFalse(pages[-1]['projection_coverage_complete'])
        self.assertNotIn('NOT_AUTHORIZED',text)

    def test_task_started_default_collaboration_mode_is_known_metadata(self):
        self.write([record('event_msg', type='task_started', turn_id='t',
                           model_context_window=128000,
                           collaboration_mode_kind='default')])
        pages, text = self.text()
        report = pages[-1]['coverage_report']
        self.assertEqual(report['unknown_records'], 0)
        self.assertEqual(report['projection_gaps'], {})
        self.assertTrue(report['projection_coverage_complete'])
        self.assertEqual(report['known_control_type_counts']['event_msg.task_started'], 1)
        self.assertEqual(text, '')

    def test_completed_context_compaction_lifecycle_is_known_not_checkpoint_payload(self):
        self.write([record('event_msg', type='item_completed', turn_id='t',
                           item={'type':'ContextCompaction','id':'cc-1'})])
        pages, text = self.text()
        report = pages[-1]['coverage_report']
        self.assertEqual(report['unknown_records'], 0)
        self.assertTrue(report['projection_coverage_complete'])
        self.assertEqual(report['known_control_type_counts']['event_msg.item_completed.ContextCompaction'], 1)
        self.assertEqual(report['compaction_checkpoints'], 0)
        self.assertIn('Context-compaction lifecycle marker', text)

    def test_known_control_records_count_without_text_rows(self):
        self.write([record('event_msg',type='turn_started',turn_id='t'),record('event_msg',type='token_count',info={'total_tokens':2}),message('hello')])
        pages,_=self.text()
        report=pages[-1]['coverage_report']
        self.assertEqual(report['known_control_records'],2)
        self.assertEqual(report['records_accounted_for'],4)
        self.assertEqual(pages[-1]['items_returned'],1)
        self.assertTrue(report['projection_coverage_complete'])

    def test_known_control_with_unclassified_text_still_blocks_coverage(self):
        self.write([record('event_msg',type='turn_started',turn_id='t',surprise_message='HIDDEN_UNSUPPORTED')])
        pages,text=self.text()
        self.assertFalse(pages[-1]['projection_coverage_complete'])
        self.assertIn('field.surprise_message',pages[-1]['coverage_report']['unknown_type_counts'])
        self.assertNotIn('HIDDEN_UNSUPPORTED',text)

    def test_started_item_is_projected_with_lifecycle_not_assumed_completed(self):
        self.write([record('event_msg',type='item_started',turn_id='t',item={'type':'ImageGeneration','id':'i','status':'in_progress','result':'PIXELS'})])
        pages,text=self.text()
        row=pages[0]['turns'][0]['items'][0]
        self.assertEqual(row['lifecycle'],'started')
        self.assertTrue(row['possible_duplicate_representation'])
        self.assertNotIn('PIXELS',text)
        self.assertIn('in_progress',text)

    def test_unknown_inner_completed_type_is_reported(self):
        self.write([record('event_msg',type='item_completed',item={'type':'FutureTurnItem','text':'UNKNOWN'})])
        pages,text=self.text()
        self.assertIn('event_msg.item_completed.FutureTurnItem', pages[-1]['coverage_report']['unknown_type_counts'])
        self.assertNotIn('UNKNOWN',text)

    def test_compaction_snapshot_public_text_separate_from_original_actions(self):
        replacement=[{'type':'message','role':'user','content':[{'type':'input_text','text':'checkpoint question'}]}, {'type':'message','role':'assistant','content':[{'type':'output_text','text':'checkpoint answer'}]}]
        self.write([message('original action'),record('compacted',message='OPAQUE_SUMMARY',replacement_history=replacement),message('later action')])
        pages,fields=self.collect(max_chars=2500)
        text='\n'.join(fields.values())
        for word in ('original action','checkpoint question','checkpoint answer','later action'):
            self.assertIn(word,text)
        self.assertNotIn('OPAQUE_SUMMARY',text)
        self.assertTrue(any(key[-1]=='checkpoint[0].text' for key in fields))
        checkpoints=[i for p in pages for t in p['turns'] for i in t['items'] if i['type']=='contextCompaction']
        self.assertTrue(all(i['checkpoint_semantics']=='model_input_replacement_not_new_actions' for i in checkpoints))
        self.assertFalse(pages[-1]['coverage_report']['effective_context_reconstructed'])
        self.assertIsNone(pages[-1]['coverage_report']['semantic_coverage_complete'])
        self.assertTrue(pages[-1]['projection_coverage_complete'])

    def test_compaction_private_and_encrypted_content_remains_hidden(self):
        replacement=[
            {'type':'message','role':'system','content':[{'type':'input_text','text':'SYSTEM_PRIVATE'}]},
            {'type':'message','role':'developer','content':[{'type':'input_text','text':'DEVELOPER_PRIVATE'}]},
            {'type':'reasoning','summary':[{'text':'REASONING_PRIVATE'}]},
            {'type':'message','role':'assistant','channel':'analysis','content':[{'type':'output_text','text':'ANALYSIS_PRIVATE'}]},
            {'type':'message','role':'assistant','content':[{'type':'output_text','text':'PUBLIC'},{'type':'encrypted_content','encrypted_content':'ENCRYPTED_PRIVATE'}]},
        ]
        self.write([record('compacted',message='SUMMARY_PRIVATE',replacement_history=replacement,guardian_history={'secret':'GUARDIAN_PRIVATE'})])
        pages,text=self.text(include_tool_output=True,include_diffs=True)
        self.assertIn('PUBLIC',text)
        self.assertNotIn('_PRIVATE',text)
        self.assertIn('untyped_compaction_summary',pages[-1]['coverage_report']['intentionally_omitted_data'])

    def test_compaction_tool_and_patch_payload_flags_are_independent(self):
        self.write([record('compacted',message='',replacement_history=[
            {'type':'function_call','name':'exec_command','arguments':'TOOL_VALUE'},
            {'type':'custom_tool_call','name':'apply_patch','input':'DIFF_VALUE'},
        ])])
        _,text=self.text(include_tool_output=True)
        self.assertIn('TOOL_VALUE',text); self.assertNotIn('DIFF_VALUE',text)
        _,text=self.text(include_diffs=True)
        self.assertNotIn('TOOL_VALUE',text); self.assertIn('DIFF_VALUE',text)

    def test_compaction_large_text_exact_continuation_and_single_counter(self):
        text='START'+ ('☃😀a\\"\n' * 17000) +'END'
        self.write([record('compacted',message='',replacement_history=[{'type':'message','role':'assistant','content':[{'type':'output_text','text':text}]}])])
        pages,fields=self.collect(max_chars=5000)
        values=[v for key,v in fields.items() if key[-1]=='checkpoint[0].text']
        self.assertEqual(values,[text])
        self.assertGreater(len(pages),20)
        self.assertEqual(pages[-1]['coverage_report']['compaction_checkpoints'],1)
        self.assertEqual(pages[-1]['coverage_report']['records_accounted_for'],2)

    def test_compaction_unknown_inner_item_persists_warning(self):
        self.write([record('compacted',message='',replacement_history=[{'type':'new_response_type','secret':'NO_LEAK'},{'type':'message','role':'assistant','content':[{'type':'output_text','text':'x'*20000}]}]),message('end')])
        pages,text=self.text(max_chars=3000)
        report=pages[-1]['coverage_report']
        self.assertEqual(report['unknown_records'],1)
        self.assertEqual(report['unknown_type_counts']['checkpoint.response_item.new_response_type'],1)
        self.assertFalse(report['projection_coverage_complete'])
        self.assertNotIn('NO_LEAK',text)

    def test_compaction_without_replacement_is_known_gap_not_green_check(self):
        self.write([record('compacted',message='OPAQUE_SUMMARY'),message('after')])
        pages,text=self.text(max_turns=1)
        report=pages[-1]['coverage_report']
        self.assertEqual(report['unknown_records'],0)
        self.assertIn('unreadable_compaction_checkpoint',report['projection_gaps'])
        self.assertFalse(report['projection_coverage_complete'])
        self.assertNotIn('OPAQUE_SUMMARY',text)

    def test_bad_checkpoint_container_is_not_silently_empty(self):
        self.write([record('compacted',message='',replacement_history={'weird':'NO_LEAK'})])
        pages,text=self.text()
        self.assertFalse(pages[-1]['projection_coverage_complete'])
        self.assertIn('unhandled_checkpoint_shape',pages[-1]['coverage_report']['projection_gaps'])
        self.assertNotIn('NO_LEAK',text)

    def test_failure_abort_reason_is_recovered(self):
        self.write([record('event_msg',type='turn_aborted',reason='interrupted')])
        pages,text=self.text()
        self.assertIn('interrupted',text)
        self.assertEqual(pages[0]['turns'][0]['items'][0]['type'],'failureMarker')

    def test_warning_plan_and_review_fields_are_visible(self):
        self.write([record('event_msg',type='plan_update',explanation='next step',plan=[{'step':'write spec','status':'pending'}]),record('event_msg',type='stream_error',message='network interrupted',additional_details='retrying'),record('event_msg',type='exited_review_mode',review_output={'overall_explanation':'not tested'})])
        pages,text=self.text()
        for expected in ('next step','write spec','network interrupted','retrying','not tested'):
            self.assertIn(expected,text)
        self.assertEqual(pages[-1]['coverage_report']['unknown_records'],0)

    def test_mcp_nested_invocation_and_result_opt_in(self):
        self.write([record('event_msg',type='mcp_tool_call_end',invocation={'server':'docs','tool':'read','arguments':{'query':'ARG_CANARY'}},result={'text':'RESULT_CANARY'})])
        _,default=self.text()
        self.assertIn('docs',default); self.assertIn('read',default)
        self.assertNotIn('ARG_CANARY',default); self.assertNotIn('RESULT_CANARY',default)
        _,detail=self.text(include_tool_output=True)
        self.assertIn('ARG_CANARY',detail); self.assertIn('RESULT_CANARY',detail)

    def test_hook_prompt_never_leaks_with_tool_flag(self):
        self.write([record('event_msg',type='hook_completed',status='failed',status_message='timeout',prompt='PRIVATE_HOOK',instructions='PRIVATE_INSTRUCTIONS')])
        _,text=self.text(include_tool_output=True)
        self.assertIn('timeout',text); self.assertNotIn('PRIVATE_',text)

    def test_byte_command_delta_is_named_deliberate_omission(self):
        self.write([record('event_msg',type='exec_command_output_delta',call_id='exec-1',stream='stdout',chunk='UkFXX0NBTkFSWQ==')])
        pages,text=self.text(include_tool_output=True)
        self.assertNotIn('UkFXX0NBTkFSWQ==',text)
        self.assertIn('binary_command_stream',pages[-1]['coverage_report']['intentionally_omitted_data'])
        self.assertEqual(pages[-1]['coverage_report']['unknown_records'],0)

    def test_unknown_histogram_bound_does_not_drop_unknown_total(self):
        self.write([record('event_msg',type=f'new_type_{i}',secret='NO_LEAK') for i in range(75)])
        pages,text=self.text(max_turns=2,max_chars=3000)
        report=pages[-1]['coverage_report']
        self.assertEqual(report['unknown_records'],75)
        self.assertEqual(len(report['unknown_type_counts']),rf.TYPE_BUCKET_LIMIT)
        self.assertEqual(report['unknown_type_inventory_overflow'],75-rf.TYPE_BUCKET_LIMIT)
        self.assertFalse(report['projection_coverage_complete'])
        self.assertNotIn('NO_LEAK',text)

    def test_stat_identity_ignores_only_ctime_discrepancy(self):
        self.write([message('hello')])
        actual=self.path.stat()
        attrs={key:getattr(actual,key) for key in ('st_mode','st_size','st_mtime_ns','st_ctime_ns','st_dev','st_ino')}
        changed={**attrs,'st_ctime_ns':attrs['st_ctime_ns']+2823715500}
        self.assertEqual(rf.stat_identity(SimpleNamespace(**attrs)),rf.stat_identity(SimpleNamespace(**changed)))
        for key in ('st_size','st_mtime_ns','st_dev','st_ino'):
            with self.subTest(key=key):
                altered={**attrs,key:attrs[key]+1}
                self.assertNotEqual(rf.stat_identity(SimpleNamespace(**attrs)),rf.stat_identity(SimpleNamespace(**altered)))

    def test_windows_fstat_path_ctime_discrepancy_does_not_break_real_read(self):
        self.write([message('hello'*10000)])
        real_fstat=os.fstat
        def different_ctime(fd):
            actual=real_fstat(fd)
            attrs={key:getattr(actual,key) for key in ('st_mode','st_size','st_mtime_ns','st_ctime_ns','st_dev','st_ino')}
            attrs['st_ctime_ns']+=2823715500
            return SimpleNamespace(**attrs)
        with patch.object(rf.os,'fstat',side_effect=different_ctime):
            pages,fields=self.collect(max_chars=3000)
        self.assertEqual(list(fields.values()),['hello'*10000])
        self.assertTrue(pages[-1]['projection_coverage_complete'])

    def test_bad_stats_in_token_rejected(self):
        self.write([message('x'*10000)])
        first=self.read(max_chars=3000)
        state=rf.unpack(first['next_page_token'],TID,{'tool_output':False,'diffs':False})
        state['stats']['records']+=1
        with self.assertRaisesRegex(rf.RolloutReadError,'Inconsistent'):
            self.read(page_token=rf.pack(state))

    def test_unknown_content_after_many_continuations_counts_once(self):
        self.write([record('response_item',type='message',role='assistant',content=[{'type':'output_text','text':'x'*20000},{'type':'future_block','secret':'NO_LEAK'}])])
        pages,text=self.text(max_chars=2000)
        self.assertEqual(pages[-1]['coverage_report']['unknown_records'],1)
        self.assertEqual(pages[-1]['coverage_report']['unknown_type_counts']['content.future_block'],1)
        self.assertNotIn('NO_LEAK',text)

    def test_audit_is_type_inventory_not_text_retrieval_proof(self):
        self.write([record('event_msg',type='future_one',private_data='DO_NOT_PRINT'),message('PRIVATE_TRANSCRIPT')])
        result=audit_rollout.audit(TID)
        encoded=json.dumps(result)
        self.assertEqual(result['result'],'AUDIT_FINISHED')
        self.assertNotIn('DO_NOT_PRINT',encoded); self.assertNotIn('PRIVATE_TRANSCRIPT',encoded)
        self.assertIn('private_data',encoded)
        self.assertIsNone(result['coverage_report']['projection_coverage_complete'])
        self.assertFalse(result['coverage_report']['record_support_complete'])
        self.assertFalse(result['text_retrieval_tested'])

    def test_audit_supports_the_three_live_rollout_variants(self):
        self.write([
            record('event_msg', type='task_started', turn_id='t', collaboration_mode_kind='default'),
            record('event_msg', type='item_completed', turn_id='t', item={'type':'ContextCompaction','id':'cc-1'}),
            record('event_msg', type='item_completed', turn_id='t', item={
                'type':'Extension','id':'img-1','kind':'image_gen.generation',
                'status':'completed','result':'PIXELS','savedPath':'E:/x.png'}),
        ])
        result = audit_rollout.audit(TID)
        report = result['coverage_report']
        self.assertEqual(report['unknown_records'], 0)
        self.assertTrue(report['record_support_complete'])
        self.assertEqual(result['diagnostic_examples'], {})

    def test_audit_reports_remaining_unknown_type_names(self):
        self.write([record('event_msg',type='future_one'),record('event_msg',type='future_one'),record('event_msg',type='future_two')])
        result=audit_rollout.audit(TID)
        self.assertEqual(result['counts_by_record_type']['event_msg.future_one'],2)
        self.assertEqual(result['coverage_report']['unknown_records'],3)
        self.assertTrue(all(not e['values_exposed'] for e in result['diagnostic_examples'].values()))

    def test_audit_stop_limit_not_eof(self):
        self.write([message('one'),message('two')])
        result=audit_rollout.audit(TID,max_records=1)
        self.assertEqual(result['result'],'AUDIT_STOPPED_AT_LIMIT')
        self.assertFalse(result['coverage_report']['traversal_complete'])
        self.assertEqual(result['coverage_report']['records_accounted_for'],1)

    def test_audit_checks_ownership_and_no_history_writes(self):
        self.write([message('a')])
        before=self.path.read_bytes()
        names_before=set(self.root.rglob('*'))
        audit_rollout.audit(TID)
        self.assertEqual(before,self.path.read_bytes())
        self.assertEqual(names_before,set(self.root.rglob('*')))
        self.path.write_bytes(before.replace(TID.encode(),b'11111111-1111-1111-1111-111111111111',1))
        with self.assertRaisesRegex(rf.RolloutReadError,'ownership'):
            audit_rollout.audit(TID)

    def test_realtime_uninterpreted_record_never_claims_semantic_coverage(self):
        self.write([record('realtime_item',type='new_audio',audio='BASE64_CANARY')])
        pages,text=self.text()
        self.assertFalse(pages[-1]['projection_coverage_complete'])
        self.assertIsNone(pages[-1]['semantic_coverage_complete'])
        self.assertNotIn('BASE64_CANARY',text)

    def test_rollback_log_projection_not_active_context_verification(self):
        self.write([message('old'),record('event_msg',type='thread_rolled_back',num_turns=1),message('new')])
        pages,text=self.text()
        self.assertIn('old',text);self.assertIn('new',text)
        self.assertTrue(pages[-1]['projection_coverage_complete'])
        self.assertFalse(pages[-1]['coverage_complete'])
        self.assertFalse(pages[-1]['coverage_report']['effective_context_reconstructed'])


class TokenMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_token_is_rejected_without_app_server_or_filesystem(self):
        with patch.object(cb,'_read_session_app_server',new_callable=AsyncMock) as native,patch.object(rf,'read_page') as fallback:
            with self.assertRaisesRegex(cb.CodexBridgeError,cb.BRIDGE_VERSION.replace('.',r'\.')):
                await cb.read_session(thread_id=TID,page_token='cbr3_old')
            native.assert_not_called();fallback.assert_not_called()

class GenericToolJsonTests(Fixture):
    def test_numeric_type_and_role_in_arbitrary_tool_result_are_data(self):
        value={'type':42,'role':7,'nested':{'type':{'label':'thing'},'value':'VISIBLE'}}
        self.write([record('response_item',type='function_call_output',output=value)])
        pages,fields=self.collect(include_tool_output=True)
        self.assertIn('VISIBLE','\n'.join(fields.values()))
        self.assertTrue(pages[-1]['projection_coverage_complete'])

    def test_private_role_objects_are_sanitized_inside_tool_output(self):
        value={'documents':[{'role':'system','content':'PRIVATE_PROMPT'},{'role':'developer','content':'PRIVATE_DEV'},{'role':'assistant','channel':'analysis','content':'PRIVATE_ANALYSIS'},{'type':'text','text':'VISIBLE'}]}
        self.write([record('response_item',type='function_call_output',output=value)])
        _,fields=self.collect(include_tool_output=True)
        text='\n'.join(fields.values())
        self.assertIn('VISIBLE',text)
        self.assertNotIn('PRIVATE_',text)
