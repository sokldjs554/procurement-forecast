from typer.testing import CliRunner

from app.cli import app


def test_apply_requires_digest_before_connecting_to_database() -> None:
    result = CliRunner().invoke(app, ["pipeline", "revalidate", "--apply"])
    assert result.exit_code == 2
    assert "must be supplied together" in result.output


def test_digest_without_apply_is_not_silently_ignored() -> None:
    result = CliRunner().invoke(app, ["pipeline", "revalidate", "--expected-digest", "abc"])
    assert result.exit_code == 2
    assert "must be supplied together" in result.output


def test_cli_remains_available_with_new_command() -> None:
    assert CliRunner().invoke(app, ["pipeline", "revalidate", "--help"]).exit_code == 0
    assert CliRunner().invoke(app, ["db", "upgrade", "--help"]).exit_code == 0
