"""Test cloud project retrieval and display helpers."""

import json
from types import SimpleNamespace

from itzi.cloud import project, urls
from itzi.cloud.schemas import ProjectSchema, TeamSchema


class FakeSession:
    def __init__(self, response) -> None:
        self.response = response
        self.requests = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None

    def get(self, url, headers):
        self.requests.append((url, headers))
        return self.response


def test_get_projects_list_uses_session_token_and_validates_response(monkeypatch) -> None:
    response_data = [
        {
            "project_id": "proj-abc123",
            "name": "Flood Studies",
            "team": {"team_id": "team-xyz987", "name": "Hydrology"},
        }
    ]
    session = FakeSession(
        SimpleNamespace(status_code=200, reason="OK", text=json.dumps(response_data))
    )
    monkeypatch.setattr(project.requests, "Session", lambda: session)
    monkeypatch.setenv(urls.API_BASE_ENV_VAR, "https://example.test/")

    projects = project.get_projects_list("token-123")

    assert projects == [
        ProjectSchema(
            project_id="proj-abc123",
            name="Flood Studies",
            team=TeamSchema(team_id="team-xyz987", name="Hydrology"),
        )
    ]
    assert session.requests == [
        ("https://example.test/execution-api/v1/projects", {"X-Session-Token": "token-123"})
    ]
    assert urls.get_execution_api_base() == "https://example.test/execution-api/v1"


def test_display_projects_list_includes_project_details(monkeypatch) -> None:
    messages = []
    monkeypatch.setattr(project.msgr, "message", messages.append)

    project.display_projects_list(
        [
            ProjectSchema(
                project_id="proj-abc123",
                name="Flood Studies",
                team=TeamSchema(team_id="team-xyz987", name="Hydrology"),
            )
        ]
    )

    assert "PROJECT ID" in messages[0]
    assert "proj-abc123" in messages[1]
    assert "Flood Studies" in messages[1]


def test_display_projects_list_reports_empty_result(monkeypatch) -> None:
    messages = []
    monkeypatch.setattr(project.msgr, "message", messages.append)

    project.display_projects_list([])

    assert messages == ["No projects found."]
