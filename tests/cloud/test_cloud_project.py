"""Test cloud project retrieval and display helpers."""

import json
from types import SimpleNamespace

from itzi.cloud import project
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
            "id": 42,
            "name": "Flood Studies",
            "slug": "flood-studies",
            "team": {"id": 7, "name": "Hydrology", "slug": "hydrology"},
        }
    ]
    session = FakeSession(
        SimpleNamespace(status_code=200, reason="OK", text=json.dumps(response_data))
    )
    monkeypatch.setattr(project.requests, "Session", lambda: session)

    projects = project.get_projects_list("token-123", url="https://example.test/projects")

    assert projects == [
        ProjectSchema(
            id=42,
            name="Flood Studies",
            slug="flood-studies",
            team=TeamSchema(id=7, name="Hydrology", slug="hydrology"),
        )
    ]
    assert session.requests == [
        ("https://example.test/projects", {"X-Session-Token": "token-123"})
    ]


def test_display_projects_list_includes_project_and_team_details(monkeypatch) -> None:
    messages = []
    monkeypatch.setattr(project.msgr, "message", messages.append)

    project.display_projects_list(
        [
            ProjectSchema(
                id=42,
                name="Flood Studies",
                slug="flood-studies",
                team=TeamSchema(id=7, name="Hydrology", slug="hydrology"),
            )
        ]
    )

    assert "PROJECT" in messages[0]
    assert "TEAM" in messages[0]
    assert "42" not in messages[0]
    assert "42" not in messages[1]
    assert "Flood Studies" in messages[1]
    assert "flood-studies" in messages[1]
    assert "Hydrology" in messages[1]
    assert "hydrology" in messages[1]


def test_display_projects_list_reports_empty_result(monkeypatch) -> None:
    messages = []
    monkeypatch.setattr(project.msgr, "message", messages.append)

    project.display_projects_list([])

    assert messages == ["No projects found."]
