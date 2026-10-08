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


def test_cloud_push_parser_accepts_yaml_batch():
    args = build_parser().parse_args(["cloud", "push", "-p", "proj-abc123", "a.yaml", "b.yaml"])

    assert args.command == "cloud"
    assert args.cloud_command == "push"
    assert args.project == "proj-abc123"
    assert args.config_file == ["a.yaml", "b.yaml"]


def test_cloud_push_parser_requires_project_id():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["cloud", "push", "sim.yaml"])


def test_cloud_push_parser_accepts_force() -> None:
    args = build_parser().parse_args(["cloud", "push", "-p", "project-id", "--force", "sim.yaml"])

    assert args.force is True


def test_cloud_push_help_requests_public_project_id(capsys):
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["cloud", "push", "--help"])

    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "--project PROJECT_ID" in help_text
    assert "Project ID (from cloud project --list)." in help_text
    assert ".yaml ensemble configuration files" in help_text


@pytest.mark.parametrize("ensemble_id", [None, "ensemble-123"])
def test_cloud_status_parser_accepts_optional_ensemble_id(ensemble_id: str | None) -> None:
    argv = ["cloud", "status"] + ([ensemble_id] if ensemble_id else [])
    args = build_parser().parse_args(argv)

    assert args.command == "cloud"
    assert args.cloud_command == "status"
    assert args.ensemble_id == ensemble_id
    assert not hasattr(args, "fingerprint")


def test_cloud_status_help_describes_local_selection(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["cloud", "status", "--help"])

    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "ENSEMBLE_ID" in help_text
    assert "locally recorded Ensemble ID" in help_text
    assert "defaults to submissions for this account" in " ".join(help_text.split())
    assert "fingerprint" not in help_text


def test_cloud_project_parser_accepts_list_option():
    args = build_parser().parse_args(["cloud", "project", "--list"])

    assert args.command == "cloud"
    assert args.cloud_command == "project"
    assert args.list is True


def test_cloud_project_parser_requires_an_action():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["cloud", "project"])


def test_cloud_pull_parser_accepts_run_selection_and_destination() -> None:
    args = build_parser().parse_args(
        [
            "cloud",
            "pull",
            "ensemble-123",
            "--run-id",
            "run-2",
            "--run-id",
            "run-1",
            "-o",
            "results.archive",
        ]
    )

    assert args.command == "cloud"
    assert args.cloud_command == "pull"
    assert args.ensemble_id == "ensemble-123"
    assert args.run_id == ["run-2", "run-1"]
    assert args.output == "results.archive"


def test_cloud_pull_parser_defaults_to_recorded_runs() -> None:
    args = build_parser().parse_args(
        ["cloud", "pull", "ensemble-123", "--output", "results.archive"]
    )
    assert args.run_id == []


@pytest.mark.parametrize("arguments", [[], ["ensemble-123"], ["--output", "results.archive"]])
def test_cloud_pull_requires_ensemble_and_destination(arguments: list[str]) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["cloud", "pull", *arguments])


@pytest.mark.parametrize("option", ["--gisdb", "--project", "--mapset", "--overwrite"])
def test_cloud_pull_rejects_legacy_import_options(option: str) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["cloud", "pull", "ensemble-123", "--output", "results.archive", option, "value"]
        )


def test_cloud_pull_help_describes_contract_gate(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["cloud", "pull", "--help"])
    assert exc_info.value.code == 0
    text = " ".join(capsys.readouterr().out.split())
    assert "GRASS import requires a published" in text
    assert "format and is not yet available" in text
    assert "--run-id" in text and "--output" in text
    assert "fingerprint" not in text


def test_main_prints_cloud_help_without_subcommand(capsys):
    args = build_parser().parse_args(["cloud"])

    assert args.command == "cloud"
    assert args.cloud_command is None
    assert args.cloud_handler == "help"

    assert main(["cloud"]) == 0

    captured = capsys.readouterr()
    assert "usage:" in captured.out
    assert " cloud [-h]" in captured.out
    assert "{login,push,status,project,pull}" in captured.out
    assert captured.err == ""


@pytest.mark.parametrize(
    ("argv", "expected_handler"),
    [
        (["cloud", "login"], "itzi_cloud_login"),
        (["cloud", "push", "-p", "proj-abc123", "sim.yaml"], "itzi_cloud_push"),
        (["cloud", "status"], "itzi_cloud_status"),
        (["cloud", "project", "--list"], "itzi_cloud_project"),
        (["cloud", "pull", "ensemble-123", "-o", "results.archive"], "itzi_cloud_pull"),
    ],
)
def test_main_dispatches_cloud_commands(monkeypatch, argv, expected_handler):
    calls = []

    for handler_name in [
        "itzi_cloud_login",
        "itzi_cloud_push",
        "itzi_cloud_status",
        "itzi_cloud_project",
        "itzi_cloud_pull",
    ]:
        monkeypatch.setattr(
            f"itzi.itzi.{handler_name}",
            lambda args, _handler_name=handler_name: calls.append(_handler_name),
        )

    main(argv)

    assert calls == [expected_handler]
