from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

import itzi.messenger as msgr
from itzi.configreader import ConfigReader
from itzi.grass.session import GrassSessionManager
from itzi.simulation_runner import SimulationRunner


@pytest.mark.parametrize("verbosity", [0, 1, 2, 4])
@pytest.mark.parametrize("grass_verbosity", [-1, 0, 2])
@pytest.mark.usefixtures("grass_xy_session")
def test_gui_progress_protocol(verbosity: int, grass_verbosity: int) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from datetime import datetime, timedelta; "
            "from grass.lib.gis import G_verbose; "
            "from itzi.messenger import percent; "
            "original_verbosity = G_verbose(); "
            "start = datetime(1, 1, 1); "
            "percent(start, start + timedelta(seconds=1), "
            "start + timedelta(microseconds=250000), datetime.now()); "
            "percent(start, start + timedelta(seconds=1), "
            "start + timedelta(seconds=1), datetime.now()); "
            "assert G_verbose() == original_verbosity",
        ],
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(sys.path),
            "GRASS_MESSAGE_FORMAT": "gui",
            "GRASS_VERBOSE": str(grass_verbosity),
            "ITZI_VERBOSE": str(verbosity),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    if verbosity == 0:
        assert result.stderr == ""
    else:
        assert "GRASS_INFO_PERCENT: 25" in result.stderr
        assert "GRASS_INFO_PERCENT: 100" in result.stderr
        assert "Time:" not in result.stderr
        assert "Advance:" not in result.stderr


def test_run_reports_completion(
    grass_5by5: None, test_data_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = ConfigReader(str(Path(test_data_path) / "5by5" / "5by5.ini"))
    updates: list[tuple[datetime, datetime]] = []

    def capture_progress(
        start_time: datetime, end_time: datetime, sim_time: datetime, sim_start_time: datetime
    ) -> None:
        updates.append((sim_time, end_time))

    monkeypatch.setattr(msgr, "percent", capture_progress)
    with GrassSessionManager(config.grass_params):
        runner = SimulationRunner(config.sim_config).initialize()
        try:
            runner.run()
        finally:
            runner.finalize()

    assert updates
    assert updates[-1][0] == updates[-1][1]
