// 법정동코드 시도 part, as the profile's region filter speaks it. 광주 (29) and 전남 (46) became
// 전남광주통합특별시 (12) on 2026-07-01.
export const SIDO = [
  { key: "11", label: "서울" }, { key: "26", label: "부산" }, { key: "27", label: "대구" },
  { key: "28", label: "인천" }, { key: "30", label: "대전" }, { key: "31", label: "울산" },
  { key: "36", label: "세종" }, { key: "41", label: "경기" }, { key: "51", label: "강원" },
  { key: "43", label: "충북" }, { key: "44", label: "충남" }, { key: "52", label: "전북" },
  { key: "12", label: "전남광주" }, { key: "47", label: "경북" }, { key: "48", label: "경남" },
  { key: "50", label: "제주" },
];

const SUCCESSORS: Record<string, string> = { "29": "12", "46": "12" };

/** A profile saved before 2026-07-01 may hold 광주 or 전남: show and save them as 전남광주. The
 * API matches the old codes too, so nothing is lost either way. */
export function currentRegions(codes: readonly string[]): string[] {
  return [...new Set(codes.map((c) => SUCCESSORS[c] ?? c))];
}
