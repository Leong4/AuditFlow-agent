from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import sys
from dataclasses import asdict, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from pydantic_ai import RunContext
from thenvoi import Agent
from thenvoi.core.protocols import AgentToolsProtocol
from thenvoi.core.types import PlatformMessage

# Path setup: allow direct execution with imports from shared/.
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from shared.schemas import (  # noqa: E402
    ReconciliationOutput,
    RootCauseOutput,
    ReconciliationSummary,
    Discrepancy,
    MatchedField,
    EntityConsistency,
)
from shared.thenvoi_pydantic_compat import PydanticAIAdapter  # noqa: E402

from agents.rootcause.rules_handle import run_root_cause_agent  # noqa: E402
from agents.rootcause.llm_client import RootCauseLLMClient, LLMClientError  # noqa: E402

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


ROOTCAUSE_SYSTEM_PROMPT = """
You are the Root-Cause Agent in the AuditFlow multi-agent reconciliation system.

YOUR ROLE:
- You receive ReconciliationOutput JSON from AuditFlow Reconciliation.
- You reconstruct a ReconciliationOutput object from that JSON.
- You call the run_root_cause_analysis tool to analyze discrepancy causes.
- You return RootCauseOutput as structured JSON. You do NOT rerun reconciliation.

IMPORTANT RULES:
1. Only respond if the message contains a ReconciliationOutput JSON with a discrepancies field.
2. If the message is a thank-you, acknowledgment, greeting, or any non-data message, do not reply at all.
3. After running root cause analysis, send the final results as a structured JSON message back to the room mentioning the user directly.

## HOW TO RESPOND
- Call `run_root_cause_analysis` with the reconciliation JSON as reconciliation_data.
- Then call `reply_to_user` with a plain English summary (NOT raw JSON). Structure your reply like this:

  **AuditFlow Root Cause Analysis — [Entity], [Time Scope]**

  **Summary:** X discrepancies found. [Normal/Anomaly/Watch breakdown.]

  For each discrepancy:
  - **[Field pair]**: [Probable cause]. Status: [normal/anomaly/watch]. Risk: [low/medium/high].
    Evidence: [key evidence points].
    Recommended action: [action].

  If no discrepancies: "All fields are consistent across CRM, ERP, and Finance. No action required."

- NEVER paste raw JSON to the user. NEVER use thenvoi_send_message.
"""


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

    raise ValueError("Could not find a structured JSON-like object in root-cause input.")


def _find_reconciliation_payload(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, str):
        try:
            return _find_reconciliation_payload(_extract_dict_from_text(payload))
        except ValueError:
            return None

    if isinstance(payload, dict):
        if "discrepancies" in payload:
            return payload

        nested = payload.get("reconciliation")
        if isinstance(nested, (dict, str)):
            found = _find_reconciliation_payload(nested)
            if found is not None:
                return found

        for value in payload.values():
            found = _find_reconciliation_payload(value)
            if found is not None:
                return found

    if isinstance(payload, list):
        for value in payload:
            found = _find_reconciliation_payload(value)
            if found is not None:
                return found

    return None


def _build_entity_consistency(value: Any) -> EntityConsistency | None:
    if value in (None, "", "None"):
        return None

    if isinstance(value, EntityConsistency):
        return value

    if not isinstance(value, dict):
        raise ValueError("entity_consistency must be a JSON object when provided.")

    allowed_fields = {field.name for field in fields(EntityConsistency)}
    data = {
        key: item
        for key, item in value.items()
        if key in allowed_fields
    }
    return EntityConsistency(**data)


def _build_discrepancies(values: Any) -> list[Discrepancy]:
    if values in (None, ""):
        return []

    if not isinstance(values, list):
        raise ValueError("discrepancies must be a list.")

    discrepancies: list[Discrepancy] = []

    for value in values:
        if isinstance(value, Discrepancy):
            discrepancies.append(value)
            continue

        if not isinstance(value, dict):
            raise ValueError("Each discrepancy must be a JSON object.")

        discrepancies.append(Discrepancy(
            field_pair=value.get("field_pair", ""),
            values=value.get("values") if isinstance(value.get("values"), dict) else {},
            difference=float(value.get("difference", 0.0) or 0.0),
            direction=value.get("direction", ""),
        ))

    return discrepancies


def _build_matched_fields(values: Any) -> list[MatchedField]:
    if values in (None, ""):
        return []

    if not isinstance(values, list):
        raise ValueError("matched must be a list when provided.")

    matched: list[MatchedField] = []

    for value in values:
        if isinstance(value, MatchedField):
            matched.append(value)
            continue

        if not isinstance(value, dict):
            raise ValueError("Each matched field must be a JSON object.")

        matched.append(MatchedField(
            field=value.get("field", ""),
            value=value.get("value"),
            consistent=bool(value.get("consistent", True)),
            note=value.get("note", ""),
        ))

    return matched


def _parse_reconciliation_output(raw_text: str) -> tuple[ReconciliationOutput, str]:
    parsed = _extract_dict_from_text(raw_text)
    payload = _find_reconciliation_payload(parsed)

    if payload is None:
        raise ValueError("Could not find ReconciliationOutput JSON with a discrepancies field.")

    reconciliation_output = ReconciliationOutput(
        entity=payload.get("entity", ""),
        entity_consistency=_build_entity_consistency(payload.get("entity_consistency")),
        discrepancies=_build_discrepancies(payload.get("discrepancies")),
        matched=_build_matched_fields(payload.get("matched", [])),
        error=payload.get("error"),
    )

    trace_id = str(payload.get("trace_id") or parsed.get("trace_id") or "")
    return reconciliation_output, trace_id


async def run_root_cause_analysis(
    ctx: RunContext[AgentToolsProtocol],
    reconciliation_data: str,
) -> str:
    """
    Run root cause analysis on ReconciliationOutput from Reconciliation Agent.
    Call this when you receive a JSON message containing reconciliation results
    with a discrepancies field.
    reconciliation_data: JSON string of ReconciliationOutput from AuditFlow Reconciliation
    """
    _ = ctx

    reconciliation_output, trace_id = _parse_reconciliation_output(reconciliation_data)
    output = RootCauseAgent().run(reconciliation_output, trace_id=trace_id)
    return json.dumps(_json_safe(output), ensure_ascii=False, indent=2)


async def reply_to_user(
    ctx: RunContext[AgentToolsProtocol],
    content: str,
) -> str:
    """
    Send the final root cause analysis result back to the user in the room.
    Always use this tool to reply. Do NOT use thenvoi_send_message.
    Pass the JSON result content only; the recipient is determined automatically.
    """
    await ctx.deps.get_participants()
    user_mentions = [
        p["name"] for p in ctx.deps.participants
        if p.get("type") == "User"
    ]
    if not user_mentions:
        return "Error: no user found in room to reply to."
    await ctx.deps.send_message(content=content, mentions=user_mentions)
    return f"Sent to user(s): {user_mentions}"


class RootCauseAgent:
    """
    Top-level Root-Cause Agent.

    This class is the external entry point for the Root-Cause module.

    Responsibilities:
    - receive ReconciliationOutput from Reconciliation Agent
    - decide whether to enable LLM enhancement
    - create RootCauseLLMClient if needed
    - call rule-based Root-Cause Agent
    - return RootCauseOutput

    It does NOT:
    - detect discrepancies
    - recalculate reconciliation results
    - directly call OpenAI
    - build prompts
    """

    def __init__(
        self,
        *,
        use_llm: bool = True,
        llm_client: Optional[RootCauseLLMClient] = None,
    ):
        self.use_llm = use_llm
        self.llm_client = llm_client

    def run(
        self,
        reconciliation_output: ReconciliationOutput,
        trace_id: str = "",
    ) -> RootCauseOutput:
        """
        Run Root-Cause Agent from ReconciliationOutput to RootCauseOutput.
        """

        if reconciliation_output.error:
            return RootCauseOutput(
                entity=reconciliation_output.entity,
                anomalies=[],
                summary=ReconciliationSummary(),
                trace_id=trace_id,
                error=reconciliation_output.error,
            )

        llm_client = self._get_llm_client()

        try:
            return run_root_cause_agent(
                reconciliation=reconciliation_output,
                trace_id=trace_id,
                llm_client=llm_client,
            )
        except Exception as exc:
            return RootCauseOutput(
                entity=reconciliation_output.entity,
                anomalies=[],
                summary=ReconciliationSummary(),
                trace_id=trace_id,
                error=f"Root-Cause Agent failed: {exc}",
            )

    def _get_llm_client(self) -> Optional[RootCauseLLMClient]:
        """
        Return an LLM client if LLM enhancement is enabled.

        If use_llm=False:
            return None

        If an llm_client was injected:
            reuse it

        Otherwise:
            create RootCauseLLMClient from .env
        """

        if not self.use_llm:
            return None

        if self.llm_client is not None:
            return self.llm_client

        try:
            self.llm_client = RootCauseLLMClient()
            return self.llm_client
        except LLMClientError:
            # If OPENAI_API_KEY is missing or client init fails,
            # fallback to rule-only mode instead of breaking the pipeline.
            return None


class RootCauseOnlyAdapter(PydanticAIAdapter):
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
        if msg.sender_name != "AuditFlow Reconciliation":
            logger.info(
                f"Ignoring message from {msg.sender_name!r} "
                f"(sender_type={msg.sender_type!r}) - not from Reconciliation"
            )
            return
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
    agent_id = os.getenv("ROOTCAUSE_AGENT_ID")
    api_key = os.getenv("ROOTCAUSE_API_KEY")

    if not agent_id or not api_key:
        raise ValueError(
            "ROOTCAUSE_AGENT_ID and ROOTCAUSE_API_KEY must be set in .env\n"
            "These are the credentials from the Band platform for the AuditFlow RootCause agent."
        )

    adapter = RootCauseOnlyAdapter(
        model="openai:gpt-4o-mini",
        custom_section=ROOTCAUSE_SYSTEM_PROMPT,
        additional_tools=[run_root_cause_analysis, reply_to_user],
    )

    agent = Agent.create(
        adapter=adapter,
        agent_id=agent_id,
        api_key=api_key,
    )

    logger.info("RootCause Agent starting - listening for messages in Band room...")
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
