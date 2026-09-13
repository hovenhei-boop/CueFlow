from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from starlette.testclient import TestClient
from trial_helpers import trial_config

from cueflow.trial_http import VISITOR_COOKIE, create_trial_app


@dataclass(frozen=True)
class Ready:
    ready: bool = True
    reasons: tuple[str, ...] = ()


class FakeService:
    def __init__(self, config: Any) -> None:
        self.config = config
        self.controls: list[tuple[str, str, int | None]] = []
        self.submissions: list[dict[str, Any]] = []

    def sweep(self) -> dict[str, Any]:
        return {}

    def refresh_storage_readiness(self) -> Ready:
        return Ready()

    def close(self) -> None:
        return None

    def heartbeat(self, visitor_id: str, **_: Any) -> dict[str, Any]:
        return {"visitor_id": visitor_id, "online_now": 1}

    def jobs(self, visitor_id: str) -> list[dict[str, Any]]:
        return []

    def submit(self, **value: Any) -> dict[str, Any]:
        assert value["media_path"].read_bytes() == b"media-bytes"
        assert value["references"][0].read_bytes() == b"reference"
        self.submissions.append(value)
        return {"job_id": value["job_id"], "status": "queued"}

    def job(self, job_id: str, visitor_id: str) -> dict[str, Any]:
        return {"job_id": job_id, "visitor_id": visitor_id}

    def result_url(self, job_id: str, visitor_id: str) -> str:
        return "https://objects.example/exact?signature=test"

    def summary(self) -> dict[str, Any]:
        return {"online_now": 1}

    def list_requests(self, *, limit: int) -> list[dict[str, Any]]:
        return [{"limit": limit}]

    def append_control(
        self, action: str, *, reason: str, daily_budget_micros: int | None = None
    ) -> dict[str, Any]:
        self.controls.append((action, reason, daily_budget_micros))
        return {"action": action}


def test_cookie_origin_operator_and_result_redirect_contract(tmp_path: Path) -> None:
    config = trial_config(tmp_path)
    service = FakeService(config)
    app = create_trial_app(service=service, config=config)  # type: ignore[arg-type]
    with TestClient(app, base_url=config.public_origin) as client:
        denied = client.post("/trial/heartbeat")
        assert denied.status_code == 403

        heartbeat = client.post("/trial/heartbeat", headers={"Origin": config.public_origin})
        assert heartbeat.status_code == 200
        cookie = heartbeat.cookies.get(VISITOR_COOKIE)
        assert cookie is not None
        assert "HttpOnly" in heartbeat.headers["set-cookie"]
        assert "Secure" in heartbeat.headers["set-cookie"]
        assert heartbeat.headers["cache-control"] == "no-store"

        assert client.get("/trial/admin/summary").status_code == 401
        admin = client.get(
            "/trial/admin/summary",
            headers={"Authorization": "Bearer " + config.operator_secret},
        )
        assert admin.json() == {"online_now": 1}

        redirect = client.get("/trial/jobs/job_test/result", follow_redirects=False)
        assert redirect.status_code == 302
        assert redirect.headers["location"].startswith("https://objects.example/exact")
        assert redirect.headers["cache-control"] == "no-store"


def test_operator_control_requires_origin_reason_and_records_budget(tmp_path: Path) -> None:
    config = trial_config(tmp_path)
    service = FakeService(config)
    app = create_trial_app(service=service, config=config)  # type: ignore[arg-type]
    headers = {
        "Origin": config.public_origin,
        "Authorization": "Bearer " + config.operator_secret,
    }
    with TestClient(app, base_url=config.public_origin) as client:
        assert client.post("/trial/admin/pause", headers=headers, json={}).status_code == 400
        response = client.post(
            "/trial/admin/daily-budget-override",
            headers=headers,
            json={"reason": "control spend", "daily_budget_micros": 12_000_000},
        )
        assert response.status_code == 200
        assert service.controls[-1] == (
            "set_daily_budget_override", "control spend", 12_000_000
        )


def test_multipart_upload_uses_server_generated_workspace_and_repeated_inputs(
    tmp_path: Path,
) -> None:
    config = trial_config(tmp_path)
    service = FakeService(config)
    app = create_trial_app(service=service, config=config)  # type: ignore[arg-type]
    with TestClient(app, base_url=config.public_origin) as client:
        response = client.post(
            "/trial/jobs",
            headers={
                "Origin": config.public_origin,
                "X-CueFlow-Fingerprint": "browser|os|zh-CN|UTC+8|large",
            },
            files=[
                ("media", ("untrusted/../../clip.wav", b"media-bytes", "audio/wav")),
                ("references", ("notes.txt", b"reference", "text/plain")),
            ],
            data={"keywords": ["CueFlow", "嘉宾姓名"]},
        )
        assert response.status_code == 202, response.text
        submitted = service.submissions[0]
        assert submitted["job_id"].startswith("job_")
        assert submitted["request_id"].startswith("req_")
        assert submitted["media_path"].name == "media.wav"
        assert submitted["media_path"].resolve().is_relative_to(config.work_root)
        assert submitted["keywords"] == ["CueFlow", "嘉宾姓名"]
        assert submitted["ip_hmac"].startswith("hmac-sha256:")
        assert "browser|os" not in submitted["fingerprint_hmac"]
