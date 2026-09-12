from __future__ import annotations

from typing import Any

import requests

from .models import AttendanceEvent


class AttendanceClientError(RuntimeError):
    """The event could not be confirmed as recorded by the API."""


class AttendanceClient:
    """Stateless attendance sender. It deliberately has no retry or event store."""

    def __init__(self, url: str, bearer_token: str, timeout: float = 3.0):
        if not bearer_token:
            raise ValueError("AUTH_APP_BEARER_TOKEN is required")
        if timeout <= 0:
            raise ValueError("API timeout must be positive")
        self.url = url
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {bearer_token}", "Accept": "application/json"}
        )

    def send(self, event: AttendanceEvent) -> dict[str, Any]:
        try:
            response = self.session.post(
                self.url, json=event.payload(), timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise AttendanceClientError("attendance API connection failed") from exc

        if response.status_code not in (200, 201):
            raise AttendanceClientError(f"attendance API returned HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise AttendanceClientError("attendance API returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise AttendanceClientError("attendance API response must be an object")
        if body.get("eventId") != event.event_id:
            raise AttendanceClientError("attendance API returned a different eventId")
        if body.get("status") not in ("recorded", "duplicate"):
            raise AttendanceClientError("attendance API returned an invalid status")
        return body

    def close(self) -> None:
        self.session.close()
