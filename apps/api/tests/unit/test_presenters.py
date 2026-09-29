from datetime import date

from app.api.presenters import head_start_days
from app.db.models import Opportunity

TODAY = date(2026, 1, 1)


def test_empty_historical_opportunity_does_not_claim_a_verified_tender() -> None:
    from app.api.presenters import card

    opp = Opportunity(
        id=1,
        title="withdrawn evidence",
        category="other",
        stage="bid_notice",
        status="dormant",
        signal_count=0,
        conversion_prob=0,
        first_seen_at=TODAY,
        last_signal_at=TODAY,
    )
    shown = card(opp, {}, None, TODAY)
    assert shown.tender_out is False
    assert shown.reasons == ["유효한 근거가 없어 검토가 필요합니다"]


def _opp(first: date, *, published: date | None = None, window: date | None = None) -> Opportunity:
    return Opportunity(first_seen_at=first, bid_published_at=published, bid_window_start=window)


def test_head_start_runs_to_the_actual_tender_when_there_is_one() -> None:
    opp = _opp(date(2025, 8, 9), published=date(2026, 9, 5), window=date(2026, 9, 5))
    assert head_start_days(opp, date(2026, 9, 25)) == 392


def test_head_start_runs_to_the_forecast_window_before_the_tender() -> None:
    assert head_start_days(_opp(date(2025, 11, 20), window=date(2026, 6, 1)), TODAY) == 193


def test_no_head_start_when_the_tender_was_the_first_signal_or_nothing_is_forecast() -> None:
    assert head_start_days(_opp(date(2026, 9, 5), published=date(2026, 9, 5)), TODAY) is None
    assert head_start_days(_opp(date(2026, 1, 5)), TODAY) is None


def test_head_start_follows_the_window_as_shown() -> None:
    first = date(2025, 6, 1)
    opened = Opportunity(
        first_seen_at=first,
        bid_published_at=None,
        bid_window_start=date(2026, 1, 15),
        bid_window_end=date(2026, 11, 30),
    )
    missed = Opportunity(
        first_seen_at=first,
        bid_published_at=None,
        bid_window_start=date(2026, 1, 15),
        bid_window_end=date(2026, 3, 31),
    )
    today = date(2026, 9, 25)
    assert head_start_days(opened, today) == (today - first).days  # counted to today, as shown
    assert head_start_days(missed, today) is None  # no head start to claim on a missed forecast


def test_a_window_that_has_opened_is_shown_from_today() -> None:
    from app.domain.timing import remaining_window

    today = date(2026, 9, 25)
    assert remaining_window(date(2026, 1, 1), date(2026, 11, 30), today) == (
        today,
        date(2026, 11, 30),
        False,
    )
    assert remaining_window(date(2026, 12, 1), date(2027, 2, 28), today)[0] == date(2026, 12, 1)
    assert remaining_window(date(2026, 1, 1), date(2026, 3, 31), today) == (
        date(2026, 1, 1),
        date(2026, 3, 31),
        True,
    )
    assert remaining_window(None, None, today) == (None, None, False)


def test_lead_days_stop_once_the_window_has_passed() -> None:
    from app.api.presenters import lead_days

    today = date(2026, 9, 25)
    opened = Opportunity(
        bid_published_at=None, bid_window_start=date(2026, 1, 1), bid_window_end=date(2026, 11, 30)
    )
    passed = Opportunity(
        bid_published_at=None, bid_window_start=date(2026, 1, 1), bid_window_end=date(2026, 3, 31)
    )
    assert lead_days(opened, today) == 0
    assert lead_days(passed, today) is None
