import json, sys, urllib.request, concurrent.futures as cf
raw, out = sys.argv[1], sys.argv[2]
d = json.load(open(raw, encoding='utf-8'))['data']
dic = d['regions_dictionary']
items = []
def walk(n, parent, depth):
    r = dic.get(str(n['id'])) or {}
    vd = r.get('visualization_data') or {}
    items.append(dict(id=n['id'], name=n['name'], parent=parent, depth=depth,
                      children=[c['id'] for c in n['children']],
                      color=vd.get('color'), mesh=(vd.get('_3d') or {}).get('mesh_file')))
    for c in n['children']:
        walk(c, n['id'], depth + 1)
for n in d['regions_hierarchy']:
    walk(n, None, 0)
items.append(dict(id=0, name='brain outline', parent=None, depth=-1, children=[], color='#cccccc', mesh='/media/Brains/Outline/Outline_new.stl'))
def head(it):
    if not it['mesh']: return it, None
    req = urllib.request.Request('https://api.mapzebrain.org' + urllib.request.quote(it['mesh'], safe='/%'), method='HEAD')
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return it, int(r.headers.get('Content-Length', 0))
    except Exception as e:
        print('ERR', e, it['mesh']); return it, None
with cf.ThreadPoolExecutor(8) as ex:
    for it, size in ex.map(head, items):
        it['bytes'] = size
json.dump(items, open(out, 'w', encoding='utf-8'), indent=1)
tot = sum(i['bytes'] or 0 for i in items)
print(len(items), 'items, total MB', round(tot/1e6, 1))
for i in sorted(items, key=lambda i: -(i['bytes'] or 0))[:8]: print(round((i['bytes'] or 0)/1e6,1), i['name'])
