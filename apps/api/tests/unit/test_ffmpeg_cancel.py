"""A worker shutting down while ffmpeg finishes must still stop: on Python 3.11
``asyncio.wait_for`` returns the result instead of raising when the cancellation and the
result land in the same loop step, and the job then runs on instead of being handed back."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.media import ffmpeg


async def test_a_shutdown_as_the_process_ends_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate: asyncio.Future[tuple[bytes, bytes]] = asyncio.get_running_loop().create_future()

    class Proc:
        returncode: int | None = None

        async def communicate(self) -> tuple[bytes, bytes]:
            out = await gate
            self.returncode = 0
            return out

        def kill(self) -> None:
            self.returncode = -9

        async def wait(self) -> int | None:
            return self.returncode

    async def fake_exec(*_: Any, **__: Any) -> Proc:
        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    task = asyncio.create_task(ffmpeg.run(["ffprobe"], max_seconds=5))
    for _ in range(3):  # let run() reach the process
        await asyncio.sleep(0)
    gate.set_result((b"", b""))  # the process ends …
    task.cancel()  # … in the same step as the shutdown
    with pytest.raises(asyncio.CancelledError):
        await task
