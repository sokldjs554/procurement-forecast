from pathlib import Path
from html.parser import HTMLParser
import re,json,hashlib
base=Path(__file__).resolve().parent
if (base/'FROZEN_SHA256SUMS').exists(): raise RuntimeError('Frozen dataset: do not overwrite after scoring')
class Extract(HTMLParser):
 def __init__(self):super().__init__(convert_charrefs=True);self.active=False;self.depth=0;self.out=[];self.skip=0
 def handle_starttag(self,tag,attrs):
  attrs=dict(attrs)
  if attrs.get('id')=='canvas' and not self.active:self.active=True;self.depth=1;return
  if not self.active:return
  if tag=='div':self.depth+=1
  if tag in ['script','style']:self.skip+=1
  if tag in ['br','div','p','hr','h1','h2','li','tr','spk']:self.out.append('\n')
 def handle_endtag(self,tag):
  if not self.active:return
  if tag in ['script','style']:self.skip=max(0,self.skip-1)
  if tag=='div':
   self.depth-=1
   if self.depth==0:self.active=False
  if tag in ['div','p','h1','h2','li','tr','spk']:self.out.append('\n')
 def handle_data(self,data):
  if self.active and not self.skip:self.out.append(data)
for f in base.glob('*.html'):
 x=Extract();x.feed(f.read_text(encoding='utf-8'));t='\n'.join(s.strip() for s in re.sub(r'[^\S\n]+',' ',''.join(x.out)).splitlines() if s.strip())+'\n';f.with_suffix('.txt').write_text(t)
 print(f.stem,len(t),t[:100])
