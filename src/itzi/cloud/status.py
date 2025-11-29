"""
Copyright (C) 2025 Laurent Courty

This program is free software; you can redistribute it and/or
modify it under the terms of the GNU General Public License
as published by the Free Software Foundation; either version 2
of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
import json

import itzi.messenger as msgr
from itzi.cloud import urls

try:
    import requests
except ImportError:
    raise ImportError(
        "To use the cloud functionalities, install itzi with: "
        "'uv tool install itzi[cloud]' "
        "or 'pip install itzi[cloud]'"
    )


@dataclass
class SimulationTaskSchema:
    """Schema for simulation task status."""

    team: str
    created_on: datetime
    last_updated: datetime
    fingerprint: str
    status: str
    progress: float
    input_bytes: int
    results_bytes: int


def get_simulation_status(
    session_token: str, url: str = urls.STATUS_ENDPOINT
) -> list[SimulationTaskSchema]:
    """Get the status of all simulations for the authenticated user.

    Args:
        session_token: Authentication session token
        url: API endpoint URL for status retrieval

    Returns:
        List of SimulationTaskSchema objects containing simulation status
    """
    headers = {"X-Session-Token": session_token}

    with requests.Session() as session:
        response = session.get(url, headers=headers)

        if response.status_code != 200:
            msgr.fatal(
                f"Failed to retrieve simulation status. "
                f"Code: {response.status_code}. Reason: {response.reason}"
            )

        response_data = json.loads(response.text)

    # Parse the response and create SimulationTaskSchema objects
    # The API returns a list of tasks directly
    tasks = []
    for task_data in response_data:
        task = SimulationTaskSchema(
            team=task_data.get("team", ""),
            created_on=datetime.fromisoformat(task_data["created_on"]),
            last_updated=datetime.fromisoformat(task_data["last_updated"]),
            fingerprint=task_data["fingerprint"],
            status=task_data["status"],
            progress=task_data["progress"],
            input_bytes=task_data["input_bytes"],
            results_bytes=task_data["results_bytes"],
        )
        tasks.append(task)

    return tasks


def display_simulation_status(tasks: list[SimulationTaskSchema]) -> None:
    """Display simulation status in a table format similar to docker ps.

    Args:
        tasks: List of simulation tasks to display
    """
    if not tasks:
        msgr.message("No simulations found.")
        return

    # Helper function to format bytes
    def format_bytes(bytes_val: int) -> str:
        if bytes_val == 0:
            return "0B"
        for unit in ["B", "KB", "MB", "GB"]:
            if bytes_val < 1024.0:
                return f"{bytes_val:.1f}{unit}"
            bytes_val /= 1024.0
        return f"{bytes_val:.1f}TB"

    # Helper function to format relative time
    def format_time_ago(dt: datetime) -> str:
        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        delta = now - dt

        if delta.days > 0:
            return f"{delta.days} days ago"
        elif delta.seconds >= 3600:
            hours = delta.seconds // 3600
            return f"{hours} hours ago"
        elif delta.seconds >= 60:
            minutes = delta.seconds // 60
            return f"{minutes} minutes ago"
        else:
            return "just now"

    # Print header
    header = f"{'FINGERPRINT':<14} {'STATUS':<12} {'PROGRESS':<10} {'CREATED':<18} {'UPDATED':<18} {'TEAM':<15} {'INPUT SIZE':<12} {'RESULTS SIZE':<10}"
    msgr.message(header)

    # Print each task
    for task in tasks:
        fingerprint = task.fingerprint[:12]  # Show first 12 chars like docker
        status = task.status[:11]  # Truncate if needed
        progress = f"{task.progress / 10:.1f}%"  # received progress is in per mille
        created = format_time_ago(task.created_on)
        updated = format_time_ago(task.last_updated)
        team = task.team[:14] if task.team else "-"
        input_size = format_bytes(task.input_bytes)
        results_size = format_bytes(task.results_bytes)

        row = f"{fingerprint:<14} {status:<12} {progress:<10} {created:<18} {updated:<18} {team:<15} {input_size:<12} {results_size:<10}"
        msgr.message(row)
