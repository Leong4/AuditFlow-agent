from __future__ import annotations

import time
from typing import Any


AUDIT_SESSIONS: dict[str, dict[str, Any]] = {}


def create_session(audit_session_id: str, room_id: str, queries: list[str]) -> dict[str, Any]:
    session = {
        "room_id": room_id,
        "queries": [
            {
                "query_text": query_text,
                "query_id": None,
                "completed": False,
                "final_reply": None,
                "sent_at": None,
            }
            for query_text in queries
        ],
        "created_at": time.time(),
    }
    AUDIT_SESSIONS[audit_session_id] = session
    return session


def get_session(audit_session_id: str) -> dict[str, Any] | None:
    return AUDIT_SESSIONS.get(audit_session_id)


def find_query_by_query_id(query_id: str) -> tuple[str, int, dict[str, Any], dict[str, Any]] | None:
    for audit_session_id, session in AUDIT_SESSIONS.items():
        for index, query in enumerate(session["queries"]):
            if query.get("query_id") == query_id:
                return audit_session_id, index, session, query
    return None


def set_query_sent_at(audit_session_id: str, index: int, sent_at: float) -> None:
    session = AUDIT_SESSIONS[audit_session_id]
    session["queries"][index]["sent_at"] = sent_at


def set_query_id(audit_session_id: str, index: int, query_id: str) -> None:
    session = AUDIT_SESSIONS[audit_session_id]
    session["queries"][index]["query_id"] = query_id


def mark_query_completed(
    audit_session_id: str,
    index: int,
    final_reply: str | None,
) -> None:
    session = AUDIT_SESSIONS[audit_session_id]
    query = session["queries"][index]
    query["completed"] = True
    query["final_reply"] = final_reply
