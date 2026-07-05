"""Test cloud status display helpers."""

import os
import time
from datetime import UTC, datetime

from itzi.cloud.schemas import SimulationTaskSchema
from itzi.cloud.status import display_simulations_list


def test_display_simulations_list_uses_local_timestamps(monkeypatch) -> None:
    messages: list[str] = []
    monkeypatch.setattr("itzi.cloud.status.msgr.message", messages.append)

    previous_tz = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Paris"
    time.tzset()

    try:
        created_on = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
        last_updated = datetime(2025, 1, 15, 13, 30, 0, tzinfo=UTC)
        expected_created = created_on.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        expected_updated = last_updated.astimezone().strftime("%Y-%m-%d %H:%M:%S")

        display_simulations_list(
            [
                SimulationTaskSchema(
                    team="integration-tests",
                    created_on=created_on,
                    last_updated=last_updated,
                    fingerprint="fp-123",
                    status="completed",
                    progress=1000,
                    input_bytes=1024,
                    results_bytes=2048,
                )
            ]
        )
    finally:
        if previous_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_tz
        time.tzset()

    assert "CREATED" in messages[0]
    assert "UPDATED" in messages[0]
    assert expected_created in messages[1]
    assert expected_updated in messages[1]
