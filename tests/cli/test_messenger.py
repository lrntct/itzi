from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

import itzi.messenger as msgr


def test_fatal_raises_runtime_compatible_error(itzi_stderr):
    with pytest.raises(msgr.FatalError, match="expected failure") as error:
        msgr.fatal("expected failure")

    assert isinstance(error.value, RuntimeError)
    assert str(error.value) == "expected failure"
    assert itzi_stderr.getvalue().count("ERROR: expected failure") == 1


@pytest.mark.parametrize(("sim_seconds", "expected_eta"), [(0, "0:00:00"), (30, "0:00:10")])
def test_progress_uses_utc_wallclock(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    sim_seconds: int,
    expected_eta: str,
) -> None:
    start = datetime.min  # noqa: DTZ901 - core's relative-time sentinel.
    wallclock_start = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setenv("ITZI_VERBOSE", str(msgr.VerbosityLevel.MESSAGE))
    with patch("itzi.messenger.datetime") as clock:
        clock.now.return_value = wallclock_start + timedelta(seconds=10)
        msgr.percent(
            start,
            start + timedelta(minutes=1),
            start + timedelta(seconds=sim_seconds),
            wallclock_start,
        )
        clock.now.assert_called_once_with(UTC)

    stderr = capsys.readouterr().err
    assert f"Advance: {sim_seconds / 60:.2%}" in stderr
    assert f"ETA: {expected_eta}" in stderr
