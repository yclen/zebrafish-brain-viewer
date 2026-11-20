import json, sys, os, urllib.request, concurrent.futures as cf
items = json.load(open(sys.argv[1], encoding='utf-8')); outdir = sys.argv[2]
os.makedirs(outdir, exist_ok=True)
def get(it):
    if not it['mesh'] or not it['bytes']: return it['name'], 'skip'
    dst = os.path.join(outdir, f"{it['id']}.stl")
    if os.path.exists(dst) and os.path.getsize(dst) == it['bytes']: return it['name'], 'cached'
    url = 'https://api.mapzebrain.org' + urllib.request.quote(it['mesh'], safe='/%')
    urllib.request.urlretrieve(url, dst)
    return it['name'], os.path.getsize(dst)
with cf.ThreadPoolExecutor(6) as ex:
    res = list(ex.map(get, items))
print(sum(1 for _, r in res if isinstance(r, int) or r == 'cached'), 'downloaded;', [n for n, r in res if r == 'skip'])
