import { expect, test } from "@playwright/test";

test("the public walkthrough opens its sources and briefing without a payment or broken source link", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("./");
  await page.getByRole("link", { name: "대표 사례 체험하기", exact: true }).click();
  await expect(page).toHaveURL(/\/app\/opportunities\/\d+\/$/);
  await expect(page.getByRole("heading", { level: 1 })).toContainText(/디지털트윈|사업|구축|설치/);
  await expect(page.getByText("합성 데이터 기반 · 실제 정확도 미검증", { exact: true })).toBeVisible();
  const sources = page.locator("#evidence details");
  expect(await sources.count()).toBeGreaterThanOrEqual(3);
  await sources.first().locator("summary").click();
  await expect(sources.first()).toContainText("전체 추출 텍스트");
  await expect(sources.first().locator("mark").first()).toBeVisible();
  // toBeVisible passes for a mark scrolled out of its box; the reader must land on the evidence
  await expect.poll(() => sources.first().locator(".overflow-y-auto").evaluate((box) => {
    const mark = box.querySelector("mark");
    if (!mark) return false;
    const b = box.getBoundingClientRect();
    const m = mark.getBoundingClientRect();
    return m.top >= b.top && m.top < b.bottom;
  })).toBe(true);
  await expect(page.locator('#evidence a[target="_blank"]')).toHaveCount(0);

  await page.getByRole("link", { name: "③ 영업 브리핑", exact: true }).click();
  await page.getByRole("button", { name: "예시 브리핑 보기", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("크레딧은 차감되지 않아요");
  await expect(page.locator("article.prose-brief")).toContainText("한 줄 요약");
  await expect(page.getByRole("button", { name: "예시 브리핑 다시 보기" })).toBeVisible();
  await page.getByRole("button", { name: "예시 브리핑 다시 보기" }).click();
  await expect(page.locator("article.prose-brief")).toHaveCount(1);
  await page.reload();
  await expect(page.getByRole("button", { name: "예시 브리핑 보기", exact: true })).toBeVisible();
  expect(errors).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test("the feed search still filters while the billing screen clearly stays a read-only example", async ({ page }) => {
  await page.goto("./app/");
  await expect(page.getByRole("heading", { name: "기회 피드", exact: true })).toBeVisible();
  await page.getByRole("textbox", { name: "사업명 검색" }).fill("디지털트윈");
  const cards = page.locator('a[href*="/app/opportunities/"]').filter({ has: page.locator("h3") });
  await expect(cards.first()).toBeVisible();
  await expect.poll(async () => {
    const titles = await cards.locator("h3").allTextContents();
    return titles.length > 0 && titles.every((title) => title.includes("디지털트윈"));
  }).toBe(true);

  await page.goto("./app/billing/");
  await expect(page.getByText(/공개 데모에서 브리핑을 열어도 크레딧은 차감되지 않아요/)).toBeVisible();
  await expect(page.getByRole("button", { name: "카드 변경" })).toBeDisabled();
  await expect(page.getByRole("button", { name: /\+30/ })).toBeDisabled();
});

test("an operator can also follow the representative case through its example brief", async ({ page }) => {
  await page.goto("./login/");
  await page.getByRole("button", { name: "운영자로 둘러보기", exact: true }).click();
  await expect(page).toHaveURL(/\/admin\/$/);
  await page.goto("./");
  await page.getByRole("link", { name: "대표 사례 체험하기", exact: true }).click();
  await expect(page).toHaveURL(/\/app\/opportunities\/\d+\/$/);
  await page.getByRole("button", { name: "예시 브리핑 보기", exact: true }).click();
  await expect(page.locator("article.prose-brief")).toContainText("한 줄 요약");
  await expect(page.getByRole("status")).toContainText("크레딧은 차감되지 않아요");
});
