from __future__ import annotations

import json
import re
from dataclasses import asdict
from enum import Enum
from typing import Any

from agents.rootcause.rules_handle import run_root_cause_agent
from backend.band_client import request_json, request_json_with_api_key, require_env
from shared.schemas import (
    Discrepancy,
    EntityConsistency,
    MatchedField,
    ReconciliationOutput,
)


SYSTEM_SENDERS = {
    "crm": "AuditFlow CRM",
    "erp": "AuditFlow ERP",
    "finance": "AuditFlow Finance",
}
SYSTEM_AMOUNT_FIELDS = {
    "crm": "contract_amount",
    "erp": "invoice_amount",
    "finance": "payment_amount",
}
SYSTEM_LABELS = {
    "crm": "CRM",
    "erp": "ERP",
    "finance": "Finance",
}
CURRENCY_SYMBOLS = {
    "GBP": "£",
    "USD": "$",
    "EUR": "€",
}


class ResultAssemblyError(RuntimeError):
    pass


def assemble_result(room_id: str, query_id: str) -> dict[str, Any]:
    system_outputs = _load_system_outputs(room_id, query_id)
    reconciliation, reconciliation_inserted_at = _load_reconciliation_output(room_id, query_id)
    rootcause = run_root_cause_agent(reconciliation)
    rootcause.query_id = query_id
    entity = reconciliation.entity or _first_entity(system_outputs)
    final_reply = _load_final_reply(room_id, query_id, entity, reconciliation_inserted_at)

    currency = _pick_currency(system_outputs)
    anomalies = rootcause.anomalies
    summary = rootcause.summary

    return {
        "entity": entity,
        "query_id": query_id,
        "status": _derive_status(anomalies, summary),
        "system_data": _assemble_system_data(system_outputs),
        "discrepancies": [
            {
                "field_pair": item.field_pair,
                "difference": _format_money(item.difference, currency),
                "direction": item.direction,
            }
            for item in reconciliation.discrepancies
        ],
        "root_cause": _assemble_root_cause(anomalies, reconciliation),
        "ai_analysis_text": _clean_final_reply(final_reply, query_id),
        "entity_consistency": _json_safe(reconciliation.entity_consistency),
        "matched": [_json_safe(item) for item in reconciliation.matched],
    }


def _load_system_outputs(room_id: str, query_id: str) -> dict[str, dict[str, Any]]:
    payload = request_json_with_api_key(
        "GET",
        f"/api/v1/agent/chats/{room_id}/messages?status=all",
        api_key=require_env("ROUTER_API_KEY"),
    )
    messages = _messages(payload)
    outputs: dict[str, dict[str, Any]] = {}
    for system, sender_name in SYSTEM_SENDERS.items():
        matches = [
            _extract_json_object(_message_content(message))
            for message in messages
            if message.get("sender_name") == sender_name
            and query_id in _message_content(message)
        ]
        matches = [
            item
            for item in matches
            if isinstance(item, dict)
            and item.get("query_id") == query_id
            and item.get("system") == system
        ]
        if matches:
            outputs[system] = matches[-1]

    if len(outputs) == len(SYSTEM_SENDERS):
        return outputs

    # Fallback for rooms where Router has already bundled the system outputs
    # into the message sent to Reconciliation.
    bundled = _load_bundled_system_outputs(room_id, query_id)
    outputs.update({key: value for key, value in bundled.items() if key not in outputs})
    missing = sorted(set(SYSTEM_SENDERS) - set(outputs))
    if missing:
        raise ResultAssemblyError(
            f"Missing system output(s) for query_id={query_id}: {', '.join(missing)}"
        )
    return outputs


def _load_bundled_system_outputs(room_id: str, query_id: str) -> dict[str, dict[str, Any]]:
    payload = request_json_with_api_key(
        "GET",
        f"/api/v1/agent/chats/{room_id}/messages?status=all",
        api_key=require_env("RECONCILIATION_API_KEY"),
    )
    outputs: dict[str, dict[str, Any]] = {}
    for message in _messages(payload):
        if message.get("sender_name") != "AuditFlow Router":
            continue
        content = _message_content(message)
        if query_id not in content:
            continue
        for label, payload_item in _extract_tagged_json(content).items():
            system = payload_item.get("system")
            if system in SYSTEM_SENDERS and payload_item.get("query_id") == query_id:
                outputs[str(system)] = payload_item
    return outputs


def _load_reconciliation_output(room_id: str, query_id: str) -> tuple[ReconciliationOutput, str]:
    payload = request_json_with_api_key(
        "GET",
        f"/api/v1/agent/chats/{room_id}/messages?status=all",
        api_key=require_env("ROOTCAUSE_API_KEY"),
    )
    matches = []
    for message in _messages(payload):
        if message.get("sender_name") != "AuditFlow Reconciliation":
            continue
        content = _message_content(message)
        if query_id not in content:
            continue
        item = _extract_json_object(content)
        if isinstance(item, dict) and item.get("query_id") == query_id:
            matches.append((_message_inserted_at(message), item))
    if not matches:
        raise ResultAssemblyError(f"Missing reconciliation output for query_id={query_id}")
    inserted_at, item = sorted(matches, key=lambda match: match[0])[-1]
    return _dict_to_reconciliation(item), inserted_at


def _load_final_reply(room_id: str, query_id: str, entity: str, after_inserted_at: str) -> str:
    status = request_json(
        "GET",
        f"/api/v1/agent/chats/{room_id}/messages?page=1&page_size=20&status=all",
    )
    matches = [
        _message_content(message)
        for message in _messages(status)
        if message.get("sender_name") == "AuditFlow RootCause"
        and query_id in _message_content(message)
    ]
    if matches:
        return matches[-1]

    # Older final replies may not include Query-ID. For those, fall back to a
    # natural-language RootCause message that either mentions the reconciled
    # entity or is the first RootCause reply after this query's reconciliation
    # payload was sent to RootCause.
    natural_language = []
    for message in _messages(status):
        content = _message_content(message)
        if message.get("sender_name") != "AuditFlow RootCause":
            continue
        if _strip_mentions(content).lstrip().startswith("{"):
            continue
        inserted_at = _message_inserted_at(message)
        if entity and entity in content:
            natural_language.append((inserted_at, content))
            continue
        if after_inserted_at and inserted_at > after_inserted_at:
            natural_language.append((inserted_at, content))
    if natural_language:
        return sorted(natural_language, key=lambda match: match[0])[0][1]
    raise ResultAssemblyError(f"Missing final RootCause reply for query_id={query_id}")


def _messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _message_content(message: dict[str, Any]) -> str:
    value = message.get("content")
    return str(value) if value is not None else ""


def _message_inserted_at(message: dict[str, Any]) -> str:
    value = message.get("inserted_at")
    return str(value) if value else ""


def _strip_mentions(content: str) -> str:
    return re.sub(r"@\[\[[^\]]+\]\]\s*", "", content).strip()


def _extract_json_object(content: str) -> dict[str, Any] | None:
    text = _strip_mentions(content)
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _extract_tagged_json(content: str) -> dict[str, dict[str, Any]]:
    matches: dict[str, dict[str, Any]] = {}
    for match in re.finditer(r"\[(AuditFlow CRM|AuditFlow ERP|AuditFlow Finance)\]:", content):
        label = match.group(1)
        payload = _extract_json_object(content[match.end():])
        if payload is not None:
            matches[label] = payload
    return matches


def _dict_to_reconciliation(payload: dict[str, Any]) -> ReconciliationOutput:
    entity_consistency = payload.get("entity_consistency")
    if isinstance(entity_consistency, dict):
        entity_consistency = EntityConsistency(**entity_consistency)
    else:
        entity_consistency = None

    discrepancies = [
        Discrepancy(**item)
        for item in payload.get("discrepancies", [])
        if isinstance(item, dict)
    ]
    matched = [
        MatchedField(**item)
        for item in payload.get("matched", [])
        if isinstance(item, dict)
    ]
    return ReconciliationOutput(
        entity=str(payload.get("entity", "")),
        entity_consistency=entity_consistency,
        discrepancies=discrepancies,
        matched=matched,
        error=payload.get("error"),
        query_id=str(payload.get("query_id", "")),
        reply_mode=str(payload.get("reply_mode", "user")),
    )


def _assemble_system_data(system_outputs: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    assembled = {}
    for system in ("crm", "erp", "finance"):
        output = system_outputs[system]
        amount_field = SYSTEM_AMOUNT_FIELDS[system]
        assembled[system] = {
            "label": SYSTEM_LABELS[system],
            "field": amount_field,
            "amount": output.get(amount_field),
            "currency": output.get("currency", "GBP"),
            "raw": output,
        }
    return assembled


def _assemble_root_cause(anomalies: list[Any], reconciliation: ReconciliationOutput) -> dict[str, Any]:
    if anomalies:
        first = anomalies[0]
        return {
            "probable_cause": _display_value(first.probable_cause),
            "evidence": list(first.evidence),
            "recommended_action": first.recommended_action,
        }

    evidence = [
        item.note
        for item in reconciliation.matched
        if item.note
    ][:3]
    if not evidence:
        evidence = ["No discrepancies were found by Reconciliation."]
    return {
        "probable_cause": "No discrepancy detected",
        "evidence": evidence,
        "recommended_action": "No action required.",
    }


def _derive_status(anomalies: list[Any], summary: Any) -> str:
    if anomalies:
        return "anomaly"
    if summary is not None and getattr(summary, "watch", 0) > 0:
        return "watch"
    return "normal"


def _clean_final_reply(content: str, query_id: str) -> str:
    text = _strip_mentions(content)
    text = re.sub(rf"(?im)^Query-ID:\s*{re.escape(query_id)}\s*\n?", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _format_money(value: Any, currency: str) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return str(value)
    symbol = CURRENCY_SYMBOLS.get(currency, f"{currency} ")
    if amount.is_integer():
        return f"{symbol}{amount:,.0f}"
    return f"{symbol}{amount:,.2f}"


def _pick_currency(system_outputs: dict[str, dict[str, Any]]) -> str:
    for output in system_outputs.values():
        currency = output.get("currency")
        if currency:
            return str(currency)
    return "GBP"


def _first_entity(system_outputs: dict[str, dict[str, Any]]) -> str:
    for output in system_outputs.values():
        entity = output.get("entity")
        if entity:
            return str(entity)
    return ""


def _display_value(value: Any) -> str:
    if isinstance(value, Enum):
        value = value.value
    return str(value).replace("_", " ")


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return _json_safe(asdict(value))
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    return value
