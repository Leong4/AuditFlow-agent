"""
AuditFlow - Finance Agent
==========================
负责查询 Finance mock 数据库，返回回款数据和业务规则。

职责边界：
- 只报告 Finance 系统里的事实和规则
- 不做跨系统判断
- 找不到记录时返回 error 字段

运行方式：
    python3 agents/system/finance_agent.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv
from pydantic_ai import RunContext
from thenvoi import Agent
from thenvoi.core.protocols import AgentToolsProtocol
from thenvoi.core.types import PlatformMessage

# ── 路径设置 ──────────────────────────────────────────────
# 让 Python 能找到 shared/ 目录
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from shared.schemas import FinanceOutput, EntityMatch, MatchMethod  # noqa: E402
from shared.thenvoi_pydantic_compat import PydanticAIAdapter  # noqa: E402

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
_processed_message_ids: set[str] = set()
_active_message_ids_by_room: dict[str, str] = {}
_query_context_by_message_id: dict[str, tuple[str, str, str, str]] = {}
_replied_message_ids: set[str] = set()


def _extract_reply_mode(content: str) -> str:
    reply_mode_match = re.search(r"(?im)^Reply-Mode:\s*(.+)$", content)
    if not reply_mode_match:
        return "user"

    reply_mode = reply_mode_match.group(1).strip().lower()
    if reply_mode not in {"user", "agent"}:
        logger.warning(f"Invalid Reply-Mode {reply_mode!r}; defaulting to 'user'")
        return "user"

    return reply_mode


def _extract_router_query_context(content: str) -> tuple[str, str, str, str] | None:
    query_id_match = re.search(r"(?im)^Query-ID:\s*(.+)$", content)
    entity_match = re.search(r"(?im)^Entity:\s*(.+)$", content)
    time_scope_match = re.search(r"(?im)^Time scope:\s*(.+)$", content)
    if entity_match and time_scope_match:
        query_id = query_id_match.group(1).strip() if query_id_match else ""
        reply_mode = _extract_reply_mode(content)
        return (
            entity_match.group(1).strip(),
            time_scope_match.group(1).strip(),
            query_id,
            reply_mode,
        )

    legacy_match = re.search(
        r"(?is)\bQuery\s+.+?\s+for\s+(.+?),\s*(.+)$",
        content.strip(),
    )
    if legacy_match:
        return legacy_match.group(1).strip(), legacy_match.group(2).strip(), "", "user"

    return None

def _extract_requested_systems(content: str) -> set[str]:
    """
    Extract requested systems from the Router's structured system query.

    Expected line:
        Systems: crm, erp, finance
    """
    systems_match = re.search(r"(?im)^Systems:\s*(.+)$", content)
    if not systems_match:
        return set()

    systems_text = systems_match.group(1).lower()
    return set(re.findall(r"\b(crm|erp|finance)\b", systems_text))


def _is_valid_router_system_query(content: str, own_system: str) -> bool:
    """
    Only accept structured Router system-query messages.

    This prevents System Agents from processing Router status messages such as:
        "I've initiated the reconciliation process..."
    """
    if "auditflow system query" not in content.lower():
        return False

    if _extract_router_query_context(content) is None:
        return False

    requested_systems = _extract_requested_systems(content)
    return own_system in requested_systems

# ── Mock 数据加载 ─────────────────────────────────────────
DATA_PATH = ROOT / "data" / "finance_mock.json"

def load_finance_data() -> list[dict]:
    with open(DATA_PATH) as f:
        return json.load(f)["records"]

FINANCE_RECORDS = load_finance_data()


# ── 查询函数 ──────────────────────────────────────────────

def find_finance_record(entity: str, time_scope: str) -> dict | None:
    """
    在 mock 数据里找匹配的 Finance 记录。
    先做精确匹配，再做模糊匹配（包含关系）。
    """
    entity_lower = entity.lower().strip()

    # 精确匹配
    for record in FINANCE_RECORDS:
        meta = record["metadata"]
        payload = record["payload"]
        if (
            payload["entity"].lower() == entity_lower
            and meta["time_scope"] == time_scope
        ):
            return record

    # 模糊匹配（名称包含关系）
    for record in FINANCE_RECORDS:
        meta = record["metadata"]
        payload = record["payload"]
        stored = payload["entity"].lower()
        if (
            (entity_lower in stored or stored in entity_lower)
            and meta["time_scope"] == time_scope
        ):
            return record

    return None


def build_finance_output(entity: str, time_scope: str) -> FinanceOutput:
    """
    查询 mock 数据，返回 FinanceOutput 对象。
    """
    record = find_finance_record(entity, time_scope)

    if record is None:
        return FinanceOutput(
            entity=entity,
            error=f"No Finance record found for entity='{entity}' time_scope='{time_scope}'"
        )

    p = record["payload"]

    entity_match = EntityMatch(
        query=p["entity_match"]["query"],
        matched_as=p["entity_match"]["matched_as"],
        match_method=MatchMethod(p["entity_match"]["match_method"]),
        confidence=p["entity_match"]["confidence"],
    )

    return FinanceOutput(
        entity=p["entity"],
        entity_match=entity_match,
        payment_id=p.get("payment_id", ""),
        payment_amount=p.get("payment_amount"),
        currency=p.get("currency", "GBP"),
        payment_date=p.get("payment_date", ""),
        payment_method=p.get("payment_method", ""),
        exchange_rate=p.get("exchange_rate"),
        refund_amount=p.get("refund_amount", 0.0),
        tax_deduction=p.get("tax_deduction", 0.0),
        overdue_days=p.get("overdue_days", 0),
        exchange_rate_policy=p.get("exchange_rate_policy", ""),
        data_freshness=p.get("data_freshness", ""),
        error=p.get("error"),
        customer_id=p.get("customer_id", ""),
        contract_id=p.get("contract_id", ""),
        invoice_id=p.get("invoice_id", ""),
        bank_fee=p.get("bank_fee", 0.0),
        original_currency_amount=p.get("original_currency_amount"),
        exchange_rate_date=p.get("exchange_rate_date", ""),
    )


async def query_and_reply_finance(
    ctx: RunContext[AgentToolsProtocol],
    entity: str,
    time_scope: str,
) -> str:
    """
    Query the Finance database and send the raw JSON result back to whoever asked.
    Call this when you receive a data query for Finance. Pass entity and time_scope exactly as stated.
    The JSON response is sent automatically — do NOT call reply_with_data or thenvoi_send_message.
    """
    reply_target = getattr(ctx.deps, "reply_target", None)
    if not reply_target:
        return "Error: no reply target set."

    message_id = getattr(ctx.deps, "current_message_id", None)
    if not message_id:
        message_id = _active_message_ids_by_room.get(getattr(ctx.deps, "room_id", ""))

    query_id = ""
    reply_mode = "user"
    query_context = _query_context_by_message_id.get(message_id or "")
    if query_context is not None:
        entity, time_scope, query_id, reply_mode = query_context

    dedupe_key = message_id or f"{getattr(ctx.deps, 'room_id', 'unknown')}:{reply_target}"
    already_replied = dedupe_key in _replied_message_ids
    logger.info(f"[finance] query_and_reply called — already_replied={already_replied}")
    if already_replied:
        logger.info("Already replied for this query — ignoring duplicate tool call.")
        return "Already replied for this query."
    _replied_message_ids.add(dedupe_key)
    ctx.deps.already_replied = True

    result = build_finance_output(entity, time_scope)
    result.query_id = query_id
    result.reply_mode = reply_mode
    def _enum_safe(obj):
        from enum import Enum
        if isinstance(obj, Enum):
            return obj.value
        raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

    json_str = json.dumps(asdict(result), default=_enum_safe, ensure_ascii=False, indent=2)
    await ctx.deps.get_participants()
    await ctx.deps.send_message(content=json_str, mentions=[reply_target])
    return f"Replied with Finance JSON to {reply_target}"


# ── Finance Agent Prompt ─────────────────────────────────

FINANCE_SYSTEM_PROMPT = """
You are the Finance Agent in the AuditFlow multi-agent reconciliation system. You have access to the Finance database containing payment data, payment methods, exchange rates, and customer information. Always respond in English.

## WHEN TO RESPOND
You only handle data-query tasks. A valid query identifies an entity (company/customer) and a time scope. The incoming message is shown to you as "[SenderName]: ...".
- If the message is a data query (it names an entity and a time scope, or clearly asks for Finance data), handle it.
- If the message is NOT a data query (a greeting, a thank-you, an acknowledgment, another agent's data, or any chit-chat), do NOTHING: do not call any tool, do not reply, output nothing.

## HOW TO HANDLE A QUERY
Call the `query_and_reply_finance` tool with the entity and time_scope. That's all — the tool queries the database, formats the JSON, and sends the reply automatically. Do NOT call any other tool to reply.

## CRITICAL
- NEVER use thenvoi_send_message.
- NEVER call reply_with_data.
- NEVER format or rewrite the data yourself.
- One tool call: query_and_reply_finance. Nothing else.

## YOUR ROLE BOUNDARY (CRITICAL)
Report ONLY what the Finance system contains. Do NOT make cross-system judgments.
- The incoming query may list multiple systems, e.g. "Query crm, erp, finance for Acme Corp". This is a broadcast sent to several agents at once. You must IGNORE the other systems entirely.
- You ONLY look up and report data from YOUR system (Finance). You ALWAYS call query_and_reply_finance, even if the query mentions crm or erp.
- NEVER comment on, report for, or say "no data found" about CRM, ERP, or any system other than your own. Those are other agents' jobs, not yours.
- You are like a witness on the stand — testify ONLY about what you saw in the Finance system. Keep responses concise and structured, and always include the raw numbers.
"""


# ── Tool 定义 ─────────────────────────────────────────────

def query_finance(
    ctx: RunContext[AgentToolsProtocol],
    entity: str,
    time_scope: str,
) -> str:
    """
    Query the Finance mock database for a specific entity and time scope.

    Args:
        entity: Company or customer name to look up (e.g. "Acme Corp")
        time_scope: Time period to query (e.g. "Q1 2026")

    Returns:
        JSON string with Finance data including payment records, deductions, and business rules
    """
    result = build_finance_output(entity, time_scope)
    return json.dumps(result.__dict__, default=str, ensure_ascii=False, indent=2)


# ── Agent 启动 ────────────────────────────────────────────

class TaskOnlyAdapter(PydanticAIAdapter):
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
        is_user = msg.sender_type == "User"
        is_router = msg.sender_name == "AuditFlow Router"

        if not (is_user or is_router):
            logger.info(
                f"Ignoring message from {msg.sender_name!r} "
                f"(sender_type={msg.sender_type!r}) - not a query task"
            )
            return

        message_id = getattr(msg, "id", None)
        if message_id and message_id in _processed_message_ids:
            logger.info(f"Skipping already-processed message {message_id!r}")
            return

        if message_id:
            _processed_message_ids.add(message_id)

        if is_router and not _is_valid_router_system_query(msg.content, "finance"):
            logger.info(
                f"Ignoring Router message {message_id!r} because it is not a valid Finance system query"
            )
            return

        _replied_message_ids.clear()
        if message_id:
            _active_message_ids_by_room[room_id] = message_id

        query_context = _extract_router_query_context(msg.content) if is_router else None

        if query_context is not None:
            _query_context_by_message_id[msg.id] = query_context
            entity, time_scope, _, _ = query_context
            tools.query_entity, tools.query_time_scope = entity, time_scope
        tools.current_message_id = msg.id
        tools.reply_target = msg.sender_name
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
    agent_id = os.getenv("FINANCE_AGENT_ID")
    api_key = os.getenv("FINANCE_API_KEY")

    if not agent_id or not api_key:
        raise ValueError(
            "FINANCE_AGENT_ID and FINANCE_API_KEY must be set in .env\n"
            "These are the credentials from the Band platform for the AuditFlow Finance agent."
        )

    adapter = TaskOnlyAdapter(
        model="openai:gpt-4o-mini",
        custom_section=FINANCE_SYSTEM_PROMPT,
        additional_tools=[query_and_reply_finance],
    )

    agent = Agent.create(
        adapter=adapter,
        agent_id=agent_id,
        api_key=api_key,
    )

    logger.info("Finance Agent starting — listening for messages in Band room...")
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
