"""The static demo's file names (the browser builds the same ones, apps/web/src/lib/demo/fetch.ts)."""

from app.demo.snapshot import file_key


def test_file_key_matches_the_browser_side() -> None:
    assert file_key("/api/me", {}) == "me"
    assert (
        file_key("/api/admin/jobs", {"status": "failed", "limit": 100})
        == "admin_jobs_limit_100_status_failed"
    )
    assert file_key("/api/admin/llm/usage", {"days": 7}) == "admin_llm_usage_days_7"
