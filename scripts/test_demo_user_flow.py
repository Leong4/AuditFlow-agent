#!/usr/bin/env python3
"""
Temporary Plan B validation script for AuditFlow.

This is not bridge code. It verifies whether a dedicated Demo User agent can:
1. create a Band room,
2. add the six AuditFlow business agents,
3. send a mention message to Router,
4. observe local agent logs for room_added / processing evidence.

Credentials are read from environment variables. Do not hardcode API keys here.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
LOG_DIR = ROOT / ".logs"
API_BASE = os.getenv("BAND_REST_URL", "https://app.thenvoi.com").rstrip("/")
TIMEOUT = 30


BUSINESS_AGENTS = [
    ("router", "ROUTER_AGENT_ID", "AuditFlow Router", "router.log"),
    ("crm", "CRM_AGENT_ID", "AuditFlow CRM", "crm.log"),
    ("erp", "ERP_AGENT_ID", "AuditFlow ERP", "erp.log"),
    ("finance", "FINANCE_AGENT_ID", "AuditFlow Finance", "finance.log"),
    (
        "reconciliation",
        "RECONCILIATION_AGENT_ID",
        "AuditFlow Reconciliation",
        "reconciliation.log",
    ),
    ("rootcause", "ROOTCAUSE_AGENT_ID", "AuditFlow RootCause", "rootcause.log"),
]


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def api_key_prefix(value: str) -> str:
    parts = value.split("_")
    if len(parts) >= 3:
        return "_".join(parts[:3]) + "..."
    return value[:12] + "..."


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str) and (value.startswith("band_a_") or value.startswith("band_u_")):
        return api_key_prefix(value)
    return value


def request_json(
    method: str,
    path: str,
    api_key: str,
    *,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    url = f"{API_BASE}{path}"
    response = requests.request(
        method,
        url,
        headers={"X-API-Key": api_key, "Content-Type": "application/json"},
        json=body,
        timeout=TIMEOUT,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": response.text}
    return response.status_code, payload


def data_id(payload: dict[str, Any]) -> str | None:
    data = payload.get("data")
    if isinstance(data, dict):
        value = data.get("id")
        return str(value) if value else None
    return None


def snapshot_log_positions() -> dict[str, int]:
    positions: dict[str, int] = {}
    for _, _, _, log_name in BUSINESS_AGENTS:
        path = LOG_DIR / log_name
        positions[log_name] = path.stat().st_size if path.exists() else 0
    return positions


def read_log_delta(log_name: str, start: int) -> str:
    path = LOG_DIR / log_name
    if not path.exists():
        return ""
    with path.open("r", errors="replace") as handle:
        handle.seek(start)
        return handle.read()


def relevant_lines(delta: str, room_id: str, *, limit: int = 10) -> list[str]:
    lines = []
    for line in delta.splitlines():
        if room_id in line or "Acme Corp" in line or "audit_" in line or "query_systems" in line:
            lines.append(line)
    return lines[-limit:]


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


def print_json(label: str, payload: Any) -> None:
    print(f"{label}: {json.dumps(redact(payload), ensure_ascii=False, indent=2)}")


def main() -> int:
    demo_agent_id = require_env("DEMO_USER_AGENT_ID")
    demo_api_key = require_env("DEMO_USER_API_KEY")
    demo_handle = os.getenv("DEMO_USER_HANDLE", "")
    env_values = load_env_file(ENV_PATH)

    agents: list[dict[str, str]] = []
    missing: list[str] = []
    for key, env_name, display_name, log_name in BUSINESS_AGENTS:
        agent_id = env_values.get(env_name) or os.getenv(env_name)
        if not agent_id:
            missing.append(env_name)
            continue
        agents.append(
            {
                "key": key,
                "env": env_name,
                "id": agent_id,
                "name": display_name,
                "log": log_name,
            }
        )
    if missing:
        raise SystemExit(f"Missing business agent IDs: {', '.join(missing)}")

    print("# AuditFlow Demo User Flow Test")
    print(f"API base: {API_BASE}")
    print(f"Demo user agent id: {demo_agent_id}")
    print(f"Demo user handle: {demo_handle or '(not set)'}")
    print(f"Demo user api key: {api_key_prefix(demo_api_key)}")

    log_positions = snapshot_log_positions()

    print("\n## Step 1: Create room")
    status, payload = request_json("POST", "/api/v1/agent/chats", demo_api_key, body={"chat": {}})
    print(f"create_room_status: {status}")
    print_json("create_room_body", payload)
    if not (200 <= status < 300):
        return 1
    room_id = data_id(payload)
    if not room_id:
        print("ERROR: create room response did not include data.id")
        return 1
    print(f"room_id: {room_id}")

    print("\n## Step 2: Add business agents")
    add_results: list[dict[str, Any]] = []
    for agent in agents:
        body = {"participant": {"participant_id": agent["id"], "role": "member"}}
        status, payload = request_json(
            "POST",
            f"/api/v1/agent/chats/{room_id}/participants",
            demo_api_key,
            body=body,
        )
        result = {
            "agent": agent["key"],
            "name": agent["name"],
            "agent_id": agent["id"],
            "status": status,
            "body": payload,
        }
        add_results.append(result)
        print_json(f"add_{agent['key']}", result)

    add_failures = [result for result in add_results if not (200 <= int(result["status"]) < 300)]
    if add_failures:
        print("\nERROR: at least one participant add failed; stopping before send.")
        return 1

    print("\n## Step 3: Wait for room_added/log sync")
    time.sleep(8)
    log_hits: dict[str, list[str]] = {}
    for agent in agents:
        delta = read_log_delta(agent["log"], log_positions.get(agent["log"], 0))
        hits = relevant_lines(delta, room_id, limit=6)
        log_hits[agent["key"]] = hits
        print_json(f"log_hits_{agent['key']}", hits)

    print("\n## Step 4: Resolve Router participant and send message")
    status, participants_payload = request_json(
        "GET",
        f"/api/v1/agent/chats/{room_id}/participants",
        demo_api_key,
    )
    print(f"list_participants_status: {status}")
    if not (200 <= status < 300):
        print_json("list_participants_body", participants_payload)
        return 1

    router = next(agent for agent in agents if agent["key"] == "router")
    router_mention = find_participant(participants_payload, router["id"], router["name"])
    content = "@AuditFlow Router Reconcile Acme Corp for Q1 2026"
    message_body = {"message": {"content": content, "mentions": [router_mention]}}
    print_json("message_request", message_body)
    status, message_payload = request_json(
        "POST",
        f"/api/v1/agent/chats/{room_id}/messages",
        demo_api_key,
        body=message_body,
    )
    print(f"send_message_status: {status}")
    print_json("send_message_body", message_payload)
    if not (200 <= status < 300):
        return 1

    print("\n## Step 5: Wait for Router processing evidence")
    router_log_start = (LOG_DIR / "router.log").stat().st_size if (LOG_DIR / "router.log").exists() else 0
    time.sleep(20)
    router_delta = read_log_delta("router.log", router_log_start)
    router_hits = relevant_lines(router_delta, room_id, limit=10)
    print_json("router_processing_hits", router_hits)

    status, messages_payload = request_json(
        "GET",
        f"/api/v1/agent/chats/{room_id}/messages?page=1&page_size=20&status=all",
        demo_api_key,
    )
    print(f"list_messages_status: {status}")
    if 200 <= status < 300:
        data = messages_payload.get("data")
        message_count = len(data) if isinstance(data, list) else None
        print_json("list_messages_summary", {"message_count": message_count, "metadata": messages_payload.get("metadata")})
    else:
        print_json("list_messages_body", messages_payload)

    if router_hits:
        print("\nRESULT: room creation, participant adds, message send, and Router log processing were observed.")
        return 0

    print("\nRESULT: message was sent, but Router processing evidence was not observed in logs.")
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except requests.RequestException as exc:
        print(f"HTTP_ERROR: {exc}")
        raise SystemExit(1)
