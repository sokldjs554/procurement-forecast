import { describe, expect, it } from "vitest";

import { SIDO, currentRegions } from "./regions";

describe("currentRegions", () => {
  it("turns 광주 and 전남 into 전남광주, once", () => {
    expect(currentRegions(["29", "41", "46"])).toEqual(["12", "41"]);
  });

  it("keeps current codes as they are", () => {
    expect(currentRegions(["11", "12"])).toEqual(["11", "12"]);
    expect(currentRegions([])).toEqual([]);
  });

  it("offers only current 시도", () => {
    const keys = SIDO.map((s) => s.key);
    expect(keys).toHaveLength(16);
    expect(keys).toContain("12");
    expect(keys).not.toContain("29");
    expect(keys).not.toContain("46");
  });
});
