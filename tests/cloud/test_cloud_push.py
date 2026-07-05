from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
import shutil
import sys
import types

import pytest

from itzi.const import TemporalType
from itzi.data_containers import GrassParams, SimulationConfig, SurfaceFlowParameters


@pytest.mark.cloud
def test_pack_input_produces_stable_hash_for_identical_inputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from itzi.cloud import push

    class FakeGrassInterface:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.yr = 3
            self.xr = 4
            self.dx = 5.0
            self.dy = 6.0

    fake_grass_interface_module = types.ModuleType("itzi.providers.grass_interface")
    fake_grass_interface_module.GrassInterface = FakeGrassInterface
    monkeypatch.setitem(sys.modules, "itzi.providers.grass_interface", fake_grass_interface_module)
    monkeypatch.setattr(push, "GrassSessionManager", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(
        push,
        "list_input_maps",
        lambda *_args, **_kwargs: {"mapset": {"raster": ["dem@mapset"], "strds": []}},
    )

    def fake_to_zarr(*_args: object, tempdir: Path, **_kwargs: object) -> None:
        tempdir.mkdir()
        (tempdir / "attrs.json").write_text("{}")
        (tempdir / "variables").mkdir()
        (tempdir / "variables" / "dem").write_text("placeholder")

    monkeypatch.setattr(push, "to_zarr", fake_to_zarr)

    sim_config = SimulationConfig(
        start_time=datetime(2025, 1, 1, 12, tzinfo=UTC),
        end_time=datetime(2025, 1, 1, 13, tzinfo=UTC),
        record_step=timedelta(minutes=15),
        temporal_type=TemporalType.ABSOLUTE,
        input_map_names={"dem": "dem@PERMANENT"},
        output_map_names={"h": "depth@PERMANENT"},
        surface_flow_parameters=SurfaceFlowParameters(),
    )
    grass_params = GrassParams(
        grassdata=str(tmp_path / "grassdb"),
        location="project",
        mapset="mapset",
    )

    first_input_info = push.pack_input(sim_config, grass_params)
    second_input_info = push.pack_input(sim_config, grass_params)

    try:
        assert first_input_info.dataset_path != second_input_info.dataset_path
        assert first_input_info.dataset_hash == second_input_info.dataset_hash
        assert first_input_info.dataset_bytes == second_input_info.dataset_bytes
        assert first_input_info.domain_info == second_input_info.domain_info
        assert first_input_info.sim_config == second_input_info.sim_config
        assert first_input_info.sim_config.input_map_names == {"dem": "dem"}
        assert first_input_info.sim_config.output_map_names == {"h": "depth"}
    finally:
        shutil.rmtree(first_input_info.dataset_path.parent, ignore_errors=True)
        shutil.rmtree(second_input_info.dataset_path.parent, ignore_errors=True)
