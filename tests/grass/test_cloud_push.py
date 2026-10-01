"""Smoke-test cloud packaging against the current GRASS interfaces."""

import shutil
from pathlib import Path

from itzi.cloud import push
from itzi.cloud.grass_utils import get_active_grass_params
from itzi.configreader import ConfigReader


def test_pack_input_from_grass(grass_5by5, test_data_path: str, monkeypatch) -> None:
    # The shared fixture is an unprojected XY location; cloud uploads require a CRS.
    monkeypatch.setattr(push, "validate_crs", lambda dataset: None)
    sim_config = ConfigReader(str(Path(test_data_path) / "5by5" / "5by5.ini")).sim_config
    grass_params = get_active_grass_params()
    assert grass_params is not None

    input_info = push.pack_input(sim_config, grass_params)
    try:
        assert input_info.dataset_path.is_file()
        assert input_info.dataset_bytes > 0
        assert input_info.domain_info.rows == 5
        assert input_info.domain_info.cols == 5
    finally:
        shutil.rmtree(input_info.dataset_path.parent)
