"""Synthetic App Server for resolver registration/transport tests only."""
import json
import sys

T={'id':'synthetic-live','sessionId':'synthetic-live','name':'Website 2','cwd':'/synthetic/project',
   'source':'vscode','threadSource':'user','createdAt':1000,'updatedAt':2000,'historyMode':'paginated','path':None}
for line in sys.stdin:
    q=json.loads(line);m=q.get('method');rid=q.get('id');p=q.get('params',{})
    if rid is None:continue
    if m=='initialize':out={'userAgent':'synthetic'}
    elif m=='thread/list':
        assert p['useStateDbOnly'] is True
        out={'data':[dict(T,id='synthetic-invalid'),T],'nextCursor':None}
    elif m=='thread/read':
        assert p['includeTurns'] is False
        if p['threadId']!='synthetic-live':
            print(json.dumps({'id':rid,'error':{'code':-32600,'message':'thread not loaded'}}),flush=True);continue
        out={'thread':T}
    elif m=='thread/turns/list':out={'data':[{'id':'t1','status':'completed','itemsView':'notLoaded'}],'nextCursor':None}
    elif m=='thread/items/list':out={'data':[{'turnId':'t1','item':{'type':'agentMessage','id':'i1','text':'x'*9000}}],'nextCursor':None}
    else:raise AssertionError(m)
    print(json.dumps({'id':rid,'result':out}),flush=True)
