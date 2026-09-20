import argparse
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch
import codex_bridge as b
import verify_pagination as verify
from test_pagination import FakeClient, message, collect, THREAD

class MorePaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_multimillion_character_message_reassembled(self):
        text='HEAD'+('abcdefghij'*250000)+'TAIL'
        pages,got=await collect(FakeClient([message('t',text)]),max_chars=120000)
        self.assertGreater(len(pages),20)
        self.assertEqual(got.text('t','i-t'),text)
    async def test_more_than_100_turns_no_tail_dropped(self):
        originals=[message(f't{i}',f'item-{i}') for i in range(137)]
        pages,got=await collect(FakeClient(originals),max_turns=100,max_chars=120000)
        self.assertTrue(pages[0]['page_complete'])
        self.assertTrue(pages[0]['has_older_turns'])
        self.assertTrue(pages[0]['has_more_content'])
        self.assertEqual(len(got.turn_ids),137)
        for i in range(137): self.assertEqual(got.text(f't{i}',f'i-t{i}'),f'item-{i}')
    async def test_command_text_and_output_are_not_clipped(self):
        cmd='command'*2200
        out='output'*8500
        t={'id':'t','items':[{'type':'commandExecution','id':'c','command':cmd,'aggregatedOutput':out,'cwd':'E:/test','exitCode':0,'status':'completed'}]}
        _,got=await collect(FakeClient([t]),max_chars=10000,include_tool_output=True)
        self.assertEqual(got.text('t','c',field='command'),cmd)
        self.assertEqual(got.text('t','c',field='output'),out)
        self.assertEqual(json.loads(got.text('t','c',field='exit_code')),0)
    async def test_exact_rendered_budget_end_has_no_spurious_token(self):
        v=[{'id':'t','status':None,'items':[{'id':'i','type':'agentMessage','text':'x'*1800}]}]
        whole,pos=b._render_visible_page(v,max_chars=10000,position=b._position())
        size=len(b._json_text(whole))
        actual,pos=b._render_visible_page(v,max_chars=size,position=b._position())
        self.assertIsNone(pos)
        self.assertEqual(whole,actual)
    async def test_smoke_script_never_prints_message_or_token(self):
        fake=FakeClient([message('t','PRIVATE_MESSAGE_TEXT'*3000)])
        output=io.StringIO()
        with patch.object(b,'CodexAppServerClient',fake), contextlib.redirect_stdout(output):
            result=await verify.run(argparse.Namespace(thread_id=THREAD,max_pages=2,max_chars=2000,max_turns=100))
        self.assertEqual(result,0)
        text=output.getvalue()
        self.assertNotIn('PRIVATE_MESSAGE_TEXT',text)
        self.assertNotIn('cb2_',text)
        self.assertIn('PASS_PAGES_TESTED_MORE_AVAILABLE',text)
    async def test_smoke_script_finishes_and_checks_offsets(self):
        fake=FakeClient([message('t','x'*3000)])
        output=io.StringIO()
        with patch.object(b,'CodexAppServerClient',fake), contextlib.redirect_stdout(output):
            result=await verify.run(argparse.Namespace(thread_id=THREAD,max_pages=10,max_chars=2000,max_turns=100))
        self.assertEqual(result,0)
        self.assertIn('PASS_HISTORY_READ_FINISHED',output.getvalue())

    async def test_empty_or_wrong_type_token_cannot_silently_restart(self):
        fake=FakeClient([message('t','x')])
        with patch.object(b,'CodexAppServerClient',fake):
            for token in ('',{},[],False,0):
                with self.subTest(token_type=type(token).__name__):
                    with self.assertRaises(ValueError):
                        await b.read_session(thread_id=THREAD,page_token=token)
        self.assertEqual(fake.calls,[])
    async def test_string_privacy_flags_cannot_enable_output(self):
        fake=FakeClient([message('t','x')])
        with patch.object(b,'CodexAppServerClient',fake):
            for flags in ({'include_tool_output':'false'},{'include_diffs':1}):
                with self.assertRaisesRegex(ValueError,'booleans'):
                    await b.read_session(thread_id=THREAD,**flags)
        self.assertEqual(fake.calls,[])

    async def test_native_search_snippets_remove_inline_binary_data(self):
        class SearchClient:
            async def __aenter__(self): return self
            async def __aexit__(self,*args): return None
            async def request(self,method,params,timeout_seconds=None):
                if method=='thread/search':
                    return {'data':[{'thread':{'id':'s','name':'Visible','cwd':'E:/synthetic',
                                              'preview':'PRIVATE_PREVIEW data:image/png;base64,PREVIEW_CANARY'},
                                     'snippet':'PRIVATE_SEARCH_CANARY data:image/png;base64,RESULT_CANARY'}]}
                if method=='thread/searchOccurrences':
                    return {'data':[{'turnId':'t','itemId':'i',
                                     'snippet':'PRIVATE_SEARCH_CANARY data:image/png;base64,OCCURRENCE_CANARY'}]}
                raise AssertionError(method)
        with patch.object(b,'CodexAppServerClient',SearchClient):
            result=await b.search_history(query='caption')
        encoded=json.dumps(result)
        self.assertIn('source item privacy could not be verified',encoded)
        for canary in ('PRIVATE_SEARCH_CANARY','PRIVATE_PREVIEW','PREVIEW_CANARY',
                       'RESULT_CANARY','OCCURRENCE_CANARY'):
            self.assertNotIn(canary,encoded)


def import_server_with_sdk_stub():
    # This verifies wrapper behavior/registration only, NOT real MCP SDK transport.
    parent=types.ModuleType('mcp'); mod=types.ModuleType('mcp.server'); typ=types.ModuleType('mcp.types')
    class Stub:
        def __init__(self,*args,**kw): self.tools={}
        def tool(self,**kw):
            def register(fn): self.tools[fn.__name__]=(fn,kw); return fn
            return register
    mod.MCPServer=Stub
    typ.ToolAnnotations=lambda **kwargs: kwargs
    path=Path(__file__).resolve().parents[1]/'server.py'
    spec=importlib.util.spec_from_file_location('offline_test_server',path)
    result=importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules,{'mcp':parent,'mcp.server':mod,'mcp.types':typ}), patch('logging.basicConfig'):
        spec.loader.exec_module(result)
    return result

class ServerWrapperTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self): self.server=import_server_with_sdk_stub()
    async def test_eleven_read_only_tools_and_continuation_instructions(self):
        self.assertEqual(len(self.server.mcp.tools),11)
        for fn,kw in self.server.mcp.tools.values(): self.assertTrue(kw['annotations']['read_only_hint'])
        fn,_=self.server.mcp.tools['codex_read_session']
        self.assertIn('has_more_content=true',fn.__doc__)
        self.assertIn('has_older_turns=false',fn.__doc__)
    async def test_known_error_has_actionable_structured_result(self):
        async def fail(**kwargs): raise b.CodexBridgeError('The selected history page changed')
        r=await self.server._call_history(fail)
        self.assertTrue(r['is_error'])
        self.assertFalse(r['history_complete'])
        self.assertIn('changed',r['error']['message'])
        self.assertEqual(r['bridge_version'],b.BRIDGE_VERSION)
    async def test_known_key_patterns_redacted_in_tool_errors(self):
        async def fail(**kwargs): raise b.CodexBridgeError('sk-abcdefgh12345678 Bearer abcdefghijklmnop')
        r=await self.server._call_history(fail)
        self.assertNotIn('abcdefgh',r['error']['message'])
    async def test_read_wrapper_forwards_token_and_privacy_arguments(self):
        captured={}
        async def read(**kwargs): captured.update(kwargs); return {'sentinel':'ok'}
        with patch.object(self.server,'read_session',read):
            result=await self.server.codex_read_session(thread_id=THREAD,page_token='example',include_diffs=True)
        self.assertEqual(result,{'sentinel':'ok'})
        self.assertEqual(captured['page_token'],'example')
        self.assertTrue(captured['include_diffs'])
        self.assertEqual(captured['max_chars'],120000)

class CurrentThreadItemProjectionTests(unittest.TestCase):
    def test_native_private_message_phases_and_roles_are_excluded(self):
        private = (
            {"type": "agentMessage", "phase": "analysis", "text": "PRIVATE"},
            {"type": "agentMessage", "phase": "reasoning", "text": "PRIVATE"},
            {"type": "agentMessage", "role": "developer", "text": "PRIVATE"},
            {"type": "agentMessage", "channel": "analysis", "text": "PRIVATE"},
        )
        for item in private:
            self.assertIsNone(b._project_item(item, include_tool_output=True, include_diffs=True))
        for phase in (None, "commentary", "final"):
            item = {"type": "agentMessage", "phase": phase, "text": "PUBLIC"}
            self.assertEqual(b._project_item(item, include_tool_output=False, include_diffs=False)["text"], "PUBLIC")

    def test_native_collaboration_status_payload_requires_tool_output(self):
        item = {"type": "collabAgentToolCall", "id": "c", "tool": "wait", "status": "completed",
                "agentStatus": {"completed": {"text": "STATUS_CANARY", "reasoning": "PRIVATE_CANARY"}}}
        default = b._project_item(item, include_tool_output=False, include_diffs=False)
        self.assertNotIn("STATUS_CANARY", json.dumps(default))
        self.assertIn("include_tool_output=true", json.dumps(default))
        opted_in = b._project_item(item, include_tool_output=True, include_diffs=False)
        self.assertIn("STATUS_CANARY", json.dumps(opted_in))
        self.assertNotIn("PRIVATE_CANARY", json.dumps(opted_in))

    def test_native_patch_payload_requires_diff_opt_in(self):
        for kind in ("mcpToolCall", "dynamicToolCall"):
            item = {"type": kind, "id": "p", "tool": "apply_patch", "arguments": "PATCH_CANARY",
                    "result": "PATCH_RESULT_CANARY", "contentItems": ["PATCH_CONTENT_CANARY"]}
            tool_only = b._project_item(item, include_tool_output=True, include_diffs=False)
            self.assertNotIn("PATCH_", json.dumps(tool_only))
            diff_only = b._project_item(item, include_tool_output=False, include_diffs=True)
            self.assertIn("PATCH_CANARY", json.dumps(diff_only))

    def test_native_opt_in_payloads_keep_private_and_binary_data_excluded(self):
        payload = {
            "content": [
                {"type": "image", "data": "IMAGE_CANARY"},
                {"type": "text", "text": "VISIBLE_TEXT"},
            ],
            "reasoning": "REASONING_CANARY",
            "nested": {"b64_json": "BINARY_CANARY", "data": "Q" * 20000},
            "private": {"role": "developer", "text": "DEVELOPER_CANARY"},
            "summary": {"type": "reasoning_summary", "text": "SUMMARY_CANARY"},
            "uri": "data:image/png;base64,DATA_URI_CANARY",
        }
        projected = b._project_item(
            {"type": "mcpToolCall", "id": "m", "tool": "inspect", "result": payload},
            include_tool_output=True,
            include_diffs=False,
        )
        text = json.dumps(projected)
        self.assertIn("VISIBLE_TEXT", text)
        self.assertIn("[binary/image payload omitted]", text)
        for canary in (
            "IMAGE_CANARY", "REASONING_CANARY", "BINARY_CANARY",
            "DEVELOPER_CANARY", "SUMMARY_CANARY", "DATA_URI_CANARY",
        ):
            self.assertNotIn(canary, text)

    def test_advanced_search_uses_the_native_message_privacy_boundary(self):
        private = {"type":"agentMessage","phase":"analysis","text":"PRIVATE_SEARCH_CANARY"}
        self.assertEqual(b._searchable_item_texts(private),[])

        visible = {"type":"agentMessage","phase":"final",
                   "text":"caption data:image/png;base64,SEARCH_BINARY_CANARY"}
        texts = b._searchable_item_texts(visible)
        self.assertEqual(texts[0][0],"assistant_message")
        self.assertIn("caption",texts[0][1])
        self.assertIn("[binary/image payload omitted]",texts[0][1])
        self.assertNotIn("SEARCH_BINARY_CANARY",texts[0][1])

        non_base64 = b._searchable_item_texts({
            "type":"agentMessage",
            "text":"caption data:image/svg+xml,<svg>NON_BASE64_CANARY</svg>",
        })
        self.assertIn("[binary/image payload omitted]",non_base64[0][1])
        self.assertNotIn("NON_BASE64_CANARY",non_base64[0][1])

        user = {"type":"userMessage","content":[
            {"type":"text","text":"ordinary visible text"},
            {"type":"image","url":"data:image/png;base64,USER_IMAGE_CANARY"},
        ]}
        user_texts = b._searchable_item_texts(user)
        self.assertIn("ordinary visible text",user_texts[0][1])
        self.assertNotIn("USER_IMAGE_CANARY",user_texts[0][1])

        summary = b._search_thread_summary({
            "id":"s", "name":"Visible", "cwd":"E:/synthetic",
            "preview":"PRIVATE_PREVIEW data:image/png;base64,PREVIEW_CANARY",
        })
        self.assertNotIn("preview",summary)
        self.assertNotIn("PRIVATE_PREVIEW",json.dumps(summary))

    def test_known_current_control_items_are_recognized_without_private_payloads(self):
        hook = b._project_item({"type":"hookPrompt","id":"h","fragments":[{"text":"SECRET"}]}, include_tool_output=False, include_diffs=False)
        self.assertEqual(hook["type"],"hookPrompt")
        self.assertNotIn("SECRET",json.dumps(hook))

        collab = b._project_item({"type":"collabAgentToolCall","id":"c","tool":"spawn_agent","status":"completed","prompt":"SECRET","senderThreadId":"s","receiverThreadIds":["r"]}, include_tool_output=False, include_diffs=False)
        self.assertEqual(collab["type"],"collabAgentToolCall")
        self.assertNotIn("SECRET",json.dumps(collab))

        image = b._project_item({"type":"imageGeneration","id":"i","status":"completed","result":"BASE64_SECRET","revisedPrompt":"PROMPT_SECRET","savedPath":"C:/safe.png","failure":None}, include_tool_output=False, include_diffs=False)
        self.assertEqual(image["status"],"completed")
        self.assertNotIn("BASE64_SECRET",json.dumps(image))
        self.assertNotIn("PROMPT_SECRET",json.dumps(image))
        image_opt = b._project_item({"type":"imageGeneration","id":"i","status":"completed","result":"BASE64_SECRET","revisedPrompt":"VISIBLE_PROMPT"}, include_tool_output=True, include_diffs=False)
        self.assertEqual(image_opt["revised_prompt"],"VISIBLE_PROMPT")
        self.assertNotIn("BASE64_SECRET",json.dumps(image_opt))

    def test_native_type_registry_covers_current_schema_variants(self):
        expected={"userMessage","hookPrompt","agentMessage","functionCallOutput","plan","commandExecution","fileChange","mcpToolCall","dynamicToolCall","collabAgentToolCall","subAgentActivity","webSearch","imageView","sleep","imageGeneration","enteredReviewMode","exitedReviewMode","contextCompaction"}
        self.assertTrue(expected.issubset(b.NATIVE_THREAD_ITEM_TYPES))


if __name__=='__main__': unittest.main()
