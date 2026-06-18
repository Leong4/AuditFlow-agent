from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any

import requests


API_BASE = os.getenv("BAND_REST_URL", "https://app.thenvoi.com").rstrip("/")
TIMEOUT = 30
ROUTER_DISPLAY_NAME = "AuditFlow Router"
ROUTER_MENTION_PREFIX = f"@{ROUTER_DISPLAY_NAME} "
REPLY_MODE_AGENT_LINE = "Reply-Mode: agent"
BUSINESS_AGENT_ENV_VARS = [
    "ROUTER_AGENT_ID",
    "CRM_AGENT_ID",
    "ERP_AGENT_ID",
    "FINANCE_AGENT_ID",
    "RECONCILIATION_AGENT_ID",
    "ROOTCAUSE_AGENT_ID",
]


class BandClientError(RuntimeError):
    pass


class BandAPIError(BandClientError):
    def __init__(self, method: str, path: str, status_code: int, payload: dict[str, Any]) -> None:
        super().__init__(f"Band API {method} {path} failed with status {status_code}: {payload}")
        self.method = method
        self.path = path
        self.status_code = status_code
        self.payload = payload


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise BandClientError(f"Missing required environment variable: {name}")
    return value


def get_business_agent_ids() -> list[str]:
    return [require_env(name) for name in BUSINESS_AGENT_ENV_VARS]


def request_json(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    api_key = require_env("DEMO_USER_API_KEY")
    return request_json_with_api_key(method, path, api_key=api_key, body=body)


def request_json_with_api_key(
    method: str,
    path: str,
    *,
    api_key: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = requests.request(
        method,
        f"{API_BASE}{path}",
        headers={"X-API-Key": api_key, "Content-Type": "application/json"},
        json=body,
        timeout=TIMEOUT,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": response.text}
    if not (200 <= response.status_code < 300):
        raise BandAPIError(method, path, response.status_code, payload)
    return payload


def _parse_inserted_at(value: object) -> float | None:
    if not value:
        return None
    try:
        raw = str(value)
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


def data_id(payload: dict[str, Any]) -> str | None:
    data = payload.get("data")
    if isinstance(data, dict):
        value = data.get("id")
        return str(value) if value else None
    return None


def create_room() -> str:
    payload = request_json("POST", "/api/v1/agent/chats", body={"chat": {}})
    room_id = data_id(payload)
    if not room_id:
        raise BandClientError("create room response did not include data.id")
    return room_id


def add_participants(room_id: str, agent_ids: list[str]) -> None:
    for agent_id in agent_ids:
        body = {"participant": {"participant_id": agent_id, "role": "member"}}
        request_json(
            "POST",
            f"/api/v1/agent/chats/{room_id}/participants",
            body=body,
        )


def capture_query_id(room_id: str, sent_at: float) -> str | None:
    matches = [
        (record["inserted_at"], record["query_id"])
        for record in capture_router_query_records(room_id)
    ]
    candidates = [
        (inserted_at, query_id)
        for inserted_at, query_id in matches
        if inserted_at > sent_at
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def capture_router_query_records(room_id: str) -> list[dict[str, Any]]:
    payload = request_json_with_api_key(
        "GET",
        f"/api/v1/agent/chats/{room_id}/messages?status=all",
        api_key=require_env("CRM_API_KEY"),
    )
    data = payload.get("data")
    messages = data if isinstance(data, list) else []
    matches: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("sender_name") != ROUTER_DISPLAY_NAME:
            continue
        inserted_at = _parse_inserted_at(message.get("inserted_at"))
        if inserted_at is None:
            continue
        content = _message_content(message)
        match = re.search(r"Query-ID:\s*(audit_\w+)", content)
        if match:
            matches.append({
                "inserted_at": inserted_at,
                "query_id": match.group(1),
                "content": content,
            })
    return sorted(matches, key=lambda item: item["inserted_at"])


def capture_router_query_ids(room_id: str) -> list[tuple[float, str]]:
    return [
        (record["inserted_at"], record["query_id"])
        for record in capture_router_query_records(room_id)
    ]


def find_participant(payload: dict[str, Any], agent_id: str, fallback_name: str) -> dict[str, str]:
    data = payload.get("data")
    participants = data if isinstance(data, list) else []
    for participant in participants:
        if not isinstance(participant, dict):
            continue
        candidates = [
            participant.get("id"),
            participant.get("agent_id"),
            participant.get("participant_id"),
        ]
        nested = participant.get("participant")
        if isinstance(nested, dict):
            candidates.extend([nested.get("id"), nested.get("agent_id")])
        if agent_id in candidates:
            item = {"id": agent_id, "name": fallback_name}
            handle = participant.get("handle")
            name = participant.get("name")
            if isinstance(nested, dict):
                handle = handle or nested.get("handle")
                name = name or nested.get("name")
            if handle:
                item["handle"] = str(handle)
            if name:
                item["name"] = str(name)
            return item
    return {"id": agent_id, "name": fallback_name}


def send_message(room_id: str, content: str) -> dict[str, Any]:
    router_agent_id = require_env("ROUTER_AGENT_ID")
    participants_payload = request_json(
        "GET",
        f"/api/v1/agent/chats/{room_id}/participants",
    )
    router_mention = find_participant(participants_payload, router_agent_id, ROUTER_DISPLAY_NAME)
    message_content = content if content.startswith(ROUTER_MENTION_PREFIX) else f"{ROUTER_MENTION_PREFIX}{content}"
    if not re.search(r"(?im)^Reply-Mode:\s*agent\s*$", message_content):
        if message_content.startswith(ROUTER_MENTION_PREFIX):
            message_content = (
                f"@{ROUTER_DISPLAY_NAME}\n"
                f"{REPLY_MODE_AGENT_LINE}\n\n"
                f"{message_content[len(ROUTER_MENTION_PREFIX):]}"
            )
        else:
            message_content = f"{REPLY_MODE_AGENT_LINE}\n\n{message_content}"
    message_body = {"message": {"content": message_content, "mentions": [router_mention]}}
    return request_json(
        "POST",
        f"/api/v1/agent/chats/{room_id}/messages",
        body=message_body,
    )


def _message_inserted_at(message: dict[str, Any]) -> str:
    value = message.get("inserted_at")
    return str(value) if value else ""


def _message_content(message: dict[str, Any]) -> str:
    value = message.get("content")
    return str(value) if value is not None else ""


def _is_natural_language_reply(content: str) -> bool:
    stripped = content.strip()
    if stripped.startswith("{"):
        return False
    if stripped.startswith("@[["):
        marker_end = stripped.find("]]")
        if marker_end != -1:
            stripped = stripped[marker_end + 2 :].strip()
    return bool(stripped) and not stripped.startswith("{")


def get_rootcause_status(room_id: str, query_id: str) -> dict[str, str | bool | None]:
    payload = request_json(
        "GET",
        f"/api/v1/agent/chats/{room_id}/messages?page=1&page_size=20&status=all",
    )
    data = payload.get("data")
    messages = data if isinstance(data, list) else []
    rootcause_messages = [
        message
        for message in messages
        if isinstance(message, dict) and message.get("sender_name") == "AuditFlow RootCause"
    ]

    direct_matches = [message for message in rootcause_messages if query_id in _message_content(message)]
    if direct_matches:
        final_message = max(direct_matches, key=_message_inserted_at)
        return {
            "completed": True,
            "final_reply": _message_content(final_message),
            "completed_at": _message_inserted_at(final_message) or None,
        }

    # In Reply-Mode: agent, RootCause's final answer is delivered to the Demo User
    # inbox. Those final natural-language replies may omit query_id, so query_id is
    # only a direct-match fast path; otherwise the latest non-JSON RootCause message
    # in the Demo User inbox is the completion signal.
    natural_language_matches = [
        message
        for message in rootcause_messages
        if _is_natural_language_reply(_message_content(message))
    ]
    if natural_language_matches:
        final_message = max(natural_language_matches, key=_message_inserted_at)
        return {
            "completed": True,
            "final_reply": _message_content(final_message),
            "completed_at": _message_inserted_at(final_message) or None,
        }

    return {"completed": False, "final_reply": None, "completed_at": None}
