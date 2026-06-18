from __future__ import annotations

import time
import uuid
import re
from typing import Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from backend import band_client, store
from backend.result_assembler import ResultAssemblyError, assemble_result


app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


class QueryRequest(BaseModel):
    queries: list[str] = Field(min_length=1, max_length=3)


def _query_status(query: dict) -> Literal["pending", "processing", "done"]:
    if query.get("completed"):
        return "done"
    if query.get("query_id"):
        return "processing"
    return "pending"


def _extract_reconcile_target(query_text: str) -> tuple[str, str] | None:
    match = re.search(r"(?i)\breconcile\s+(.+?)\s+for\s+(.+)$", query_text.strip())
    if not match:
        return None
    return match.group(1).strip(), match.group(2).strip()


def _router_record_matches_query(record: dict, query_text: str) -> bool:
    target = _extract_reconcile_target(query_text)
    if target is None:
        return False
    entity, time_scope = target
    content = str(record.get("content", ""))
    return (
        re.search(rf"(?im)^Entity:\s*{re.escape(entity)}\s*$", content) is not None
        and re.search(rf"(?im)^Time scope:\s*{re.escape(time_scope)}\s*$", content) is not None
    )


def _try_capture_missing_query_ids(audit_session_id: str, session: dict) -> None:
    room_id = session["room_id"]
    router_records = band_client.capture_router_query_records(room_id)
    seen_query_ids = {
        query.get("query_id") for query in session["queries"] if query.get("query_id")
    }

    for index, query in enumerate(session["queries"]):
        if query.get("query_id"):
            continue
        for record in router_records:
            query_id = str(record["query_id"])
            if query_id in seen_query_ids:
                continue
            if _router_record_matches_query(record, str(query["query_text"])):
                store.set_query_id(audit_session_id, index, query_id)
                seen_query_ids.add(query_id)
                break

    for index, query in enumerate(session["queries"]):
        if query.get("query_id") or query.get("sent_at") is None:
            continue
        sent_at = float(query["sent_at"])
        next_sent_at = None
        for later_query in session["queries"][index + 1 :]:
            if later_query.get("sent_at") is not None:
                next_sent_at = float(later_query["sent_at"])
                break

        candidates = [
            (record["inserted_at"], record["query_id"])
            for record in router_records
            if record["query_id"] not in seen_query_ids
            and record["inserted_at"] > sent_at
            and (next_sent_at is None or record["inserted_at"] < next_sent_at)
        ]
        if candidates:
            store.set_query_id(audit_session_id, index, candidates[0][1])
            seen_query_ids.add(candidates[0][1])

    # If the final query's Router message arrived after all sent_at timestamps,
    # the interval logic above captures it. This fallback covers delayed earlier
    # captures without assigning duplicate query IDs.
    for index, query in enumerate(session["queries"]):
        query = session["queries"][index]
        if query.get("query_id") or query.get("sent_at") is None:
            continue
        query_id = band_client.capture_query_id(room_id, float(query["sent_at"]))
        if query_id and query_id not in seen_query_ids:
            store.set_query_id(audit_session_id, index, query_id)
            seen_query_ids.add(query_id)


def _run_query_submission(audit_session_id: str, queries: list[str]) -> None:
    session = store.get_session(audit_session_id)
    if session is None:
        return

    room_id = session["room_id"]
    for index, query_text in enumerate(queries):
        sent_at = time.time()
        band_client.send_message(room_id, query_text)
        store.set_query_sent_at(audit_session_id, index, sent_at)
        if index < len(queries) - 1:
            time.sleep(1)

    time.sleep(10)
    session = store.get_session(audit_session_id)
    if session is not None:
        _try_capture_missing_query_ids(audit_session_id, session)


@app.post("/api/queries")
def create_queries(
    payload: QueryRequest,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    queries = [query.strip() for query in payload.queries if query.strip()]
    if not 1 <= len(queries) <= 3:
        raise HTTPException(status_code=422, detail="queries must include 1 to 3 non-empty strings")

    room_id = band_client.create_room()
    band_client.add_participants(room_id, band_client.get_business_agent_ids())
    audit_session_id = str(uuid.uuid4())
    store.create_session(audit_session_id, room_id, queries)
    background_tasks.add_task(_run_query_submission, audit_session_id, queries)
    return {"audit_session_id": audit_session_id, "room_id": room_id}


@app.get("/api/queries/{audit_session_id}")
def get_queries(audit_session_id: str) -> dict:
    session = store.get_session(audit_session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="audit session not found")

    _try_capture_missing_query_ids(audit_session_id, session)
    for index, query in enumerate(session["queries"]):
        if query.get("completed") or not query.get("query_id"):
            continue
        status = band_client.get_rootcause_status(session["room_id"], str(query["query_id"]))
        if status.get("completed"):
            store.mark_query_completed(
                audit_session_id,
                index,
                status.get("final_reply") if isinstance(status.get("final_reply"), str) else None,
            )

    return {
        "audit_session_id": audit_session_id,
        "room_id": session["room_id"],
        "queries": [
            {
                "query_text": query["query_text"],
                "query_id": query.get("query_id"),
                "status": _query_status(query),
            }
            for query in session["queries"]
        ],
    }


@app.get("/api/queries/{query_id}/result")
def get_query_result(query_id: str) -> dict:
    match = store.find_query_by_query_id(query_id)
    if match is None:
        raise HTTPException(status_code=404, detail="query_id not found")

    audit_session_id, index, session, query = match
    if not query.get("completed"):
        status = band_client.get_rootcause_status(session["room_id"], query_id)
        if status.get("completed"):
            store.mark_query_completed(
                audit_session_id,
                index,
                status.get("final_reply") if isinstance(status.get("final_reply"), str) else None,
            )
            query = session["queries"][index]

    if not query.get("completed"):
        raise HTTPException(
            status_code=425,
            detail=f"result not ready yet, status={_query_status(query)}",
        )

    try:
        return assemble_result(session["room_id"], query_id)
    except ResultAssemblyError as exc:
        raise HTTPException(
            status_code=425,
            detail=f"result not ready yet, status=processing, reason={exc}",
        ) from exc


@app.get("/api/queries/{audit_session_id}/raw")
def get_queries_raw(audit_session_id: str) -> dict:
    session = store.get_session(audit_session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="audit session not found")
    return {"audit_session_id": audit_session_id, **session}
