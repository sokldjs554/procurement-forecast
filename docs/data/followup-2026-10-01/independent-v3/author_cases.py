"""Frozen independent labels: source text slices only; never imports extraction code.
Run BEFORE predictions only. After freeze do not rerun or modify to fit outputs.
"""
from pathlib import Path
import json,hashlib,datetime
P=Path(__file__).resolve().parent
if (P/'FROZEN_SHA256SUMS').exists(): raise RuntimeError('Frozen labels: create a new version for any correction')
texts={f.stem:f.read_text() for f in P.glob('*.txt') if '-robots' not in f.stem}
meta={
 'gwangjin-5492':('광진구','2023-11-29','council_minutes'),
 'gwangjin-5614':('광진구','2023-11-30','council_minutes'),
 'seongbuk-300-4':('성북구','2023-12-04','council_minutes'),
 'yangcheon-8370':('양천구','2023-12-12','council_minutes'),
 'seongbuk-workplan-2024':('성북구도시관리공단','2024-02-21','budget_book'),
}
cases=[]
def signal(kw,cat,budget,commit='committed',year=2024):
 return {'title_keywords':kw,'category':cat,'budget_krw':budget,'expected_year':year,'commitment':commit}
def add(cid,sid,start,end,expected,notes,tags):
 s=texts[sid];a=s.index(start);b=s.index(end,a)+len(end)
 assert a>=0 and b>a
 institution,date,dt=meta[sid]
 loc={'text_char_start':a,'text_char_end':b,'offset_unit':'Unicode code points','text_sha256':hashlib.sha256(s[a:b].encode()).hexdigest()}
 if dt=='budget_book':loc.update(pdf_page_1based=s[:a].count('\f')+1,pdf_end_page_1based=s[:b].count('\f')+1)
 cases.append({'id':cid,'source_id':sid,'doc_type':dt,'institution':institution,'date':date,
 'date_basis':'Hearing date in archived official minutes; not asserted to be web-publication date.' if dt=='council_minutes' else '2024-02-21 hearing date of attached 2024 workplan; archived date-provenance page links to byte-identical appendix. PDF creation metadata 2024-02-05. Not web-publication date.',
 'fiscal_year':2024,'text':s[a:b],'expected':expected,'locator':loc,'difficulty_tags':tags,
 'annotation_notes':notes,'label_review':{'author':'separate evaluation agent; blind to provider code and predictions','review':'same annotator second-pass source review before freeze','human_expert_review':'NONE'}})

add('iv3-gj-cctv-scope','gwangjin-5492',
 '○이동길위원 그러면 94페이지요. CCTV 있잖아요.',
 '○스마트정보담당관 유종헌 조달로 되어 있는 걸.',
 [signal(['지능형 CCTV'],'safety_cctv',None),signal(['노후 CCTV','노후화 CCTV'],'safety_cctv',None)],
 'Two distinct executive-backed budget projects: pole-plus-camera expansion and roughly 120 existing camera replacements. Unit prices 2,500만원/300만원 are not project totals; no budget multiplication. Year is the explicit 2024 budget being examined, not the seven-year useful life.',
 ['multiple_signals','unit_price_not_total','replacement_vs_expansion','fiscal_context'])
add('iv3-gj-mentoring','gwangjin-5614',
 '설명서 230페이지에 보면 온라인진학 상담 지원 해서 멘토링서비스 이용권 구매',
 '접수는 선착순으로 진행할 예정에 있습니다.',
 [signal(['멘토링','온라인진학 상담'],'education',None)],
 'Executive explains a newly budgeted purchased mentoring service. An in-person center is a rejected alternative, not an additional purchase. No contract yet does not erase the concrete budget-backed intent. No amount stated.',
 ['rejected_alternative','uncontracted_budget','service_purchase'])
add('iv3-gj-elementary-subscription','gwangjin-5614',
 '○김상희위원 그것과 연관이 되는지 모르겠는데 그다음에 233페이지',
 '○교육지원과장 김애덕 2개월입니다.',
 [signal(['초등학생','온라인학습','수강권'],'education',None)],
 'Newly budgeted elementary interactive learning subscriptions. Adjacent existing 서울런 and 강남 인강 programs are comparator history. 6만원/90명/5기 are unit and quantity references, not an explicit total appropriation; do not synthesize a total budget.',
 ['service_purchase','existing_program_comparators','unit_price_not_total'])
add('iv3-gj-mobile-water','gwangjin-5614',
 '설명서 217페이지에 보면 이동형 물놀이장 물품구입이 있는데',
 '그렇게 준비하고 있습니다.',
 [signal(['이동형 물놀이장'],'facility',None,'planned')],
 'Executive plans summer temporary waterplay equipment at four as-yet-unfixed districts. Existing fixed water facilities and last year popup playground are not new signals. Site uncertainty is retained as planned, not declined. 2024 derives only from this budget hearing fiscal context.',
 ['uncertain_locations','past_comparator','relative_timing'])
add('iv3-gj-tools','gwangjin-5614',
 '○김상희위원 지금 여기에 보시면 공구대여소 등 해서 공구물품 구매 금액이 증가했잖아요?',
 '○김상희위원 그 점에 대해서는 저희가 세금을 내고 있는 부분들이기 때문에 적극적으로 동의하고요.',
 [signal(['공구'],'other',None)],
 'Budget increase buys replacement tools lost or damaged during public lending. This is replacement goods, not a labor-only maintenance contract. No amount for the tool procurement is stated; later publicity budget deliberately excluded as different business.',
 ['replacement_goods','no_exact_project_title','amount_unstated'])
add('iv3-gj-maintenance','gwangjin-5492',
 '○전은혜위원 과장님! 202쪽 정보시스템 도입 및 유지관리, 사업설명서 77쪽.',
 '저희가 완벽하게 운영할 수 있도록 하고 있습니다.',[],
 'The heading mentions introduction, but executive describes existing annual network/server maintenance contracts and resident staff. No new system purchase or replacement commitment in the excerpt.',
 ['heading_false_friend','existing_maintenance','executive_context'])
add('iv3-yc-completed-smartcity','yangcheon-8370',
 '예, 알겠습니다. 410쪽에 중소도시 스마트시티 조성사업은 한 5,000 정도 증액이 됐는데',
 '추진한 사업이라고 보시면 되겠습니다.',[],
 'Despite a budget increase question and system terminology, executive says the four projects were completed in October of the meeting year. Describes already installed RFID bicycle zones; no additional purchase plan.',
 ['completed_project','budget_increase_distractor','technology_terms'])
add('iv3-yc-appraisal-contingency','yangcheon-8370',
 '과장님 안녕하십니까? 김광성 위원입니다. 세출예산사업명세서 335쪽에 보시면',
 '○재무과장 고승환\n예, 그렇습니다.',[],
 '600만원 twice is a contingent appraisal fee reserve. Executive expressly states there is no particular purchase or sale planned. Neither real-estate purchase nor a definite commissioned appraisal should be inferred.',
 ['explicit_no_purchase','contingent_budget','fee_not_asset_cost'])
add('iv3-yc-maintenance-period','yangcheon-8370',
 '알겠습니다. 그리고 예산서 321페이지 보면 중·소도시 스마트시티 조성사업',
 '○스마트정보과장 이강헌\n예, 그렇습니다.',[],
 'Completed October project; next year amount covers two months of post-warranty maintenance, followed by full-year maintenance. Fiscal references do not imply future deployment.',
 ['completed_vs_future_maintenance','multi_year','budget_increase_distractor'])
add('iv3-yc-kiosk-reduction','yangcheon-8370',
 '○스마트정보과장 이강헌\n스마트도시 조성 및 운영이 있는데',
 '구매해서 보급할 계획으로 감액을 했습니다.',
 [signal(['교육용 키오스크','키오스크'],'education',None,'planned')],
 'Only two education kiosks are planned next year even though budget is reduced. Nine recycling robots and seven existing kiosk sites are historical inventory, not forecast quantities. No project budget provided; category uses educational purpose.',
 ['budget_reduction_positive','mixed_past_future','inventory_not_quantity'])
add('iv3-yc-onnara-total','yangcheon-8370',
 '그러면 본 위원장이 한 가지만 질의하겠습니다. 사업예산서에 보시면 행정정보시스템 보강 및 구축',
 '그것까지 총 6억 6,200 정도가 소요될 예정입니다.',
 [signal(['온나라','행정정보시스템'],'public_sw',662000000)],
 'One integrated Onnara 2.0 migration combines hardware, software and redundant peripheral hardware. 4억7,000 is hardware subtotal; 6억6,200 is stated total in same Korean amount convention. Fiscal2024 explicit hearing context. Budget is planned spending, not a signed contract.',
 ['subtotal_vs_total','multi_component_single_project','fiscal_context'])
add('iv3-yc-data-vs-consulting','yangcheon-8370',
 '마지막으로 궁금한 거 하나 더 질의 드리겠습니다. 422쪽에 빅데이터 과제 발굴 및 분석',
 '절감됐다고 보시면 될 것 같습니다.',
 [signal(['데이터','빅데이터'],'ai_data',20000000)],
 'Executive cancels next-year 8,000만원 consulting and replaces it with internal analysis plus data purchase budget. 2,000 and 6,000 retain 만원 units from same paragraph; 2,000만원 purchase, not 8,000만원 prior service or 6,000만원 savings. One data purchase signal only.',
 ['cancelled_service_vs_new_goods','budget_savings_distractor','ellipsis_amount_units'])
add('iv3-yc-display-carryover','yangcheon-8370',
 '두 번째 건은 목동깨비시장 홍보전광판 설치 사업입니다.',
 '’24년으로 이월하는 사항입니다.',
 [signal(['목동깨비시장','홍보전광판','홍보전자게시대'],'other',None,'planned')],
 'Executive carryover of a concrete digital publicity-display purchase to 2024 because site changes prevent in-year execution. Not a completed expenditure. Category other: generic market publicity equipment, no smart-city/AI functionality stated. No amount supplied.',
 ['carryover','site_change','explicit_two_digit_year'])
add('iv3-sb-health-equipment','seongbuk-300-4',
 '○이인순위원 과장님, 473쪽 중앙에 보면 자산취득비가 있어요.',
 '○도시안전과장 이인복 내년에 인바디 해서 체지방이라든가 이런 부분까지도 측정하려고 합니다.',
 [signal(['인바디','건강증진실'],'welfare_care',None)],
 'One employee health-room equipment purchase, centered on new InBody/body-composition measurement. Last-year thermometers/cholesterol meters already purchased and held in storage are not separately forecast. No budget stated.',
 ['mixed_inventory_and_future','cross_department_use','relative_year'])
add('iv3-sb-tools-next-year','seongbuk-300-4',
 '○경수현위원 과장님, 517페이지에 보면 공유도시 관련 예산이 있는데요.',
 '내년에도 마찬가지로 동의 수요를 파악해서 지원할 예정입니다.',
 [signal(['공구대여소','공구'],'other',None,'planned')],
 'Explicit continuation of demand-based tool purchases next year; prior-year purchase is context. Amount 200 lacks an explicit monetary unit in this excerpt, so budget is intentionally unknown rather than guessed.',
 ['recurrent_purchase','ambiguous_amount_units','demand_survey'])
add('iv3-sb-led-budget','seongbuk-300-4',
 '마지막으로 163쪽 환경과 소관 기후변화기금입니다.',
 '1억 8,800만 원을 예치할 계획입니다.',
 [signal(['LED'],'energy_env',10000000)],
 'Concrete low-income LED replacement expenditure 1,000만원 in 2024 fund plan. Fund revenue 1억9,900만원 and deposit 1억8,800만원 are not purchase budget. Executive budget-backed intent uses committed; no contract assertion.',
 ['fund_balances_vs_purchase','multiple_amounts','replacement'])
add('iv3-sb-maintenance-newline','seongbuk-300-4',
 '○박영섭위원 도시안전과장님, 472쪽에 보시면 재난안전상황실 유지보수비가',
 '○도시안전과장 이인복 예, 맞습니다.',[],
 'First appearance of 700만원 in next-year budget is seven months of maintenance after one-year warranty on system built in May2023. Word 도입 describes the budget line, not acquisition of new hardware/software.',
 ['new_budget_not_new_asset','warranty','year_resolution'])
add('iv3-sb-solar-proposal','seongbuk-300-4',
 '○이인순위원 공공건물은 그렇게 하고 앞으로 신규도 그렇게 계획이 있는데,',
 '○이인순위원 이상입니다.',[],
 'Councilmember suggests solar on specific parking lots. Executive says eligibility/efficiency and expansion need consideration, then promises to review. There is no chosen procurement, funded design, or executive implementation commitment. Excluded from concrete purchase forecasts even though exploratory reviewing exists in the general schema.',
 ['councilmember_proposal','polite_review_not_commitment','conditional_expansion'])
add('iv3-sb-private-charger','seongbuk-300-4',
 '○환경과장 유천곤 전기차 충전소가 아파트 등 공동주택은',
 '자기 투자비를 회수하게 됩니다. 이상입니다.',[],
 'Executive plans more sites but explicitly says a private operator installs chargers free of public purchase funding and recovers investment through user fees. Under this evaluation scope, not an authority procurement signal; public site incentives are not equipment price. Scope-sensitive negative, not a claim of no private commercial demand.',
 ['private_finance_not_public_purchase','mixed_past_future','incentive_not_purchase_price'])
add('iv3-sb-plan-iot','seongbuk-workplan-2024',
 ' 주차 공유서비스 운영 활성화',
 '∘ IOT 센서 추가 설치(교통지도과 협의)                       1월~12월         100',
 [signal(['IOT','IoT','Iot','센서'],'mobility',None,'planned')],
 '2024 workplan adds sensors for 50 parking spaces. Existing125/25 spaces and historical receipts are not new purchase quantity/budget. Named ongoing operating firms do not establish a new award. Category mobility is derived from parking purpose; smart_city is plausible but not used as the primary purpose label.',
 ['workplan','historical_revenue_distractor','quantity_vs_budget','purpose_taxonomy'])
add('iv3-sb-plan-cctv-replacements','seongbuk-workplan-2024',
 '장위3동 진흥선원 공동주차장 CCTV교체',
 '정릉2동 (나)공동주차장 CCTV 교체                9,250천원     수탁자산(공기구)',
 [signal(['장위3동','진흥선원'],'safety_cctv',18450000),signal(['솔샘터널','정릉3동'],'safety_cctv',12300000),signal(['정릉2동'],'safety_cctv',9250000)],
 'Three separately located, separately budgeted CCTV replacement asset rows in 2024 workplan. Amounts explicitly 천원, multiplied by1000. Source PDF page43/printed34 visually checked; fiscal year from document and case context. Do not aggregate into one project or treat them as mere maintenance.',
 ['table','multiple_signals','thousand_won','replacement_assets'])
add('iv3-sb-plan-parking-systems','seongbuk-workplan-2024',
 '마을공원 공영주차장 스마트 화재 감시시스템 구축',
 '삼선동 공영주차장 CCTV 설치                     40,000천원   수탁자산(공기구)',
 [signal(['마을공원','화재 감시'],'safety_cctv',40000000),signal(['입출차','차단기'],'mobility',50000000),signal(['삼선동'],'safety_cctv',40000000)],
 'Three distinct budgeted asset rows: smart fire-monitoring system, parking entry/exit barriers, and CCTV. Explicit 천원 units. Category safety_cctv covers surveillance/fire monitoring; mobility covers access barriers. Page43/printed34 visually checked. Adjacent canopy installation is outside selected contiguous excerpt.',
 ['table','multiple_signals','mixed_categories','thousand_won'])
add('iv3-sb-plan-historical-safety','seongbuk-workplan-2024',
 '    2023년 경영 전략별 주요성과',
 '○ 자동화재 탐지 설비 구축, CCTV, 비상벨 등 범죄예방을 위한 안전설비 설치',[],
 '2024 workplan page14/printed10 is headed 2023 major performance results. AED replacement, fire-detection equipment, CCTV and emergency bells are historical achievements, not2024 plans. Entire local heading and surrounding results retained. Visual inspection confirmed heading hierarchy.',
 ['workplan_with_past_section','heading_temporal_scope','multiple_equipment_distractors'])

for c in cases:
 s=texts[c['source_id']];loc=c['locator'];assert s[loc['text_char_start']:loc['text_char_end']]==c['text']
 assert len(c['text'])>60
 assert c['date']<'2026-01-01'
assert len(cases)>=18 and sum(bool(c['expected']) for c in cases)>=10 and sum(not c['expected'] for c in cases)>=6
assert len({c['institution'].replace('도시관리공단','') for c in cases})>=3
(P/'cases.jsonl').write_text(''.join(json.dumps(c,ensure_ascii=False)+'\n' for c in cases))
print(json.dumps({'cases':len(cases),'positive_cases':sum(bool(c['expected']) for c in cases),'positive_signals':sum(len(c['expected']) for c in cases),'negative_cases':sum(not c['expected'] for c in cases),'label_authored_at':datetime.datetime.now(datetime.timezone.utc).isoformat()},indent=2))
