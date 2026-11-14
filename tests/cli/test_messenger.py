from datetime import datetime, timedelta

import pytest

import itzi.messenger as msgr


def test_fatal_raises_runtime_compatible_error(itzi_stderr):
    with pytest.raises(msgr.FatalError, match="expected failure") as error:
        msgr.fatal("expected failure")

    assert isinstance(error.value, RuntimeError)
    assert str(error.value) == "expected failure"
    assert itzi_stderr.getvalue().count("ERROR: expected failure") == 1


@pytest.mark.parametrize("verbosity", [0, 1, 2])
def test_terminal_progress(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], verbosity: int
) -> None:
    monkeypatch.delenv("GRASS_MESSAGE_FORMAT", raising=False)
    monkeypatch.setenv("ITZI_VERBOSE", str(verbosity))
    start = datetime(1, 1, 1)
    msgr.percent(
        start, start + timedelta(seconds=100), start + timedelta(seconds=25), datetime.now()
    )
    output = capsys.readouterr().err
    if verbosity == 0:
        assert output == ""
    elif verbosity == 1:
        assert output == "25.00%\r"
    else:
        assert "Time: 0001-01-01 00:00:25 Advance: 25.00% ETA:" in output
        assert output.endswith("\r")
