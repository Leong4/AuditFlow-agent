# Core logic for the Reconciliation Agent.
# Responsibility boundary: only detects whether cross-system fields are consistent
# or discrepant; it does not explain discrepancy causes.
# Root-Cause Agent handles cause analysis and risk judgment later.

from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import asdict, fields, is_dataclass
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

from dotenv import load_dotenv
from pydantic_ai import RunContext
from thenvoi import Agent
from thenvoi.core.protocols import AgentToolsProtocol
from thenvoi.core.types import PlatformMessage

# ── Path setup ──────────────────────────────────────────────
# Ensure Python can find the shared/ directory.
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from shared.schemas import (  # noqa: E402
    CRMOutput,
    ERPOutput,
    FinanceOutput,
    ReconciliationOutput,
    Discrepancy,
    MatchedField,
    EntityConsistency,
    EntityMatch,
    MatchMethod,
)

from shared.trace import AuditTrace, TraceStep, add_step  # noqa: E402
from shared.thenvoi_pydantic_compat import PydanticAIAdapter  # noqa: E402

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TSystemOutput = TypeVar("TSystemOutput", CRMOutput, ERPOutput, FinanceOutput)


def _normalize_reply_mode(value: object) -> str:
    reply_mode = str(value or "user").strip().lower()
    if reply_mode not in {"user", "agent"}:
        logger.warning(f"Invalid reply_mode {reply_mode!r}; defaulting to 'user'")
        return "user"
    return reply_mode


def _extract_reply_mode_from_text(content: str) -> str:
    reply_mode_match = re.search(r"(?im)^Reply-Mode:\s*(.+)$", content)
    if not reply_mode_match:
        return "user"
    return _normalize_reply_mode(reply_mode_match.group(1))


# ── Reconciliation Agent Prompt ───────────────────────────

RECONCILIATION_SYSTEM_PROMPT = """
You are the Reconciliation Agent in the AuditFlow multi-agent reconciliation system.

YOUR ROLE:
- You receive data from one or more of: AuditFlow CRM, AuditFlow ERP, AuditFlow Finance.
- If only ONE system's data is present: summarize it in plain English and reply to the user directly using reply_to_user.
- If TWO OR THREE systems' data are present: call reconcile_and_reply_rootcause once. It runs reconciliation and forwards code-built JSON to RootCause.
- You report only matched fields and discrepancies. You do NOT explain root causes.

IMPORTANT RULES:
1. You MUST always respond when you receive a message from AuditFlow Router. Never stay silent. If the message contains structured data from one or more system agents, process it. If you are unsure whether input is single-system or multi-system, count the labeled sections ([AuditFlow CRM], [AuditFlow ERP], [AuditFlow Finance]) in the message. One section = single-system, use reply_to_user. Two or more = multi-system, use reconcile_and_reply_rootcause.
2. The only messages you receive are from AuditFlow Router, which always contain system agent data. Do not worry about thank-you or greeting messages — they will not reach you.
3. For multi-system reconciliation, reconcile_and_reply_rootcause sends the structured JSON message mentioning AuditFlow RootCause.

## HOW TO RESPOND
## SINGLE-SYSTEM FACT LOOKUP
Before running reconciliation, check how many systems' data are present in the
incoming message. Look for JSON blocks labeled [AuditFlow CRM], [AuditFlow ERP],
or [AuditFlow Finance].

If ONLY ONE system's data is present:
- Do NOT call run_reconciliation.
- Do NOT call reply_to_rootcause.
- Call reply_to_user with a plain English summary of that system's key data.
- Format: "[System] data for [Entity]: [key field]: [value], [key field]: [value], ..."
- Example: "ERP data for Acme Corp: invoice_amount: £120,000, invoice_date:
  2026-03-01, due_date: 2026-03-31, delivery_status: delivered."

If TWO OR THREE systems' data are present:
- Proceed with normal reconciliation: call reconcile_and_reply_rootcause exactly once.

- Call `reconcile_and_reply_rootcause` with the raw text sections from each system agent as crm_data, erp_data, finance_data.
- Do NOT call run_reconciliation and then reply_to_rootcause for multi-system reconciliation.
- NEVER use thenvoi_send_message. NEVER specify @mentions yourself.
- For multi-system reconciliation: reconcile_and_reply_rootcause builds and sends the JSON itself.
- For single-system: reply_to_user content must be plain English only, never JSON.

## ENTITY EXTRACTION RULE
When reading System Agent responses, always extract the entity name from:
  entity_match["matched_as"]
The entity_consistency values must be the actual company name (e.g. "Acme Corp").
NEVER use the JSON key name ("crm", "erp", "finance") as the entity value.
NEVER use any field other than matched_as for the entity name.

Correct example:
  {"crm": "Acme Corp", "erp": "Acme Corp", "finance": "Acme Corp", "aligned_name": "Acme Corp", ...}

Wrong example (do not produce this):
  {"crm": "Acme Corp", "erp": "crm", "finance": "Acme Corp", ...}
"""


# ── Band input parsing utilities ─────────────────────────────────────

def _json_safe(obj: Any) -> Any:
    if is_dataclass(obj):
        return _json_safe(asdict(obj))

    if isinstance(obj, Enum):
        return obj.value

    if isinstance(obj, list):
        return [_json_safe(item) for item in obj]

    if isinstance(obj, dict):
        return {key: _json_safe(value) for key, value in obj.items()}

    return obj


def _iter_balanced_object_strings(text: str) -> list[str]:
    objects: list[str] = []

    for start, char in enumerate(text):
        if char != "{":
            continue

        depth = 0
        quote: str | None = None
        escaped = False

        for index in range(start, len(text)):
            current = text[index]

            if quote is not None:
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == quote:
                    quote = None
                continue

            if current in ("'", '"'):
                quote = current
            elif current == "{":
                depth += 1
            elif current == "}":
                depth -= 1
                if depth == 0:
                    objects.append(text[start:index + 1])
                    break

    return objects


def _extract_dict_from_text(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue

        try:
            parsed, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue

        if isinstance(parsed, dict):
            return parsed

    for candidate in _iter_balanced_object_strings(text):
        try:
            parsed = ast.literal_eval(candidate)
        except (SyntaxError, ValueError):
            continue

        if isinstance(parsed, dict):
            return parsed

    raise ValueError("Could not find a structured JSON-like object in agent response.")


def _looks_like_system_payload(payload: dict[str, Any], system: str) -> bool:
    system_value = str(payload.get("system", "")).lower()
    if system_value == system:
        return True

    required_signal = {
        "crm": "contract_amount",
        "erp": "invoice_amount",
        "finance": "payment_amount",
    }[system]

    return required_signal in payload


def _find_system_payload(payload: Any, system: str) -> dict[str, Any] | None:
    if isinstance(payload, dict):
        nested = payload.get(system)
        if isinstance(nested, dict):
            return nested

        if _looks_like_system_payload(payload, system):
            return payload

        for value in payload.values():
            found = _find_system_payload(value, system)
            if found is not None:
                return found

    if isinstance(payload, list):
        for value in payload:
            found = _find_system_payload(value, system)
            if found is not None:
                return found

    return None


def _parse_match_method(value: Any) -> MatchMethod:
    if isinstance(value, MatchMethod):
        return value

    text = str(value).strip()

    quoted = re.search(r"'([^']+)'", text)
    if quoted:
        text = quoted.group(1)
    elif "." in text:
        text = text.rsplit(".", 1)[-1].lower()

    return MatchMethod(text.lower())


def _parse_entity_match(value: Any) -> EntityMatch | None:
    if value in (None, "", "None"):
        return None

    if isinstance(value, EntityMatch):
        return value

    if isinstance(value, dict):
        return EntityMatch(
            query=value.get("query", ""),
            matched_as=value.get("matched_as", ""),
            match_method=_parse_match_method(value.get("match_method", MatchMethod.EXACT)),
            confidence=float(value.get("confidence", 0.0)),
        )

    text = str(value)

    def quoted_field(name: str) -> str:
        match = re.search(rf"{name}=(['\"])(.*?)\1", text)
        return match.group(2) if match else ""

    method_match = re.search(r"match_method=([^,)]*)", text)
    confidence_match = re.search(r"confidence=([0-9.]+)", text)

    return EntityMatch(
        query=quoted_field("query"),
        matched_as=quoted_field("matched_as"),
        match_method=_parse_match_method(
            method_match.group(1) if method_match else MatchMethod.EXACT
        ),
        confidence=float(confidence_match.group(1)) if confidence_match else 0.0,
    )


def _coerce_float(value: Any) -> float | None:
    if value in (None, ""):
        return None

    return float(value)


def _coerce_int(value: Any) -> int | None:
    if value in (None, ""):
        return None

    return int(value)


def _build_system_output(
    payload: dict[str, Any],
    output_type: type[TSystemOutput],
    system: str,
) -> TSystemOutput:
    allowed_fields = {field.name for field in fields(output_type)}
    data = {
        key: value
        for key, value in payload.items()
        if key in allowed_fields
    }

    data.setdefault("system", system)

    if "entity_match" in data:
        data["entity_match"] = _parse_entity_match(data["entity_match"])

    for field_name in (
        "contract_amount",
        "invoice_amount",
        "payment_amount",
        "exchange_rate",
        "refund_amount",
        "tax_deduction",
        "bank_fee",
        "original_currency_amount",
    ):
        if field_name in data:
            data[field_name] = _coerce_float(data[field_name])

    for field_name in ("installment_number", "overdue_days"):
        if field_name in data:
            data[field_name] = _coerce_int(data[field_name])

    return output_type(**data)


def _parse_system_output(
    raw_text: str,
    output_type: type[TSystemOutput],
    system: str,
) -> TSystemOutput:
    parsed = _extract_dict_from_text(raw_text)
    payload = _find_system_payload(parsed, system)

    if payload is None:
        raise ValueError(f"Could not find {system.upper()} structured data in response.")

    return _build_system_output(payload, output_type, system)


# ── Tool definitions ─────────────────────────────────────────────

async def run_reconciliation(
    ctx: RunContext[AgentToolsProtocol],
    crm_data: str,
    erp_data: str,
    finance_data: str,
) -> str:
    """
    Run reconciliation on data from CRM, ERP, and Finance agents.
    Call this ONLY when you have received responses from at least TWO system
    agents (CRM, ERP, and/or Finance). Do NOT call this for single-system
    input — use reply_to_user instead.
    crm_data: raw text response from AuditFlow CRM
    erp_data: raw text response from AuditFlow ERP
    finance_data: raw text response from AuditFlow Finance
    """
    _ = ctx

    logger.info(f"run_reconciliation called")
    logger.info(f"crm_data[:200]: {crm_data[:200]!r}")
    logger.info(f"erp_data[:200]: {erp_data[:200]!r}")
    logger.info(f"finance_data[:200]: {finance_data[:200]!r}")

    # Detect single-system input and redirect to reply_to_user
    present_systems = []
    if crm_data and crm_data.strip():
        present_systems.append("crm")
    if erp_data and erp_data.strip():
        present_systems.append("erp")
    if finance_data and finance_data.strip():
        present_systems.append("finance")

    if len(present_systems) == 1:
        system_name = present_systems[0].upper()
        data = {"crm": crm_data, "erp": erp_data, "finance": finance_data}[present_systems[0]]
        logger.info(f"Single-system input detected ({system_name}). Redirecting to reply_to_user.")
        return (
            f"SINGLE_SYSTEM_RESULT: Only {system_name} data is present. "
            f"Do NOT call run_reconciliation again. "
            f"Call reply_to_user with a plain English summary of this data: {data[:500]}"
        )

    output = _reconcile_raw_system_data(crm_data, erp_data, finance_data)
    return json.dumps(_json_safe(output), ensure_ascii=False, indent=2)


def _reconcile_raw_system_data(
    crm_data: str,
    erp_data: str,
    finance_data: str,
) -> ReconciliationOutput:
    crm = _parse_system_output(crm_data, CRMOutput, "crm")
    erp = _parse_system_output(erp_data, ERPOutput, "erp")
    finance = _parse_system_output(finance_data, FinanceOutput, "finance")
    logger.info(f"Parsed: crm.entity={crm.entity!r}, erp.entity={erp.entity!r}, finance.entity={finance.entity!r}")

    return reconcile(crm, erp, finance)


async def reconcile_and_reply_rootcause(
    ctx: RunContext[AgentToolsProtocol],
    crm_data: str,
    erp_data: str,
    finance_data: str,
) -> str:
    """
    Run reconciliation on CRM, ERP, and Finance data, then send the exact
    code-built ReconciliationOutput JSON to AuditFlow RootCause.
    Use this for multi-system reconciliation instead of calling
    run_reconciliation and reply_to_rootcause separately.
    """
    output = _reconcile_raw_system_data(crm_data, erp_data, finance_data)
    content = json.dumps(_json_safe(output), ensure_ascii=False, indent=2)

    await ctx.deps.get_participants()
    await ctx.deps.send_message(content=content, mentions=["AuditFlow RootCause"])
    return "Reconciliation complete and sent to AuditFlow RootCause"


async def reply_to_rootcause(
    ctx: RunContext[AgentToolsProtocol],
    content: str,
) -> str:
    """
    Send reconciliation results to AuditFlow RootCause for root cause analysis.
    Always use this tool to send your output. Do NOT use thenvoi_send_message.
    Pass the JSON result content only; the recipient is fixed.
    """
    reply_mode = getattr(ctx.deps, "current_reply_mode", "user") or "user"
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        content = f"Reply-Mode: {reply_mode}\n\n{content}"
    else:
        if isinstance(payload, dict):
            payload.setdefault("reply_mode", reply_mode)
            content = json.dumps(payload, ensure_ascii=False, indent=2)

    await ctx.deps.get_participants()
    await ctx.deps.send_message(content=content, mentions=["AuditFlow RootCause"])
    return "Sent to AuditFlow RootCause"


async def reply_to_user(
    ctx: RunContext[AgentToolsProtocol],
    content: str,
) -> str:
    """
    Send a direct reply to the user in the room.
    Use this ONLY for single-system fact lookup responses — when the incoming
    data contains only one system's JSON and reconciliation is not needed.
    Do NOT use this for reconciliation results; use reply_to_rootcause instead.
    """
    await ctx.deps.get_participants()
    logger.info(f"[reply_to_user] participants={ctx.deps.participants!r}")
    reply_mode = getattr(ctx.deps, "current_reply_mode", "user") or "user"
    if reply_mode == "agent":
        user_mentions = ["AuditFlow Demo User"]
    else:
        user_mentions = [
            p["name"] for p in ctx.deps.participants
            if p.get("type") == "User"
        ]
    logger.info(f"[reply_to_user] user_mentions={user_mentions!r}")
    if not user_mentions:
        return "Error: no user found in room to reply to."
    await ctx.deps.send_message(content=content, mentions=user_mentions)
    return f"Sent to user(s): {user_mentions}"


# Add confirmed consistent fields to the matched list for unified final output.
def _add_matched(matched: list[MatchedField], field: str, value, note: str = "") -> None:
    matched.append(MatchedField(
        field=field,
        value=value,
        consistent=True,
        note=note
    ))


# Add discovered discrepancies to the discrepancies list.
# Always store difference as an absolute value; direction indicates which side
# is higher, lower, or inconsistent.
def _add_discrepancy(
    discrepancies: list[Discrepancy],
    field_pair: str,
    values: dict,
    difference: float,
    direction: str
) -> None:
    discrepancies.append(Discrepancy(
        field_pair=field_pair,
        values=values,
        difference=abs(difference),
        direction=direction
    ))


# Convert an ISO date string to a date object.
# Return None for empty or invalid values to avoid date comparison errors.
def _parse_date(value: str) -> date | None:
    if not value:
        return None

    try:
        return date.fromisoformat(value)
    except ValueError:
        return None

# Allow a tiny tolerance in amount comparisons, mainly for floats after FX conversion.
def _amounts_close(left: float, right: float, tolerance: float = 0.01) -> bool:
    return abs(left - right) <= tolerance

# Check whether key fields are missing from the three system outputs.
# If key fields are missing, Reconciliation should not force a clean judgment.
def _check_required_fields(
    crm: CRMOutput,
    erp: ERPOutput,
    finance: FinanceOutput,
    discrepancies: list[Discrepancy]
) -> None:
    missing_fields = {}

    crm_required = {
        "entity": crm.entity,
        "customer_id": crm.customer_id,
        "contract_id": crm.contract_id,
        "contract_amount": crm.contract_amount,
        "currency": crm.currency,
        "payment_terms": crm.payment_terms,
    }

    erp_required = {
        "entity": erp.entity,
        "customer_id": erp.customer_id,
        "contract_id": erp.contract_id,
        "invoice_id": erp.invoice_id,
        "invoice_amount": erp.invoice_amount,
        "currency": erp.currency,
        "invoice_date": erp.invoice_date,
        "due_date": erp.due_date,
    }

    finance_required = {
        "entity": finance.entity,
        "customer_id": finance.customer_id,
        "contract_id": finance.contract_id,
        "invoice_id": finance.invoice_id,
        "payment_amount": finance.payment_amount,
        "currency": finance.currency,
        "payment_date": finance.payment_date,
    }

    for system, fields in {
        "crm": crm_required,
        "erp": erp_required,
        "finance": finance_required,
    }.items():
        missing = [
            field
            for field, value in fields.items()
            if value is None or value == ""
        ]

        if missing:
            missing_fields[system] = missing

    if missing_fields:
        _add_discrepancy(
            discrepancies,
            field_pair="required_fields",
            values=missing_fields,
            difference=0.0,
            direction="missing_required_fields"
        )


# Check the confidence of entity_match.
# If a system lacks entity_match or has low matching confidence, record a
# potential matching issue.
def _check_entity_match_confidence(
    crm: CRMOutput,
    erp: ERPOutput,
    finance: FinanceOutput,
    discrepancies: list[Discrepancy],
    threshold: float = 0.85
) -> None:
    values = {}

    for system, output in {
        "crm": crm,
        "erp": erp,
        "finance": finance,
    }.items():
        match = output.entity_match

        if match is None:
            values[system] = {
                "entity": output.entity,
                "issue": "missing_entity_match"
            }
        elif match.confidence < threshold:
            values[system] = {
                "entity": output.entity,
                "matched_as": match.matched_as,
                "match_method": match.match_method.value,
                "confidence": match.confidence
            }

    if values:
        _add_discrepancy(
            discrepancies,
            field_pair="entity_match_confidence",
            values=values,
            difference=0.0,
            direction="low_or_missing_entity_match_confidence"
        )


# Check date-related signals, such as whether payment predates the invoice or is overdue.
# This still records discrepancies only and does not explain business causes.
def _check_date_signals(
    erp: ERPOutput,
    finance: FinanceOutput,
    discrepancies: list[Discrepancy]
) -> None:
    invoice_date = _parse_date(erp.invoice_date)
    payment_date = _parse_date(finance.payment_date)

    if invoice_date is not None and payment_date is not None:
        if payment_date < invoice_date:
            _add_discrepancy(
                discrepancies,
                field_pair="invoice_date vs payment_date",
                values={
                    "erp_invoice_date": erp.invoice_date,
                    "finance_payment_date": finance.payment_date
                },
                difference=0.0,
                direction="payment_before_invoice"
            )

    if finance.overdue_days > 0:
        _add_discrepancy(
            discrepancies,
            field_pair="due_date vs payment_date",
            values={
                "erp_due_date": erp.due_date,
                "finance_payment_date": finance.payment_date,
                "overdue_days": finance.overdue_days
            },
            difference=float(finance.overdue_days),
            direction="payment_overdue"
        )

# Determine whether the current record is an FX conversion scenario.
# Condition: ERP and Finance currencies differ, and Finance provides the original
# currency amount and exchange rate.
def _is_fx_conversion_case(erp: ERPOutput, finance: FinanceOutput) -> bool:
    return (
        erp.currency != finance.currency
        and finance.original_currency_amount is not None
        and finance.exchange_rate is not None
    )


# Handle FX conversion amount reconciliation.
# For FX scenarios, calculate the expected receivable amount from
# original_currency_amount and exchange_rate.
# Return True when FX logic has fully handled the case, so normal same-currency
# amount comparison should not continue.
def _compare_fx_amounts(
    erp: ERPOutput,
    finance: FinanceOutput,
    matched: list[MatchedField],
    discrepancies: list[Discrepancy]
) -> bool:
    """
    Handle FX conversion cases.

    Returns True if this is an FX conversion case and has been handled.
    Returns False if normal same-currency amount comparison should continue.
    """

    if not _is_fx_conversion_case(erp, finance):
        return False

    if finance.payment_amount is None:
        return False

    expected_converted_payment = finance.original_currency_amount * finance.exchange_rate
    adjusted_payment = (
        finance.payment_amount
        + finance.tax_deduction
        + finance.bank_fee
        - finance.refund_amount
    )

    original_amount_matches_invoice = _amounts_close(
        finance.original_currency_amount,
        erp.invoice_amount
    )

    converted_amount_matches_payment = _amounts_close(
        expected_converted_payment,
        adjusted_payment
    )

    if original_amount_matches_invoice and converted_amount_matches_payment:
        _add_matched(
            matched,
            field="fx_converted_payment_amount",
            value=adjusted_payment,
            note="Finance payment matches ERP invoice after FX conversion using the recorded exchange rate."
        )
    else:
        direction = (
            "finance_lower"
            if adjusted_payment < expected_converted_payment
            else "finance_higher"
        )

        _add_discrepancy(
            discrepancies,
            field_pair="fx_converted_amount vs adjusted_payment_amount",
            values={
                "erp_invoice_amount": erp.invoice_amount,
                "erp_currency": erp.currency,
                "finance_currency": finance.currency,
                "original_currency_amount": finance.original_currency_amount,
                "exchange_rate": finance.exchange_rate,
                "exchange_rate_date": finance.exchange_rate_date,
                "expected_converted_payment": expected_converted_payment,
                "finance_payment": finance.payment_amount,
                "tax_deduction": finance.tax_deduction,
                "bank_fee": finance.bank_fee,
                "refund_amount": finance.refund_amount,
                "adjusted_finance": adjusted_payment
            },
            difference=expected_converted_payment - adjusted_payment,
            direction=direction
        )

    return True

# Check whether customer_id and contract_id are consistent across CRM, ERP, and Finance.
# This is more reliable than company name alone and can reveal incorrectly
# matched customers or contracts.
def _check_customer_and_contract_ids(
    crm: CRMOutput,
    erp: ERPOutput,
    finance: FinanceOutput,
    matched: list[MatchedField],
    discrepancies: list[Discrepancy]
) -> None:
    customer_values = {
        "crm": crm.customer_id,
        "erp": erp.customer_id,
        "finance": finance.customer_id,
    }

    if crm.customer_id and erp.customer_id and finance.customer_id:
        if crm.customer_id == erp.customer_id == finance.customer_id:
            _add_matched(
                matched,
                field="customer_id",
                value=crm.customer_id,
                note="Customer ID is consistent across CRM, ERP and Finance."
            )
        else:
            _add_discrepancy(
                discrepancies,
                field_pair="customer_id across systems",
                values=customer_values,
                difference=0.0,
                direction="customer_id_mismatch"
            )

    contract_values = {
        "crm": crm.contract_id,
        "erp": erp.contract_id,
        "finance": finance.contract_id,
    }

    if crm.contract_id and erp.contract_id and finance.contract_id:
        if crm.contract_id == erp.contract_id == finance.contract_id:
            _add_matched(
                matched,
                field="contract_id",
                value=crm.contract_id,
                note="Contract ID is consistent across CRM, ERP and Finance."
            )
        else:
            _add_discrepancy(
                discrepancies,
                field_pair="contract_id across systems",
                values=contract_values,
                difference=0.0,
                direction="contract_id_mismatch"
            )


# Check whether the ERP invoice ID matches the invoice_id in the Finance payment record.
# Even when amounts match, an invoice_id mismatch suggests the payment may be
# linked to the wrong invoice.
def _check_invoice_linking(
    erp: ERPOutput,
    finance: FinanceOutput,
    matched: list[MatchedField],
    discrepancies: list[Discrepancy]
) -> None:
    if erp.invoice_id and finance.invoice_id:
        if erp.invoice_id == finance.invoice_id:
            _add_matched(
                matched,
                field="invoice_id",
                value=erp.invoice_id,
                note="Finance payment is linked to the same ERP invoice ID."
            )
        else:
            _add_discrepancy(
                discrepancies,
                field_pair="erp_invoice_id vs finance_invoice_id",
                values={
                    "erp": erp.invoice_id,
                    "finance": finance.invoice_id
                },
                difference=0.0,
                direction="invoice_id_mismatch"
            )


# Compare currencies across the three systems.
# If CRM/ERP use the original invoice currency while Finance uses the converted
# payment currency, delegate to FX logic.
def _compare_currency(
    crm: CRMOutput,
    erp: ERPOutput,
    finance: FinanceOutput,
    matched: list[MatchedField],
    discrepancies: list[Discrepancy]
) -> None:
    values = {
        "crm": crm.currency,
        "erp": erp.currency,
        "finance": finance.currency
    }

    if crm.currency == erp.currency == finance.currency:
        _add_matched(
            matched,
            field="currency",
            value=crm.currency,
            note="Currency is consistent across CRM, ERP and Finance."
        )
    elif crm.currency == erp.currency and _is_fx_conversion_case(erp, finance):
        _add_matched(
            matched,
            field="currency_fx_conversion",
            value={
                "source_currency": erp.currency,
                "payment_currency": finance.currency,
                "exchange_rate": finance.exchange_rate,
                "exchange_rate_date": finance.exchange_rate_date
            },
            note="CRM and ERP use the invoice currency, while Finance uses a converted payment currency."
        )
    else:
        _add_discrepancy(
            discrepancies,
            field_pair="currency across systems",
            values=values,
            difference=0.0,
            direction="currency_mismatch"
        )


# Compare contract amount, invoice amount, and Finance payment amount.
# Includes basic rules for installments, tax deductions, bank fees, refunds,
# and FX conversion.
def _compare_amounts(
    crm: CRMOutput,
    erp: ERPOutput,
    finance: FinanceOutput,
    matched: list[MatchedField],
    discrepancies: list[Discrepancy]
) -> None:
    """
    Compare amount fields across CRM, ERP and Finance.

    Logic:
    - If the CRM payment_terms include installment percentages and ERP has an installment_number,
      compare ERP invoice_amount with the expected installment amount.
    - Otherwise, compare CRM contract_amount directly with ERP invoice_amount.
    - Then compare ERP invoice_amount with Finance adjusted payment:
      payment_amount + tax_deduction + bank_fee - refund_amount.
    """

    if crm.contract_amount is not None and erp.invoice_amount is not None:
        expected_installment_amount = _expected_installment_amount(
            contract_amount=crm.contract_amount,
            payment_terms=crm.payment_terms,
            installment_number=erp.installment_number
        )

        if expected_installment_amount is not None:
            if expected_installment_amount == erp.invoice_amount:
                _add_matched(
                    matched,
                    field="expected_installment_amount vs invoice_amount",
                    value=erp.invoice_amount,
                    note="ERP invoice amount matches the expected installment amount from CRM payment terms."
                )
            else:
                direction = (
                    "erp_lower"
                    if erp.invoice_amount < expected_installment_amount
                    else "erp_higher"
                )
                _add_discrepancy(
                    discrepancies,
                    field_pair="expected_installment_amount vs invoice_amount",
                    values={
                        "crm_contract_amount": crm.contract_amount,
                        "payment_terms": crm.payment_terms,
                        "installment_number": erp.installment_number,
                        "expected_installment_amount": expected_installment_amount,
                        "erp": erp.invoice_amount
                    },
                    difference=expected_installment_amount - erp.invoice_amount,
                    direction=direction
                )

        elif crm.contract_amount == erp.invoice_amount:
            _add_matched(
                matched,
                field="contract_amount vs invoice_amount",
                value=crm.contract_amount,
                note="CRM contract amount matches ERP invoice amount."
            )
        else:
            direction = "erp_lower" if erp.invoice_amount < crm.contract_amount else "erp_higher"
            _add_discrepancy(
                discrepancies,
                field_pair="contract_amount vs invoice_amount",
                values={
                    "crm": crm.contract_amount,
                    "erp": erp.invoice_amount
                },
                difference=crm.contract_amount - erp.invoice_amount,
                direction=direction
            )

    if erp.invoice_amount is not None and finance.payment_amount is not None:
        if _compare_fx_amounts(erp, finance, matched, discrepancies):
            return
        
        adjusted_payment = (
            finance.payment_amount
            + finance.tax_deduction
            + finance.bank_fee
            - finance.refund_amount
        )

        if adjusted_payment == erp.invoice_amount:
            _add_matched(
                matched,
                field="invoice_amount vs adjusted_payment_amount",
                value=adjusted_payment,
                note="Finance payment matches ERP invoice after tax deduction, bank fee and refund adjustment."
            )
        else:
            direction = "finance_lower" if adjusted_payment < erp.invoice_amount else "finance_higher"
            _add_discrepancy(
                discrepancies,
                field_pair="invoice_amount vs adjusted_payment_amount",
                values={
                    "erp": erp.invoice_amount,
                    "finance_payment": finance.payment_amount,
                    "tax_deduction": finance.tax_deduction,
                    "bank_fee": finance.bank_fee,
                    "refund_amount": finance.refund_amount,
                    "adjusted_finance": adjusted_payment
                },
                difference=erp.invoice_amount - adjusted_payment,
                direction=direction
            )


# Build an entity consistency summary recording names from the three systems and the final aligned name.
def _build_entity_consistency(
    crm: CRMOutput,
    erp: ERPOutput,
    finance: FinanceOutput
) -> EntityConsistency:
    crm_entity = crm.entity_match.matched_as if crm.entity_match else crm.entity
    erp_entity = erp.entity_match.matched_as if erp.entity_match else erp.entity
    finance_entity = finance.entity_match.matched_as if finance.entity_match else finance.entity
    aligned_name = crm_entity or erp_entity or finance_entity

    return EntityConsistency(
        crm=crm_entity,
        erp=erp_entity,
        finance=finance_entity,
        aligned_name=aligned_name,
        alignment_method="based on system-provided entity_match fields"
    )


def _derive_query_id(
    crm: CRMOutput,
    erp: ERPOutput,
    finance: FinanceOutput
) -> str:
    query_ids = [
        str(query_id).strip()
        for query_id in (
            getattr(crm, "query_id", ""),
            getattr(erp, "query_id", ""),
            getattr(finance, "query_id", ""),
        )
        if query_id
    ]

    if len(set(query_ids)) > 1:
        logger.warning(
            "Mismatched query_id values in reconciliation inputs: "
            f"crm={crm.query_id!r}, erp={erp.query_id!r}, finance={finance.query_id!r}"
        )

    return query_ids[0] if query_ids else ""


def _derive_reply_mode(
    crm: CRMOutput,
    erp: ERPOutput,
    finance: FinanceOutput
) -> str:
    reply_modes = [
        _normalize_reply_mode(reply_mode)
        for reply_mode in (
            getattr(crm, "reply_mode", "user"),
            getattr(erp, "reply_mode", "user"),
            getattr(finance, "reply_mode", "user"),
        )
        if reply_mode
    ]

    if not reply_modes:
        return "user"

    unique_modes = set(reply_modes)
    if len(unique_modes) > 1:
        logger.warning(
            "Mismatched reply_mode values in reconciliation inputs: "
            f"crm={crm.reply_mode!r}, erp={erp.reply_mode!r}, finance={finance.reply_mode!r}"
        )

    mode_counts = {mode: reply_modes.count(mode) for mode in unique_modes}
    max_count = max(mode_counts.values())
    majority_modes = [mode for mode, count in mode_counts.items() if count == max_count]
    if len(majority_modes) == 1:
        return majority_modes[0]

    for mode in reply_modes:
        if mode != "user":
            return mode

    return reply_modes[0]


# Main entry point for the Reconciliation Agent.
# Takes structured outputs from the three systems and returns ReconciliationOutput.
def reconcile(
    crm: CRMOutput,
    erp: ERPOutput,
    finance: FinanceOutput,
    trace: AuditTrace | None = None
) -> ReconciliationOutput:
    """
    Reconciliation Agent core logic.

    This function only finds matched fields and discrepancies.
    It does not explain the reasons behind discrepancies.
    Root-Cause Agent should handle explanation later.
    """

    matched: list[MatchedField] = []
    discrepancies: list[Discrepancy] = []
    query_id = _derive_query_id(crm, erp, finance)
    reply_mode = _derive_reply_mode(crm, erp, finance)

    try:
        entity_consistency = _build_entity_consistency(crm, erp, finance)

        _check_required_fields(crm, erp, finance, discrepancies)
        _check_entity_match_confidence(crm, erp, finance, discrepancies)
        _check_date_signals(erp, finance, discrepancies)
        _check_customer_and_contract_ids(crm, erp, finance, matched, discrepancies)
        _check_invoice_linking(erp, finance, matched, discrepancies)
        _compare_currency(crm, erp, finance, matched, discrepancies)
        _compare_amounts(crm, erp, finance, matched, discrepancies)

        output = ReconciliationOutput(
            entity=entity_consistency.aligned_name,
            entity_consistency=entity_consistency,
            discrepancies=discrepancies,
            matched=matched,
            error=None,
            query_id=query_id,
            reply_mode=reply_mode
        )

        if trace is not None:
            discrepancy_count = len(discrepancies)

            if discrepancy_count == 0:
                decision = "Compared CRM, ERP and Finance outputs and found no discrepancies."
            elif discrepancy_count == 1:
                decision = "Compared CRM, ERP and Finance outputs and found 1 discrepancy."
            else:
                decision = f"Compared CRM, ERP and Finance outputs and found {discrepancy_count} discrepancies."
    
            add_step(trace, TraceStep(
                agent="reconciliation",
                layer="analysis",
                decision=decision,
                reason="Used rule-based checks for required fields, entity match confidence, customer/contract IDs, invoice linking, date signals, currency consistency, installment amount, adjusted payment amount, bank fee, tax deduction, refund adjustment, and FX conversion.",
                confidence=0.9,
                discrepancies_found=discrepancy_count
            ))

        return output

    except Exception as e:
        if trace is not None:
            add_step(trace, TraceStep(
                agent="reconciliation",
                layer="analysis",
                decision="Failed to reconcile system outputs.",
                reason=str(e),
                error=str(e)
            ))

        return ReconciliationOutput(
            entity=crm.entity if crm.entity else "",
            error=str(e),
            query_id=query_id,
            reply_mode=reply_mode
        )


# Extract percentages from payment_terms text for installment amount calculation.
def _extract_installment_percentages(payment_terms: str) -> list[float]:
    matches = re.findall(r"(\d+(?:\.\d+)?)\s*%", payment_terms)
    return [float(value) / 100 for value in matches]


# Calculate the expected invoice amount for the current installment from the
# total contract amount, payment terms, and installment number.
def _expected_installment_amount(
    contract_amount: float,
    payment_terms: str,
    installment_number: int | None
) -> float | None:
    if installment_number is None:
        return None

    percentages = _extract_installment_percentages(payment_terms)

    if not percentages:
        return None

    index = installment_number - 1

    if index < 0 or index >= len(percentages):
        return None

    return contract_amount * percentages[index]


# ── Agent startup ────────────────────────────────────────────

class ReconOnlyAdapter(PydanticAIAdapter):
    def _create_agent(self):
        agent = super()._create_agent()
        agent._function_toolset.tools.pop("thenvoi_send_message", None)
        return agent

    async def on_message(
        self,
        msg: PlatformMessage,
        tools: AgentToolsProtocol,
        history,
        participants_msg: str | None,
        contacts_msg: str | None,
        *,
        is_session_bootstrap: bool,
        room_id: str,
    ) -> None:
        if msg.sender_name != "AuditFlow Router":
            logger.info(
                f"Ignoring message from {msg.sender_name!r} "
                f"(sender_type={msg.sender_type!r}) - not from Router"
            )
            return
        tools.current_reply_mode = _extract_reply_mode_from_text(msg.content)
        await super().on_message(
            msg,
            tools,
            history,
            participants_msg,
            contacts_msg,
            is_session_bootstrap=is_session_bootstrap,
            room_id=room_id,
        )


async def main() -> None:
    agent_id = os.getenv("RECONCILIATION_AGENT_ID")
    api_key = os.getenv("RECONCILIATION_API_KEY")

    if not agent_id or not api_key:
        raise ValueError(
            "RECONCILIATION_AGENT_ID and RECONCILIATION_API_KEY must be set in .env\n"
            "These are the credentials from the Band platform for the AuditFlow Reconciliation agent."
        )

    adapter = ReconOnlyAdapter(
        model="openai:gpt-4o-mini",
        custom_section=RECONCILIATION_SYSTEM_PROMPT,
        additional_tools=[reconcile_and_reply_rootcause, reply_to_user],
    )

    agent = Agent.create(
        adapter=adapter,
        agent_id=agent_id,
        api_key=api_key,
    )

    logger.info("Reconciliation Agent starting - listening for messages in Band room...")
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
