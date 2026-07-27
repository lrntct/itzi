"""Tests for detecting parameters from an active GRASS session."""

import os
import sys
from types import ModuleType

import pytest

from itzi.cloud import grass_utils
from itzi.grass_session import GrassParams


def install_fake_grass(monkeypatch, gisenv) -> None:
    grass_module = ModuleType("grass")
    grass_module.__path__ = []
    script_module = ModuleType("grass.script")
    script_module.gisenv = gisenv
    grass_module.script = script_module

    monkeypatch.setattr(grass_utils.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setitem(sys.modules, "grass", grass_module)
    monkeypatch.setitem(sys.modules, "grass.script", script_module)


def create_grass_tree(tmp_path):
    grassdata = tmp_path / "grassdb"
    mapset = grassdata / "project" / "mapset"
    mapset.mkdir(parents=True)
    return grassdata


def grass_env(grassdata):
    return {
        "GISDBASE": str(grassdata),
        "LOCATION_NAME": "project",
        "MAPSET": "mapset",
    }


def test_get_active_grass_params_uses_gisenv_without_exported_values(monkeypatch, tmp_path):
    grassdata = create_grass_tree(tmp_path)
    calls = []
    for name in ("GISDBASE", "LOCATION_NAME", "MAPSET"):
        monkeypatch.delenv(name, raising=False)
    install_fake_grass(monkeypatch, lambda: calls.append(None) or grass_env(grassdata))

    result = grass_utils.get_active_grass_params()

    assert result == GrassParams(
        grassdata=str(grassdata),
        location="project",
        mapset="mapset",
        region=None,
        mask=None,
        grass_bin=None,
    )
    assert calls == [None]


def test_grass_package_unavailable(monkeypatch):
    monkeypatch.setattr(grass_utils.importlib.util, "find_spec", lambda name: None)

    assert grass_utils.get_active_grass_params() is None
    assert grass_utils.is_grass_session_active() is False


def test_get_active_grass_params_handles_runtime_query_failure(monkeypatch):
    def raise_runtime_error():
        raise RuntimeError("GRASS runtime is unavailable")

    install_fake_grass(monkeypatch, raise_runtime_error)

    assert grass_utils.get_active_grass_params() is None


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("GISDBASE", None),
        ("GISDBASE", ""),
        ("LOCATION_NAME", None),
        ("LOCATION_NAME", ""),
        ("MAPSET", None),
        ("MAPSET", ""),
    ],
)
def test_get_active_grass_params_rejects_missing_or_empty_values(
    monkeypatch, tmp_path, key, value
):
    values = grass_env(create_grass_tree(tmp_path))
    if value is None:
        values.pop(key)
    else:
        values[key] = value
    install_fake_grass(monkeypatch, lambda: values)

    assert grass_utils.get_active_grass_params() is None


@pytest.mark.parametrize("existing_level", ["none", "database", "location"])
def test_get_active_grass_params_rejects_missing_directories(
    monkeypatch, tmp_path, existing_level
):
    grassdata = tmp_path / "grassdb"
    if existing_level == "database":
        grassdata.mkdir()
    elif existing_level == "location":
        (grassdata / "project").mkdir(parents=True)
    install_fake_grass(monkeypatch, lambda: grass_env(grassdata))

    assert grass_utils.get_active_grass_params() is None


def test_get_active_grass_params_rejects_non_writable_mapset(monkeypatch, tmp_path):
    grassdata = create_grass_tree(tmp_path)
    mapset = grassdata / "project" / "mapset"
    install_fake_grass(monkeypatch, lambda: grass_env(grassdata))
    monkeypatch.setattr(
        grass_utils.os,
        "access",
        lambda path, mode: not (path == mapset and mode == os.W_OK),
    )

    assert grass_utils.get_active_grass_params() is None


def test_is_grass_session_active_delegates_to_parameter_detection(monkeypatch):
    params = GrassParams(grassdata="/db", location="project", mapset="mapset")
    monkeypatch.setattr(grass_utils, "get_active_grass_params", lambda: params)

    assert grass_utils.is_grass_session_active() is True
