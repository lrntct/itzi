"""Test cloud CLI helpers."""

import argparse

import pytest

from itzi.cloud.cli import resolve_cloud_pull_grass_params
from itzi.data_containers import GrassParams
from itzi.itzi_error import ItziFatal


def test_resolve_cloud_pull_grass_params_prefers_active_session(monkeypatch):
    session_params = GrassParams(grassdata="/db", location="loc", mapset="mapset")
    monkeypatch.setattr("itzi.cloud.grass_utils.get_active_grass_params", lambda: session_params)
    monkeypatch.setattr(
        "itzi.cloud.metadata_storage.load_simulation_metadata",
        lambda fingerprint: pytest.fail("metadata should not be consulted"),
    )

    grass_params, source = resolve_cloud_pull_grass_params(
        argparse.Namespace(
            fingerprint="fp-123",
            gisdb=None,
            project=None,
            mapset=None,
        )
    )

    assert grass_params == session_params
    assert source == "active GRASS session"


def test_resolve_cloud_pull_grass_params_uses_cli_args_when_complete(monkeypatch):
    monkeypatch.setattr("itzi.cloud.grass_utils.get_active_grass_params", lambda: None)
    monkeypatch.setattr(
        "itzi.cloud.metadata_storage.load_simulation_metadata",
        lambda fingerprint: pytest.fail("metadata should not be consulted"),
    )

    grass_params, source = resolve_cloud_pull_grass_params(
        argparse.Namespace(
            fingerprint="fp-123",
            gisdb="/db",
            project="loc",
            mapset="mapset",
        )
    )

    assert grass_params == GrassParams(
        grassdata="/db",
        location="loc",
        mapset="mapset",
        region=None,
        mask=None,
        grass_bin=None,
    )
    assert source == "CLI arguments"


def test_resolve_cloud_pull_grass_params_falls_back_to_metadata(monkeypatch):
    metadata_params = GrassParams(grassdata="/meta", location="proj", mapset="ms")
    monkeypatch.setattr("itzi.cloud.grass_utils.get_active_grass_params", lambda: None)
    monkeypatch.setattr(
        "itzi.cloud.metadata_storage.load_simulation_metadata",
        lambda fingerprint: metadata_params,
    )

    grass_params, source = resolve_cloud_pull_grass_params(
        argparse.Namespace(
            fingerprint="fp-123",
            gisdb=None,
            project=None,
            mapset=None,
        )
    )

    assert grass_params == metadata_params
    assert source == "stored metadata"


def test_resolve_cloud_pull_grass_params_rejects_partial_cli_override(monkeypatch):
    monkeypatch.setattr("itzi.cloud.grass_utils.get_active_grass_params", lambda: None)
    monkeypatch.setattr(
        "itzi.cloud.cli.msgr.fatal",
        lambda message: (_ for _ in ()).throw(ItziFatal(message)),
    )

    with pytest.raises(ItziFatal, match="all three are required"):
        resolve_cloud_pull_grass_params(
            argparse.Namespace(
                fingerprint="fp-123",
                gisdb="/db",
                project="loc",
                mapset=None,
            )
        )


def test_resolve_cloud_pull_grass_params_requires_any_source(monkeypatch):
    monkeypatch.setattr("itzi.cloud.grass_utils.get_active_grass_params", lambda: None)
    monkeypatch.setattr(
        "itzi.cloud.metadata_storage.load_simulation_metadata",
        lambda fingerprint: None,
    )
    monkeypatch.setattr(
        "itzi.cloud.cli.msgr.fatal",
        lambda message: (_ for _ in ()).throw(ItziFatal(message)),
    )

    with pytest.raises(ItziFatal, match="Could not determine GRASS parameters"):
        resolve_cloud_pull_grass_params(
            argparse.Namespace(
                fingerprint="fp-123",
                gisdb=None,
                project=None,
                mapset=None,
            )
        )
