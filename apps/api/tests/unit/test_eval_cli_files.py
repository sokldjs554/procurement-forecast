from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from app.cli import app


@pytest.mark.parametrize("command", ["holdout", "longitudinal"])
@pytest.mark.parametrize("alias", [False, True])
def test_evaluation_cannot_overwrite_input_or_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, alias: bool
) -> None:
    original = b"frozen evidence must survive"
    source = tmp_path / "frozen.jsonl"
    source.write_bytes(original)
    destination = source
    if alias:
        destination = tmp_path / "alias.jsonl"
        destination.symlink_to(source)

    async def holdout(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"evaluated": True}

    def longitudinal(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"evaluated": True}

    monkeypatch.setattr("app.eval.holdout.evaluate_holdout", holdout)
    monkeypatch.setattr("app.eval.longitudinal.evaluate_longitudinal", longitudinal)
    args = ["eval", command, str(source), "--out", str(destination)]
    if command == "holdout":
        args += ["--code-revision", "test"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code != 0
    assert source.read_bytes() == original
