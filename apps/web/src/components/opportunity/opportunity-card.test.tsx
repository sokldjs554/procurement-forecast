import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { Schemas } from "@/lib/api/client";

import { OpportunityCard } from "./opportunity-card";

type Card = Schemas["OpportunityCard"];

const base: Card = {
  id: 79,
  title: "스마트폴 구축 용역",
  institution: { code: "3000000", name: "서울특별시 중구" },
  department: "스마트도시과",
  category: "smart_city",
  category_label: "스마트시티",
  stage: "budget_line",
  stage_label: "예산 편성",
  status: "open",
  est_budget_krw: 350_000_000,
  bid_window_start: "2027-03-01",
  bid_window_end: "2027-06-30",
  window_passed: false,
  bid_published_at: null,
  tender_out: false,
  conversion_prob: 0.72,
  signal_count: 4,
  first_seen_at: "2025-11-12T00:00:00Z",
  last_signal_at: "2026-09-01T00:00:00Z",
  score: 0.84,
  reasons: ["관심 분야 일치", "관심 지역"],
  feedback: null,
  lead_days: 180,
  head_start_days: null,
};

describe("OpportunityCard", () => {
  it("shows the forecast for a project that has not been tendered yet", () => {
    render(<OpportunityCard item={base} />);

    expect(screen.getByRole("link")).toHaveAttribute("href", "/app/opportunities/79");
    expect(screen.getByText("입찰 약 6개월 후")).toBeInTheDocument();
    expect(screen.getByText("입찰 예상 2027.03 ~ 2027.06")).toBeInTheDocument();
    expect(screen.getByText("3억 5,000만원")).toBeInTheDocument();
    expect(screen.getByText(/공고 전환 추정치/)).toBeInTheDocument();
    expect(screen.getByText("84")).toBeInTheDocument();
    expect(screen.queryByText(/전에 찾음/)).not.toBeInTheDocument();
  });

  it("states facts instead of a forecast once the tender is out", () => {
    render(
      <OpportunityCard
        item={{
          ...base,
          stage: "bid_notice",
          stage_label: "입찰공고",
          status: "bid_open",
          tender_out: true,
          bid_published_at: "2026-06-15",
          lead_days: null,
          head_start_days: 365,
        }}
      />,
    );

    expect(screen.getByText("입찰공고 2026.06.15")).toBeInTheDocument();
    expect(screen.getByText("공고 12개월 전에 찾음")).toBeInTheDocument();
    expect(screen.queryByText(/입찰 예상/)).not.toBeInTheDocument();
    // there is no probability left to show once the tender exists
    expect(screen.queryByText(/공고로 이어질 확률/)).not.toBeInTheDocument();
  });

  it("says so when the expected window has passed without a tender", () => {
    render(<OpportunityCard item={{ ...base, window_passed: true, lead_days: 0 }} />);

    expect(screen.getByText("예상 시기 지남")).toBeInTheDocument();
    expect(screen.getByText("입찰 예상 기간 중")).toBeInTheDocument();
  });

  it("does not invent an amount or a score it does not have", () => {
    render(<OpportunityCard item={{ ...base, est_budget_krw: null, score: null }} />);

    expect(screen.getByText("금액 미상")).toBeInTheDocument();
    expect(screen.queryByText("적합도")).not.toBeInTheDocument();
  });
});
