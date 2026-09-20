"""Synthetic stdio fixture: an empty native index despite readable history."""
import json
import sys

ROWS = [
    {'id': 'synthetic-one', 'name': 'Notes', 'cwd': '/synthetic/primary'},
    {'id': 'synthetic-two', 'name': 'Notes', 'cwd': '/synthetic/primary'},
    {'id': 'synthetic-old', 'name': 'Older', 'cwd': '/synthetic/secondary'},
]
for t in ROWS:
    t.update({'sessionId': t['id'], 'source': 'vscode', 'threadSource': 'user',
              'createdAt': 1000, 'updatedAt': 2000, 'historyMode': 'paginated', 'path': None})
for line in sys.stdin:
    request = json.loads(line)
    method, rid, p = request.get('method'), request.get('id'), request.get('params', {})
    if rid is None:
        continue
    if method == 'initialize':
        out = {'userAgent': 'synthetic'}
    elif method == 'thread/list':
        assert p['useStateDbOnly'] is True
        selected = ROWS[2:] if p['archived'] else ROWS[:2]
        if p.get('cwd'):
            selected = [t for t in selected if t['cwd'] == p['cwd']]
        i = int(p.get('cursor', '0'))
        out = {'data': selected[i:i+1], 'nextCursor': str(i+1) if i+1 < len(selected) else None}
    elif method == 'thread/read':
        assert p['includeTurns'] is False
        out = {'thread': next(t for t in ROWS if t['id'] == p['threadId'])}
    elif method == 'thread/turns/list':
        out = {'data': [{'id': 'synthetic-turn', 'status': 'completed', 'itemsView': 'notLoaded'}], 'nextCursor': None}
    elif method == 'thread/items/list':
        out = {'data': [{'turnId': 'synthetic-turn', 'item': {'id': 'synthetic-message', 'type': 'agentMessage',
                'text': 'x'*1395 + 'ProjectSignal' + 'y'*2600}}], 'nextCursor': None}
    elif method in ('thread/search', 'thread/searchOccurrences'):
        out = {'data': [], 'nextCursor': None}
    else:
        raise AssertionError(method)
    print(json.dumps({'id': rid, 'result': out}), flush=True)
