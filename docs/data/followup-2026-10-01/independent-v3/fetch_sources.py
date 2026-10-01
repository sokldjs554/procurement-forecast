from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser
import json,datetime,hashlib,time
base=Path(__file__).resolve().parent
if (base/'FROZEN_SHA256SUMS').exists(): raise RuntimeError('Frozen dataset: do not overwrite after scoring')
co=json.loads((base/'cohort-selection.json').read_text()); logs=[]; robots={};last={}
ua='PublicSourceEvaluationResearch/1.0'
def get(u):
 h=urlsplit(u).netloc;delay=2-(time.monotonic()-last.get(h,0))
 if delay>0:time.sleep(delay)
 try:
  r=urlopen(Request(u,headers={'User-Agent':ua}),timeout=50);data=r.read();status=r.status;headers=dict(r.headers)
 except HTTPError as e:data=e.read();status=e.code;headers=dict(e.headers)
 last[h]=time.monotonic()
 return data,status,headers
for s in co['sources']:
 u=s['url'];host=urlsplit(u).netloc
 if host not in robots:
  ru='https://'+host+'/robots.txt';b,status,headers=get(ru)
  rp=RobotFileParser();rp.parse(b.decode('utf-8','replace').splitlines() if status==200 else [])
  if status>=500 or status in [401,403,429]:raise RuntimeError('Cannot establish robots permission '+host)
  robots[host]=rp
  (base/(host+'-robots.txt')).write_bytes(b)
  logs.append({'url':ru,'status':status,'fetched_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'sha256':hashlib.sha256(b).hexdigest(),'note':'404 means robots policy absent' if status==404 else 'Parsed policy'})
 if not robots[host].can_fetch(ua,u):raise RuntimeError('Disallowed '+u)
 b,status,headers=get(u);ext='.pdf' if s['doc_type']=='work_plan' else '.html'
 if status!=200:raise RuntimeError(str(status)+u)
 (base/(s['id']+ext)).write_bytes(b)
 logs.append({'id':s['id'],'url':u,'status':status,'headers':headers,'fetched_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'original_path':s['id']+ext,'original_sha256':hashlib.sha256(b).hexdigest(),'bytes':len(b)})
 (base/'fetch-log.json').write_text(json.dumps(logs,ensure_ascii=False,indent=2)+'\n')
 print(s['id'],len(b),flush=True)
