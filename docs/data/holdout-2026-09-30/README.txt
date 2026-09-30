Blinded source-grounded holdout, drafted 2026-09-30

32 cases / 8 authoritative source documents / 3 institutions: Dongducheon,
Wonju, Chungcheongbuk-do. 17 signal-bearing cases, 15 negative cases, 28 project
signals. No Seongnam source used. No implementation, model output, prior
examples or evaluator was inspected by the collecting agent.

Start with manifest.json and cases.jsonl. holdout.jsonl preserves the richer
original source-derived labels, including ambiguous values intentionally omitted
from scoreable expected fields. sources-manifest.json preserves source metadata.
Every case.text is an exact contiguous substring of its archived .txt source,
with character and line offsets. Every title keyword occurs literally in that
case. Raw source bytes were not obtainable by direct workspace download; .html
files and PDF .txt files are the web connector's extracted representations.
Their hashes are genuine archive hashes, not claimed original-response hashes.

Method: public official-source discovery, review of readable project/budget
sections, deliberate coverage of purchases, existing operations, salaries,
subsidies, questions, conditional promises, multi-year costs and suspended works. Concrete projects
under review or explicitly declined remain labelled with that commitment; the
signal-bearing count is not a count of affirmative future procurements.
This is purposive sampling, not a random or representative corpus. Labels are
source-grounded AI-authored and blinded to implementation/output. They have been reviewed by a separate blind AI agent, but have not
been independently human/expert-reviewed.

Stage semantics: 'committed' marks an explicit budget line/proposal with amount;
it is NOT a claim of enacted final appropriation or signed procurement contract.
'planned' marks explicit executive intentions. Category omitted where hardware
does not map unambiguously to the supplied product taxonomy. Missing expected
field is unscorable; a present null means source does not specify that field.

Source-native document types are retained as source_document_type. For extraction
interface compatibility, work_plan and budget_review are mapped to budget_book.
All published_at fields are null. date_basis='meeting_date' and 'report_cover_date'
are genuine source dates; 'fiscal_year_context_only' is a contextual January 1
placeholder, never an asserted publication date. Retrieval date follows session
environment date, 2026-09-30.

Important review boundaries:
- fresh-003: smart-centre construction 344m versus operations 63m; amount and
  commitment omitted from scoring because annual/programme timing is unclear.
- fresh-002: one modernization programme, with 2026 VPN 212m, 2026 ringtone
  server 71m, 2027 VPN 85m; budget/year/commitment excluded from scoring.
- fresh-014: one-time accessibility-related terminal parts upgrade versus
  recurring maintenance; all three explicit purchases labelled for blind review.
- fresh-027: concrete memorial proposal plus executive active review is a
  reviewing signal; no year/amount inferred.
- fresh-030: expressly suspended named construction is a declined signal; no
  historical, returned or hoped-for budget is used as current project price.
- fresh-030 through fresh-032 use publisher-marked provisional minutes.
- Exact source transcript typos are preserved, not silently corrected.

No future tender/award outcome is provided. This holdout can assess extraction
from source evidence, not accuracy of predicting whether procurement will occur.
Duplicate/novelty comparison against development sets is left to the parent,
because the collector was intentionally blinded to them. Blind review changes are recorded in review-changes.json. This is the final
pre-scoring freeze; do not alter gold in response to evaluated model output.
