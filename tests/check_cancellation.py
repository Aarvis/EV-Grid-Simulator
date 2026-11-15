import csv, io, json, time
from urllib.request import Request, urlopen
base='http://localhost:8765'
def get(path):
    return json.load(urlopen(base+path))
def post(path,body=None):
    return json.load(urlopen(Request(base+path,data=json.dumps(body or {}).encode(),headers={'Content-Type':'application/json'},method='POST')))
r=post('/api/runs',{'date':'2018-10-01','days':2})['id']
deadline=time.monotonic()+55
while time.monotonic()<deadline:
    job=get('/api/runs/'+r)
    if job['completed']>=3:
        post('/api/runs/'+r+'/cancel')
        break
    time.sleep(.3)
else:
    raise RuntimeError('Run did not make progress')
while time.monotonic()<deadline:
    job=get('/api/runs/'+r)
    if job['status']!='running':
        break
    time.sleep(.2)
assert job['status']=='cancelled' and 0<job['completed']<192,job
rows=list(csv.DictReader(io.StringIO(urlopen(base+'/api/runs/'+r+'/export').read().decode())))
assert len(rows)==job['completed']
assert job['totals']['balance_failures']==0
print(json.dumps({'partial_cancellation':'passed','id':r,'retained_intervals':len(rows),'balance_failures':0}))