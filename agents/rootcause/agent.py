from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import re
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
_replied_message_ids: set[str] = set()
_rootcause_output_by_message_id: dict[str, RootCauseOutput] = {}
_reply_mode_by_message_id: dict[str, str] = {}
DEMO_USER_MENTION = "AuditFlow Demo User"


ROOTCAUSE_SYSTEM_PROMPT = """
You are the Root-Cause Agent in the AuditFlow multi-agent reconciliation system.

YOUR ROLE:
- You receive ReconciliationOutput JSON from AuditFlow Reconciliation.
- You reconstruct a ReconciliationOutput object from that JSON.
- You call the run_root_cause_analysis tool to analyze discrepancy causes.
- You use RootCauseOutput internally, but the final message to the user must be a plain English summary. You do NOT rerun reconciliation.

IMPORTANT RULES:
1. Only respond if the message contains a ReconciliationOutput JSON with a discrepancies field.
2. If the message is a thank-you, acknowledgment, greeting, or any non-data message, do not reply at all.
3. For EVERY message containing reconciliation data, your FIRST action MUST be to call `run_root_cause_analysis` with the reconciliation JSON.
4. NEVER call `reply_to_user` before calling `run_root_cause_analysis`.
5. NEVER write your own summary from reconciliation data before calling `run_root_cause_analysis`, even when the data looks clean or fully consistent.
6. After running root cause analysis, send a plain English summary back to the room mentioning the user directly unless the tool reports that it already replied.

## HOW TO RESPOND
- For both clean/no-discrepancy cases and anomaly cases, call `run_root_cause_analysis` with the reconciliation JSON as reconciliation_data before doing anything else.
- Even if the reconciliation data has no discrepancies or all fields appear consistent, do not shortcut to a manual "everything matches" reply; call `run_root_cause_analysis` first.
- If the return value starts with "CLEAN_CASE_ALREADY_REPLIED", STOP. Do not call `reply_to_user`; the no-discrepancy reply has already been sent.
- Otherwise, call `reply_to_user` with a plain English summary (NOT raw JSON). Structure your reply like this:

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


def _normalize_reply_mode(value: object) -> str:
    reply_mode = str(value or "user").strip().lower()
    if reply_mode not in {"user", "agent"}:
        logger.warning(f"Invalid reply_mode {reply_mode!r}; defaulting to 'user'")
        return "user"
    return reply_mode


def _extract_reply_mode_from_text(raw_text: str) -> str:
    reply_mode_match = re.search(r"(?im)^Reply-Mode:\s*(.+)$", raw_text)
    if reply_mode_match:
        return _normalize_reply_mode(reply_mode_match.group(1))

    try:
        parsed = _extract_dict_from_text(raw_text)
    except ValueError:
        return "user"

    payload = _find_reconciliation_payload(parsed)
    if payload is not None:
        return _normalize_reply_mode(payload.get("reply_mode", "user"))

    if isinstance(parsed, dict):
        return _normalize_reply_mode(parsed.get("reply_mode", "user"))

    return "user"


def _reply_mentions(participants: list[dict], reply_mode: str) -> list[str]:
    if _normalize_reply_mode(reply_mode) == "agent":
        return [DEMO_USER_MENTION]

    return [
        p["name"] for p in participants
        if p.get("type") == "User"
    ]


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

    raw_reply_mode = payload.get("reply_mode")
    if not raw_reply_mode and isinstance(parsed, dict):
        raw_reply_mode = parsed.get("reply_mode")
    if not raw_reply_mode:
        raw_reply_mode = _extract_reply_mode_from_text(raw_text)

    reconciliation_output = ReconciliationOutput(
        entity=payload.get("entity", ""),
        entity_consistency=_build_entity_consistency(payload.get("entity_consistency")),
        discrepancies=_build_discrepancies(payload.get("discrepancies")),
        matched=_build_matched_fields(payload.get("matched", [])),
        error=payload.get("error"),
        query_id=str(payload.get("query_id", "")),
        reply_mode=_normalize_reply_mode(raw_reply_mode),
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
    ctx.deps.current_reply_mode = reconciliation_output.reply_mode
    logger.info(
        f"run_root_cause_analysis started for entity={reconciliation_output.entity!r}"
    )
    output = RootCauseAgent().run(reconciliation_output, trace_id=trace_id)
    message_id = getattr(ctx.deps, "current_message_id", None)
    if message_id:
        _rootcause_output_by_message_id[message_id] = output
        _reply_mode_by_message_id[message_id] = reconciliation_output.reply_mode

    is_clean = (
        not output.error
        and output.summary is not None
        and output.summary.total_discrepancies == 0
        and len(output.anomalies) == 0
    )
    if is_clean and message_id:
        if message_id in _replied_message_ids:
            logger.info(
                f"Clean-case deterministic reply skipped; "
                f"message_id={message_id!r} already replied"
            )
            return (
                "CLEAN_CASE_ALREADY_REPLIED: A deterministic no-discrepancy "
                "reply has already been sent to the user. Do not call reply_to_user."
            )

        try:
            content = format_rootcause_reply(output)
            await ctx.deps.get_participants()
            user_mentions = _reply_mentions(
                ctx.deps.participants,
                reconciliation_output.reply_mode,
            )
            if user_mentions:
                await ctx.deps.send_message(content=content, mentions=user_mentions)
                _replied_message_ids.add(message_id)
                logger.info(
                    f"Clean-case deterministic reply sent for "
                    f"message_id={message_id!r} to user(s): {user_mentions}"
                )
                return (
                    "CLEAN_CASE_ALREADY_REPLIED: A deterministic no-discrepancy "
                    "reply has already been sent to the user. Do not call reply_to_user."
                )

            logger.warning(
                f"Clean-case deterministic reply found no target participants for "
                f"message_id={message_id!r}; falling through to LLM reply path"
            )
        except Exception as exc:
            logger.exception(
                f"Clean-case deterministic reply failed for "
                f"message_id={message_id!r}: {exc}; falling through to LLM reply path"
            )

    result = json.dumps(_json_safe(output), ensure_ascii=False, indent=2)
    logger.info(
        f"run_root_cause_analysis completed for entity={reconciliation_output.entity!r}; "
        "returning result to LLM"
    )
    return result

def _looks_like_rootcause_output_json(content: str) -> bool:
    """
    Detect whether the model is trying to send raw RootCauseOutput JSON
    directly to the user.

    Soft guard only:
    - If this returns True, reply_to_user will NOT send the message.
    - The outer LLM should convert the JSON into a plain English summary
      and call reply_to_user again.
    """
    text = content.strip()

    if not text.startswith("{"):
        return False

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return False

    if not isinstance(data, dict):
        return False

    return (
        "entity" in data
        and "summary" in data
        and "anomalies" in data
        and "error" in data
    )


def _display_value(value: Any) -> str:
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def format_rootcause_reply(output: RootCauseOutput) -> str:
    entity = output.entity or "Unknown entity"
    lines = [f"**AuditFlow Root Cause Analysis — {entity}**", ""]
    if output.query_id:
        lines.extend([f"Query-ID: {output.query_id}", ""])

    if output.error:
        lines.extend([
            "**Summary:** Root cause analysis could not be completed.",
            "",
            f"Error: {output.error}",
        ])
        return "\n".join(lines)

    summary = output.summary
    total = summary.total_discrepancies if summary is not None else len(output.anomalies)
    normal = summary.normal if summary is not None else 0
    anomaly = summary.anomaly if summary is not None else 0
    watch = summary.watch if summary is not None else 0

    lines.append(
        f"**Summary:** {total} discrepancies found. "
        f"Normal: {normal}, Anomaly: {anomaly}, Watch: {watch}."
    )

    if total == 0 or not output.anomalies:
        lines.extend([
            "",
            "All fields are consistent across CRM, ERP, and Finance. No action required.",
        ])
        return "\n".join(lines)

    for anomaly_item in output.anomalies:
        lines.extend([
            "",
            f"- **{anomaly_item.field_pair}**: {anomaly_item.probable_cause}. "
            f"Status: {_display_value(anomaly_item.status)}. "
            f"Risk: {_display_value(anomaly_item.risk_level)}.",
        ])

        if anomaly_item.evidence:
            evidence = "; ".join(anomaly_item.evidence[:3])
            lines.append(f"  Evidence: {evidence}.")

        action = anomaly_item.recommended_action or "Review the discrepancy and confirm the correct source data."
        lines.append(f"  Recommended action: {action}")

    return "\n".join(lines)


async def _maybe_send_fallback_reply(
    tools: AgentToolsProtocol,
    message_id: str,
) -> bool:
    if message_id in _replied_message_ids:
        return False

    try:
        output = _rootcause_output_by_message_id.get(message_id)
        if output is None:
            logger.warning(
                f"No rootcause output cached for message_id={message_id!r}; "
                "cannot send fallback"
            )
            return False

        content = format_rootcause_reply(output)
        await tools.get_participants()
        reply_mode = getattr(
            tools,
            "current_reply_mode",
            _reply_mode_by_message_id.get(message_id, "user"),
        )
        user_mentions = _reply_mentions(tools.participants, reply_mode)
        if not user_mentions:
            logger.warning(
                f"Fallback reply found no target participants for "
                f"message_id={message_id!r}; reply was not sent"
            )
            return False

        await tools.send_message(content=content, mentions=user_mentions)
        _replied_message_ids.add(message_id)
        logger.info(
            f"Fallback reply sent for message_id={message_id!r} "
            f"to user(s): {user_mentions}"
        )
        return True
    except Exception as exc:
        logger.exception(
            f"Fallback reply failed for message_id={message_id!r}: {exc}"
        )
        return False


async def reply_to_user(
    ctx: RunContext[AgentToolsProtocol],
    content: str,
    reply_mode: str = "user",
) -> str:
    """
    Send the final root cause analysis result back to the user in the room.
    Always use this tool to reply. Do NOT use thenvoi_send_message.

    IMPORTANT:
    - Pass a plain English user-facing summary only.
    - Do NOT pass raw RootCauseOutput JSON.
    - If you receive RootCauseOutput JSON from run_root_cause_analysis,
      summarize it first in plain English, then call this tool.
    """
    message_id = getattr(ctx.deps, "current_message_id", None)
    if message_id and message_id in _replied_message_ids:
        logger.info(
            f"reply_to_user skipped; message_id={message_id!r} already replied"
        )
        return "Already replied to user for this message; not sending again."

    looks_like_raw_json = _looks_like_rootcause_output_json(content)
    logger.info(
        f"reply_to_user called; looks_like_raw_rootcause_json={looks_like_raw_json}"
    )

    if looks_like_raw_json:
        logger.warning(
            "reply_to_user rejected raw RootCauseOutput JSON; reply was not sent"
        )
        return (
            "Error: reply_to_user received raw RootCauseOutput JSON. "
            "Do not send raw JSON to the user. Convert it into the required "
            "plain English summary format described in HOW TO RESPOND, then "
            "call reply_to_user again."
        )

    effective_reply_mode = getattr(ctx.deps, "current_reply_mode", reply_mode)
    await ctx.deps.get_participants()
    user_mentions = _reply_mentions(ctx.deps.participants, effective_reply_mode)
    if not user_mentions:
        logger.warning("reply_to_user found no user participants; reply was not sent")
        return "Error: no user found in room to reply to."

    await ctx.deps.send_message(content=content, mentions=user_mentions)
    logger.info(f"reply_to_user sent reply to user(s): {user_mentions}")
    if message_id:
        _replied_message_ids.add(message_id)
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
                query_id=reconciliation_output.query_id,
                error=reconciliation_output.error,
                reply_mode=reconciliation_output.reply_mode,
            )

        llm_client = self._get_llm_client()

        try:
            output = run_root_cause_agent(
                reconciliation=reconciliation_output,
                trace_id=trace_id,
                llm_client=llm_client,
            )
            output.reply_mode = reconciliation_output.reply_mode
            output.query_id = reconciliation_output.query_id
            return output
        except Exception as exc:
            return RootCauseOutput(
                entity=reconciliation_output.entity,
                anomalies=[],
                summary=ReconciliationSummary(),
                trace_id=trace_id,
                query_id=reconciliation_output.query_id,
                error=f"Root-Cause Agent failed: {exc}",
                reply_mode=reconciliation_output.reply_mode,
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
        logger.info(
            f"Accepted RootCause message from {msg.sender_name!r} in room {room_id!r}"
        )
        message_id = msg.id
        _replied_message_ids.discard(message_id)
        _rootcause_output_by_message_id.pop(message_id, None)
        _reply_mode_by_message_id.pop(message_id, None)
        tools.current_message_id = message_id
        tools.current_reply_mode = _extract_reply_mode_from_text(msg.content)

        try:
            await super().on_message(
                msg,
                tools,
                history,
                participants_msg,
                contacts_msg,
                is_session_bootstrap=is_session_bootstrap,
                room_id=room_id,
            )

            if message_id in _replied_message_ids:
                return

            await _maybe_send_fallback_reply(tools, message_id)
        finally:
            _rootcause_output_by_message_id.pop(message_id, None)
            _reply_mode_by_message_id.pop(message_id, None)
            _replied_message_ids.discard(message_id)


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
