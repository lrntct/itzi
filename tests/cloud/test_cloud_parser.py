"""Test cloud CLI parser and dispatch."""

import pytest

from itzi.cli_parser import build_parser
from itzi.itzi import main


def test_cloud_login_parser_accepts_options():
    args = build_parser().parse_args(
        ["cloud", "login", "--email", "user@example.com", "--password", "secret", "-s"]
    )

    assert args.command == "cloud"
    assert args.cloud_command == "login"
    assert args.email == "user@example.com"
    assert args.password == "secret"
    assert args.status is True
    assert args.logout is False


def test_cloud_push_parser_accepts_batch_and_flags():
    args = build_parser().parse_args(["cloud", "push", "-p", "42", "-f", "a.ini", "b.ini"])

    assert args.command == "cloud"
    assert args.cloud_command == "push"
    assert args.project == 42
    assert args.force is True
    assert args.config_file == ["a.ini", "b.ini"]


def test_cloud_push_parser_requires_project_id():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["cloud", "push", "sim.ini"])


def test_cloud_status_parser_accepts_optional_fingerprint():
    args = build_parser().parse_args(["cloud", "status", "fp-123"])

    assert args.command == "cloud"
    assert args.cloud_command == "status"
    assert args.fingerprint == "fp-123"


def test_cloud_pull_parser_accepts_overrides_and_overwrite():
    args = build_parser().parse_args(
        [
            "cloud",
            "pull",
            "fp-123",
            "--gisdb",
            "/db",
            "--project",
            "loc",
            "--mapset",
            "mapset",
            "-o",
        ]
    )

    assert args.command == "cloud"
    assert args.cloud_command == "pull"
    assert args.fingerprint == "fp-123"
    assert args.gisdb == "/db"
    assert args.project == "loc"
    assert args.mapset == "mapset"
    assert args.overwrite is True


def test_main_prints_cloud_help_without_subcommand(capsys):
    args = build_parser().parse_args(["cloud"])

    assert args.command == "cloud"
    assert args.cloud_command is None
    assert args.cloud_handler == "help"

    assert main(["cloud"]) is None

    captured = capsys.readouterr()
    assert "usage:" in captured.out
    assert " cloud [-h]" in captured.out
    assert "{login,push,status,pull}" in captured.out
    assert captured.err == ""


@pytest.mark.parametrize(
    ("argv", "expected_handler"),
    [
        (["cloud", "login"], "itzi_cloud_login"),
        (["cloud", "push", "-p", "42", "sim.ini"], "itzi_cloud_push"),
        (["cloud", "status"], "itzi_cloud_status"),
        (["cloud", "pull", "fp-123"], "itzi_cloud_pull"),
    ],
)
def test_main_dispatches_cloud_commands(monkeypatch, argv, expected_handler):
    calls = []

    for handler_name in [
        "itzi_cloud_login",
        "itzi_cloud_push",
        "itzi_cloud_status",
        "itzi_cloud_pull",
    ]:
        monkeypatch.setattr(
            f"itzi.itzi.{handler_name}",
            lambda args, _handler_name=handler_name: calls.append(_handler_name),
        )

    main(argv)

    assert calls == [expected_handler]
