from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import codex_bridge as bridge

FIXTURE=Path(__file__).with_name('mock_app_server.py')

def command(mode='normal'):
    return lambda *args: [sys.executable,str(FIXTURE),mode]

class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_jsonl_response_and_real_subprocess_continuation(self):
        chunks=[]; token=None; pages=0
        with patch.object(bridge,'build_codex_command',command()):
            for _ in range(10):
                r=await bridge.read_session(thread_id='synthetic-stdio-session',page_token=token,max_chars=120000)
                pages+=1
                for turn in r['turns']:
                    for item in turn['items']:
                        chunks += [f['text'] for f in item['fragments'] if f['field']=='text']
                token=r['next_page_token']
                if token is None: break
        self.assertIsNone(token)
        self.assertGreater(pages,1)
        self.assertEqual(''.join(chunks),'START'+'z'*400000+'END')
        self.assertEqual(bridge.STREAM_LIMIT,16*1024*1024)
    async def test_transport_limit_is_actionable_not_silent(self):
        with patch.object(bridge,'build_codex_command',command('oversized')):
            with self.assertRaisesRegex(bridge.CodexAppServerError,'16 MiB'):
                async with bridge.CodexAppServerClient() as c:
                    await c.request('thread/read',{'threadId':'x'})
    async def test_init_failure_cleans_up_child(self):
        client=bridge.CodexAppServerClient()
        with patch.object(bridge,'build_codex_command',command('init_error')):
            with self.assertRaisesRegex(bridge.CodexAppServerError,'initialize failure'):
                async with client: pass
        self.assertIsNone(client.process)
        self.assertIsNone(client._stderr_task)
    async def test_server_approval_is_rejected(self):
        with patch.object(bridge,'build_codex_command',command('approval')):
            async with bridge.CodexAppServerClient() as c:
                result=await c.request('thread/read',{'threadId':'synthetic-stdio-session'})
        self.assertEqual(result['thread']['id'],'synthetic-stdio-session')
    async def test_invalid_wire_message_does_not_expose_raw_content(self):
        with patch.object(bridge,'build_codex_command',command('invalid_json')):
            with self.assertRaises(bridge.CodexAppServerError) as caught:
                async with bridge.CodexAppServerClient() as c:
                    await c.request('thread/read',{'threadId':'x'})
        self.assertNotIn('PRIVATE_PAYLOAD',str(caught.exception))
    async def test_write_rpc_is_rejected_before_launch(self):
        c=bridge.CodexAppServerClient()
        with self.assertRaisesRegex(bridge.CodexBridgeError,'refuses method'):
            await c.request('thread/start',{})
        self.assertIsNone(c.process)

if __name__=='__main__': unittest.main()
