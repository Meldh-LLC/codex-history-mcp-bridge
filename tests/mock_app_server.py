"""Synthetic stdio server fixture. This never reads real Codex data."""
import json
import sys

MODE = sys.argv[1] if len(sys.argv)>1 else 'normal'
THREAD = 'synthetic-stdio-session'
TEXT = 'START' + 'z'*400000 + 'END'

def send(obj):
    sys.stdout.write(json.dumps(obj,ensure_ascii=True,separators=(',',':'))+'\n')
    sys.stdout.flush()

for line in sys.stdin:
    request=json.loads(line)
    method=request.get('method')
    rid=request.get('id')
    if rid is None: continue
    if method=='initialize':
        if MODE=='init_error':
            send({'id':rid,'error':{'code':-32000,'message':'synthetic initialize failure'}})
        else:
            send({'id':rid,'result':{'userAgent':'offline-fixture'}})
    elif method=='thread/read':
        if MODE=='invalid_json':
            print('NOT JSON PRIVATE_PAYLOAD',flush=True)
        elif MODE=='oversized':
            send({'id':rid,'result':{'large':'x'*(17*1024*1024)}})
        elif MODE=='approval':
            send({'method':'item/commandExecution/requestApproval','id':'approval-1','params':{}})
            answer=json.loads(next(sys.stdin))
            assert answer['error']['code']==-32601
            send({'id':rid,'result':{'thread':{'id':THREAD,'historyMode':'legacy'}}})
        else:
            send({'id':rid,'result':{'thread':{'id':THREAD,'historyMode':'legacy'}}})
    elif method=='thread/turns/list':
        send({'id':rid,'result':{'data':[{'id':'turn-1','status':'completed','itemsView':'full','items':[{'type':'agentMessage','id':'item-1','text':TEXT}]}],'nextCursor':None}})
    else:
        send({'id':rid,'error':{'code':-32601,'message':'method not found'}})
