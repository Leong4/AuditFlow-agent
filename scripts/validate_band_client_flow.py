#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def print_json(label: str, payload: Any) -> None:
    print(f"{label}: {json.dumps(payload, ensure_ascii=False, indent=2)}")


def main() -> int:
    sys.path.insert(0, str(ROOT))
    load_env_file(ENV_PATH)

    from backend import band_client

    room_id = band_client.create_room()
    print(f"room_id: {room_id}")

    agent_ids = band_client.get_business_agent_ids()
    band_client.add_participants(room_id, agent_ids)
    print_json("add_participants", {"success": True, "agent_count": len(agent_ids)})

    participants_payload = band_client.request_json(
        "GET",
        f"/api/v1/agent/chats/{room_id}/participants",
    )
    router_agent_id = band_client.require_env("ROUTER_AGENT_ID")
    router_mention = band_client.find_participant(
        participants_payload,
        router_agent_id,
        band_client.ROUTER_DISPLAY_NAME,
    )
    preview_content = "Reconcile Acme Corp for Q1 2026"
    message_content = (
        preview_content
        if preview_content.startswith(band_client.ROUTER_MENTION_PREFIX)
        else f"{band_client.ROUTER_MENTION_PREFIX}{preview_content}"
    )
    print_json(
        "message_request_preview",
        {"message": {"content": message_content, "mentions": [router_mention]}},
    )

    send_response = band_client.send_message(room_id, preview_content)
    print_json("send_message_response", send_response)

    messages = band_client.get_room_messages(room_id)
    print_json("get_room_messages", messages)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
