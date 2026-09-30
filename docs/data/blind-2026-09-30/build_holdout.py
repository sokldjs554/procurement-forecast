"""Independent manual annotations. Never run against, or import, the extractor."""
from pathlib import Path
import json,hashlib,datetime,re
R=Path(__file__).parent
sources={
 'seosan_admin_20251204':('서산시','2025-12-04',2026,'council_minutes','제310회 서산시의회 (제2차 정례회) 행정문화복지위원회회의록 제5차'),
 'seosan_industry_20251205':('서산시','2025-12-05',2026,'council_minutes','제310회 서산시의회 제2차 정례회 산업건설위원회회의록 제6차'),
 'gangnam_admin_20240905':('서울특별시 강남구','2024-09-05',2024,'council_minutes','제321회 강남구의회(임시회) 행정안전위원회회의록 제2호'),
 'geumcheon_admin_20241126':('서울특별시 금천구','2024-11-26',2025,'council_minutes','제252회 서울특별시 금천구의회(제2차정례회) 행정재경위원회회의록 제1호'),
 'geumcheon_plan_20241128':('서울특별시 금천구','2024-11-28',2025,'council_minutes','제252회 서울특별시 금천구의회(제2차정례회) 행정재경위원회회의록 제3호'),
 'gangnam_budget_2025':('서울특별시 강남구','2024-12-17',2025,'budget_book','2025년도 예산안 및 기금운용계획안 심사보고서'),
}
rows=[]
def sig(phrases,category=None,commitment='planned',budget=None,year=None,scope=None):
 d={'title_keywords':phrases,'commitment':commitment,'budget_krw':budget}
 if category is not None:d['category']=category
 if year is not None:d['expected_year']=year
 if scope:d['budget_scope']=scope
 return d
def add(sid,lo,hi,expected,kind,note):
 s=(R/'sources'/f'{sid}.txt').read_text();lines=s.split('\n');start=sum(len(v)+1 for v in lines[:lo-1]);text='\n'.join(lines[lo-1:hi])+'\n'
 assert s[start:start+len(text)]==text,(sid,lo,hi)
 inst,date,fiscal,typ,title=sources[sid]
 locator={'text_char_start':start,'text_char_end':start+len(text),'text_line_start':lo,'text_line_end':hi,'offset_unit':'Unicode code points in archived UTF-8 decoded text'}
 if typ=='budget_book':locator.update(pdf_page_start=s[:start].count('\f')+1,pdf_page_end=s[:start+len(text)].count('\f')+1)
 row={'id':f'new-blind-{len(rows)+1:03d}','source_id':sid,'doc_type':typ,'institution':inst,'date':date,'date_basis':'report_date_on_cover; online_publication_date_unknown' if typ=='budget_book' else 'meeting_date_in_transcript_header; online_publication_date_unknown','fiscal_year':fiscal,'text':text,'expected':expected,'locator':locator,'annotation_type':kind,'annotation_notes':note}
 for e in expected:
  assert any(re.sub(r'\s+','',p) in re.sub(r'\s+','',text) for p in e['title_keywords']),(row['id'],e)
 rows.append(row)
S='seosan_admin_20251204'
add(S,38,57,[sig(['사무실 집기 및 물품','노후 사무 장비와 집기'],'other',budget=100000000,year=2026,scope='Furniture and office equipment purchase allocation only; not aggregate asset acquisition 153,073,000.'),sig(['노후 관용 차량 대체 취득','이카운티'],'mobility',year=2026,scope='Replacement bus is separately described; its own amount is not stated. Do not subtract unrelated totals.')],'future_procurement','Executive explains two separate acquisitions; both included. Fiscal year is explicit in full source budget heading.')
add(S,76,93,[],'recurring_nonprocurement','National property rent under existing agreement, with prepaid fiscal periods; no new procurement project.')
add(S,1137,1144,[sig(['생성형 AI 도구 구입','AI 생성형 도구'],'ai_data',year=2026)],'future_procurement','Executive plans additional AI tools next year. Already-built data platform and its ongoing maintenance expense do not establish a new procurement scope in this passage.')
add(S,1228,1240,[sig(['지능 정보화 종합 계획 수립'],'public_sw',year=2026),sig(['인공지능 기본계획 수립'],'ai_data',year=2026)],'future_procurement','Two named outsourced planning studies confirmed by executive, with quoted cost estimation and future ordering. No numerical study budget is stated.')
add(S,1241,1258,[sig(['AI 민원 플랫폼'],'ai_data',budget=280000000,year=2026,scope='Combined pilot chatbot and voicebot budget; 1 billion-plus is hypothetical full expansion, not pilot budget.')],'future_procurement','Executive confirms pilot scope and next-year plan. One platform project with two components, not two duplicate signals.')
add(S,1277,1296,[sig(['AI IoT 센서 기반 주차 정보 공유 서비스','주차 정보 공유 서비스'],'smart_city',commitment='committed',budget=692000000,year=2026,scope='Total sharing-service budget 692m, including secured national grant of 484m. Existing car-park construction is background.')],'future_procurement','Funding secured and executive describes implementation, roadside boards and mobile integration. No separate procurement is inferred for already-underway car parks.')
add(S,1830,1845,[],'subsidy','Explicit installation-cost support for private homes and senior centres; no direct municipal procurement commitment stated.')
add(S,13,36,[],'procedure','Committee opening, schedule changes and budget-hearing procedure only.')
add(S,1174,1188,[],'completed_or_operational','Existing data platform screens and website are already functioning; no prospective project or new contract in excerpt.')
S='seosan_industry_20251205'
add(S,847,877,[sig(['수도기 미터기 및 보호통 구입','보호통이랑 계량기'],'energy_env',budget=150000000,year=2026,scope='Annual meter/protective-container acquisition budget, procured in batches as needed.')],'future_procurement','Executive confirms planned acquisitions even though bulk-purchase interpretation is corrected to on-demand batches.')
add(S,1503,1536,[sig(['공공디자인 진흥계획 용역','공공디자인 진흥 계획'],year=2026)],'future_procurement','Executive confirms first statutory outsourced plan, budget and future ordering. Category omitted because facility/design-policy boundary is ambiguous; no numerical budget.')
add(S,1172,1182,[],'member_request_only','Member suggests fencing/removal and compares rent. This excerpt contains no executive adoption; do not convert recommendation into procurement.')
add(S,12,34,[],'procedure','Substantive budget-hearing procedure and list of departments; no project.')
add(S,959,963,[],'completed_work','Member praises an already-created reservoir trail; executive thanks them. No new work is proposed in excerpt.')
S='geumcheon_admin_20241126'
add(S,848,864,[sig(['공중화장실 정비','호압사'],'facility',budget=1244300000,year=2025,scope='2025 proposed annual construction allocation after 50m reduction: 1,244,300,000. Earlier 1,294,300,000 annual and 1,394,300,000 total figures are superseded/disputed; historical design 100m is not next-year procurement.')],'future_procurement','Executive confirms Hoapsa toilet reconstruction, relocation and 50m reduction. Excerpt ends before separately-budgeted upstream pipe replacement, so no unlabelled second project.')
add(S,60,88,[],'procedure','Ordinance deliberation and adoption, not an identifiable procurement plan.')
add(S,386,415,[],'subsidy_and_past_service','Scholarship fund, personnel/operating costs and past event services. No new identified procurement commitment.')
add(S,718,727,[],'subsidy','Youth cultural, examination and interview cost support; executive confirms benefit eligibility and limits, not municipal procurement.')
S='geumcheon_plan_20241128'
add(S,57,59,[sig(['암호화 트래픽 가시화 시스템'],'public_sw',year=2025)],'future_procurement','Three systems are mentioned by member, but question and executive 2025 response specifically concern encrypted-traffic visibility system. No independent executive plan for the other two is inferred.')
add(S,61,68,[sig(['배수로 그레이팅 교체','썬큰광장'],'facility',year=2025)],'future_procurement','Executive confirms replacement of broken drainage gratings; 317 describes inventory, not budget.')
add(S,269,277,[sig(['얼음생수 나눔냉장고'],'welfare_care',year=2025)],'future_procurement','Executive confirms new next-year refrigerator programme and outsourced refrigerator installation/management. Water distribution is one programme, not several signals.')
add(S,130,137,[],'completed_nonprocurement','Previously donor-funded volunteer meal programme; continuation lacked resources, with no municipal procurement plan.')
add(S,123,123,[],'member_request_only','Member recommends future flag campaigns and coordination. No identifiable procurement supported by an executive response in excerpt.')
S='gangnam_admin_20240905'
add(S,476,487,[sig(['제2별관 외벽 판넬 설치 공사'],'facility',year=2024)],'future_procurement','Executive official work report schedules second-half exterior-panel construction; other lines are routine personnel/welfare/intercity policy.')
add(S,536,550,[sig(['2층, 4층 화장실','화장실 리모델링'],'facility'),sig(['1층 라운지'],'facility',year=2024)],'future_procurement','Two separately named future renovations confirmed. Toilet timing not explicit, so expected_year is omitted; lounge explicitly by year end. Historical hundreds of billions are not budgets for either.')
add(S,22,29,[],'subsidy_policy','Scholarship fund ordinance and future educational assistance, not procurement.')
add(S,488,490,[],'completed_work','Election held and temporary offices already installed/operating. Cultural-centre construction is background, with no new procurement statement in excerpt.')
S='gangnam_budget_2025'
add(S,1379,1405,[sig(['강남 기록 용역 및 전시회 개최'],'tourism_culture',budget=206900000,year=2025,scope='Whole named records-and-exhibition programme proposed budget, in thousand-won table; not a contract award value.')],'budget_procurement','Complete named budget subsection. Reviewer suggestions for an archive/VR are not executive-approved extra projects.')
add(S,2374,2391,[sig(['동복합문화센터 리모델링 공사'],'facility',budget=1967852000,year=2025,scope='Whole named two-centre remodelling programme, including supervision; component estimates are not additional signals.')],'budget_procurement','Official budget subsection contains two sites within one programme and a precise thousand-won total. Aggregate programme identity is predeclared.')
add(S,4446,4462,[sig(['일원1동 스마트보안등 시스템 구축'],'smart_city',budget=300000000,year=2025,scope='Ilwon-1 smart-lighting project only, 300,000 thousand KRW.')],'budget_procurement','Complete subsection crossing a PDF page boundary, keeping original unit and table structure.')
add(S,5083,5096,[sig(['스마트 빗물펌프장 원격제어 고도화'],'smart_city',budget=145000000,year=2025,scope='Precise table budget 145,000 thousand KRW; rounded prose says about 150m.')],'budget_procurement','Remote-control hardware upgrade, not previous subsection flood-information website. Exact table amount takes precedence over rounded prose.')
add(S,3932,3950,[],'subsidy','Prepaid transit-card incentive to elderly drivers surrendering licences. The benefit budget does not establish an identifiable municipal procurement.')
add(S,167,176,[],'recurring_nonprocurement','Council member allowances and general operating appropriations; no procurement project.')

sha=lambda b:hashlib.sha256(b).hexdigest()
casebytes=''.join(json.dumps(x,ensure_ascii=False,separators=(',',':'))+'\n' for x in rows).encode()
(R/'cases.jsonl').write_bytes(casebytes)
sm=[]
for sid,(inst,date,fiscal,typ,title) in sources.items():
 meta=json.loads((R/'sources'/f'{sid}.retrieval.json').read_text());p=R/'sources'/f'{sid}.txt';raw=p.read_bytes()
 sm.append(meta|{'path':str(p.relative_to(R)),'sha256':sha(raw),'institution':inst,'doc_type':typ,'title':title,'publication_date':None,'publication_date_basis':'No online publication date verified; search-engine published dates not treated as authoritative.','meeting_date':date if typ=='council_minutes' else None,'report_date':date if typ=='budget_book' else None,'fiscal_year':fiscal,'representation':'Full minutes root text from archived HTTP HTML; HTMLParser entity decoding, block/line boundaries and whitespace normalization; no semantic edits.' if typ=='council_minutes' else 'Full 545-page PDF text from pdftotext -layout, including form feeds and original table spacing.','original_available':True})
manifest={'schema_version':'source-holdout-v1','name':'new-institutions-blind-20260930','frozen_before_scoring':True,'frozen_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'label_origin':'AI-authored independent source annotation by delegated new_blind_holdout agent; no production extractor, development gold or prior result files read; no model outputs seen; no paid APIs. Not human/expert-validated gold.','cases_path':'cases.jsonl','cases_sha256':sha(casebytes),'sources':sm,'sampling':'Purposive source- and semantic-diversity sample from six official documents; 33 contiguous excerpts. Positives require executive response or official budget plan. Not randomly sampled, not population-representative. Cases selected before scoring; lexical simplicity was not a selection criterion.','excluded_institutions':['Seongnam','Wonju','Dongducheon','Chungcheongbuk-do'],'schema_checked_from':'Only app/eval/holdout.py was inspected; no extractor implementation or outputs used.','taxonomy':['smart_city','safety_cctv','public_sw','ai_data','mobility','energy_env','welfare_care','education','tourism_culture','facility','other'],'limits':['AI labels need expert adjudication.','Publication dates unknown; meeting/report dates are explicitly labelled and are not asserted to be publication dates.','One budget_book source is a council budget review report containing official proposed allocations, not the original standalone budget book.','Parsed-text extraction only: no OCR, discovery/recall of whole corpora, actual future tenders, or commercial forecasting accuracy measured.','Source coverage is three institutions, six documents and 33 curated excerpts; no claim of statistical representativeness.','Some excerpts begin within an executive turn; provenance is available in the complete archived source and exact offsets.','Committed means funding secured plus described implementation; planned covers budget proposals and executive intentions, not contract awards.','Sources contain their own typographical errors and inconsistent references; preserved rather than silently corrected.']}
(R/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'cases':len(rows),'positive_cases':sum(bool(x['expected']) for x in rows),'negative_cases':sum(not x['expected'] for x in rows),'signals':sum(len(x['expected']) for x in rows),'manifest_sha256':sha((R/'manifest.json').read_bytes()),'cases_sha256':sha(casebytes)},indent=2))
