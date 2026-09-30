from html.parser import HTMLParser
from pathlib import Path
import re, json, hashlib, urllib.request, datetime, subprocess
ROOT=Path(__file__).parent
class MinutesText(HTMLParser):
 def __init__(self,target):
  super().__init__(convert_charrefs=True);self.target=target;self.depth=0;self.parts=[]
 def handle_starttag(self,tag,attrs):
  a=dict(attrs)
  if tag=='div':
   if self.depth:self.depth+=1
   elif a.get('id')==self.target:self.depth=1
  if self.depth and tag in {'div','p','br','hr','li','h1','h2','dt','dd','tr'}:self.parts.append('\n')
 def handle_endtag(self,tag):
  if self.depth and tag in {'div','p','li','h1','h2','dt','dd','tr'}:self.parts.append('\n')
  if tag=='div' and self.depth:self.depth-=1
 def handle_data(self,data):
  if self.depth:self.parts.append(data)
 def text(self):
  return '\n'.join(x for line in ''.join(self.parts).splitlines() if (x:=re.sub(r'[\t \xa0]+',' ',line).strip()))+'\n'
def fetch(sid,url):
 with urllib.request.urlopen(url,timeout=30) as r:
  b=r.read();p=ROOT/'sources'/f'{sid}.html';p.write_bytes(b)
  meta={'id':sid,'url':url,'retrieved_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'http_status':r.status,'content_type':r.headers.get('content-type'),'original_path':str(p.relative_to(ROOT)),'original_sha256':hashlib.sha256(b).hexdigest(),'bytes':len(b)}
  (ROOT/'sources'/f'{sid}.retrieval.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2))
if not (ROOT/'sources/geumcheon_plan_20241128.html').exists():fetch('geumcheon_plan_20241128','https://council.geumcheon.go.kr/council/viewer/minutes/2654.do')
for p in (ROOT/'sources').glob('*.html'):
 parser=MinutesText('content' if p.stem.startswith('seosan') else 'minutes');parser.feed(p.read_text());out=parser.text();p.with_suffix('.txt').write_text(out)
 print(p.stem,len(out),out[:120].replace('\n',' | '))
subprocess.run(['pdftotext','-layout',str(ROOT/'sources/gangnam_budget_2025.pdf'),str(ROOT/'sources/gangnam_budget_2025.txt')],check=True)
