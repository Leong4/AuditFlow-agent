"""
AuditFlow - ERP Agent
======================
负责查询 ERP mock 数据库，返回发票数据和业务规则。

职责边界：
- 只报告 ERP 系统里的事实和规则
- 不做跨系统判断
- 找不到记录时返回 error 字段

运行方式：
    python3 agents/system/erp_agent.py
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

from shared.schemas import ERPOutput, EntityMatch, MatchMethod  # noqa: E402
from shared.thenvoi_pydantic_compat import PydanticAIAdapter  # noqa: E402

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
_processed_message_ids: set[str] = set()
_active_message_ids_by_room: dict[str, str] = {}
_query_context_by_message_id: dict[str, tuple[str, str]] = {}
_replied_message_ids: set[str] = set()


def _extract_router_query_context(content: str) -> tuple[str, str] | None:
    entity_match = re.search(r"(?im)^Entity:\s*(.+)$", content)
    time_scope_match = re.search(r"(?im)^Time scope:\s*(.+)$", content)
    if entity_match and time_scope_match:
        return entity_match.group(1).strip(), time_scope_match.group(1).strip()

    legacy_match = re.search(
        r"(?is)\bQuery\s+.+?\s+for\s+(.+?),\s*(.+)$",
        content.strip(),
    )
    if legacy_match:
        return legacy_match.group(1).strip(), legacy_match.group(2).strip()

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
DATA_PATH = ROOT / "data" / "erp_mock.json"

def load_erp_data() -> list[dict]:
    with open(DATA_PATH) as f:
        return json.load(f)["records"]

ERP_RECORDS = load_erp_data()


# ── 查询函数 ──────────────────────────────────────────────

def find_erp_record(entity: str, time_scope: str) -> dict | None:
    """
    在 mock 数据里找匹配的 ERP 记录。
    先做精确匹配，再做模糊匹配（包含关系）。
    """
    entity_lower = entity.lower().strip()

    # 精确匹配
    for record in ERP_RECORDS:
        meta = record["metadata"]
        payload = record["payload"]
        if (
            payload["entity"].lower() == entity_lower
            and meta["time_scope"] == time_scope
        ):
            return record

    # 模糊匹配（名称包含关系）
    for record in ERP_RECORDS:
        meta = record["metadata"]
        payload = record["payload"]
        stored = payload["entity"].lower()
        if (
            (entity_lower in stored or stored in entity_lower)
            and meta["time_scope"] == time_scope
        ):
            return record

    return None


def build_erp_output(entity: str, time_scope: str) -> ERPOutput:
    """
    查询 mock 数据，返回 ERPOutput 对象。
    """
    record = find_erp_record(entity, time_scope)

    if record is None:
        return ERPOutput(
            entity=entity,
            error=f"No ERP record found for entity='{entity}' time_scope='{time_scope}'"
        )

    p = record["payload"]

    entity_match = EntityMatch(
        query=p["entity_match"]["query"],
        matched_as=p["entity_match"]["matched_as"],
        match_method=MatchMethod(p["entity_match"]["match_method"]),
        confidence=p["entity_match"]["confidence"],
    )

    return ERPOutput(
        entity=p["entity"],
        entity_match=entity_match,
        invoice_id=p.get("invoice_id", ""),
        invoice_amount=p.get("invoice_amount"),
        currency=p.get("currency", "GBP"),
        invoice_date=p.get("invoice_date", ""),
        due_date=p.get("due_date", ""),
        delivery_status=p.get("delivery_status", ""),
        installment_number=p.get("installment_number"),
        invoice_rules=p.get("invoice_rules", ""),
        data_freshness=p.get("data_freshness", ""),
        error=p.get("error"),
        customer_id=p.get("customer_id", ""),
        contract_id=p.get("contract_id", ""),
    )


async def query_and_reply_erp(
    ctx: RunContext[AgentToolsProtocol],
    entity: str,
    time_scope: str,
) -> str:
    """
    Query the ERP database and send the raw JSON result back to whoever asked.
    Call this when you receive a data query for ERP. Pass entity and time_scope exactly as stated.
    The JSON response is sent automatically — do NOT call reply_with_data or thenvoi_send_message.
    """
    reply_target = getattr(ctx.deps, "reply_target", None)
    if not reply_target:
        return "Error: no reply target set."

    message_id = getattr(ctx.deps, "current_message_id", None)
    if not message_id:
        message_id = _active_message_ids_by_room.get(getattr(ctx.deps, "room_id", ""))

    query_context = _query_context_by_message_id.get(message_id or "")
    if query_context is not None:
        entity, time_scope = query_context

    dedupe_key = message_id or f"{getattr(ctx.deps, 'room_id', 'unknown')}:{reply_target}"
    already_replied = dedupe_key in _replied_message_ids
    logger.info(f"[erp] query_and_reply called — already_replied={already_replied}")
    if already_replied:
        logger.info("Already replied for this query — ignoring duplicate tool call.")
        return "Already replied for this query."
    _replied_message_ids.add(dedupe_key)
    ctx.deps.already_replied = True

    result = build_erp_output(entity, time_scope)
    def _enum_safe(obj):
        from enum import Enum
        if isinstance(obj, Enum):
            return obj.value
        raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

    json_str = json.dumps(asdict(result), default=_enum_safe, ensure_ascii=False, indent=2)
    await ctx.deps.get_participants()
    logger.info(f"ERP JSON being sent: {json_str[:500]}")
    await ctx.deps.send_message(content=json_str, mentions=[reply_target])
    return f"Replied with ERP JSON to {reply_target}"


# ── ERP Agent Prompt ──────────────────────────────────────

ERP_SYSTEM_PROMPT = """
You are the ERP Agent in the AuditFlow multi-agent reconciliation system. You have access to the ERP database containing invoice data, due dates, delivery status, and installment information. Always respond in English.

## WHEN TO RESPOND
You only handle data-query tasks. A valid query identifies an entity (company/customer) and a time scope. The incoming message is shown to you as "[SenderName]: ...".
- If the message is a data query (it names an entity and a time scope, or clearly asks for ERP data), handle it.
- If the message is NOT a data query (a greeting, a thank-you, an acknowledgment, another agent's data, or any chit-chat), do NOTHING: do not call any tool, do not reply, output nothing.

## HOW TO HANDLE A QUERY
Call the `query_and_reply_erp` tool with the entity and time_scope. That's all — the tool queries the database, formats the JSON, and sends the reply automatically. Do NOT call any other tool to reply.

## CRITICAL
- NEVER use thenvoi_send_message.
- NEVER call reply_with_data.
- NEVER format or rewrite the data yourself.
- One tool call: query_and_reply_erp. Nothing else.

## YOUR ROLE BOUNDARY (CRITICAL)
Report ONLY what the ERP system contains. Do NOT make cross-system judgments.
- The incoming query may list multiple systems, e.g. "Query crm, erp, finance for Acme Corp". This is a broadcast sent to several agents at once. You must IGNORE the other systems entirely.
- You ONLY look up and report data from YOUR system (ERP). You ALWAYS call query_and_reply_erp, even if the query mentions crm or finance.
- NEVER comment on, report for, or say "no data found" about CRM, Finance, or any system other than your own. Those are other agents' jobs, not yours.
- You are like a witness on the stand — testify ONLY about what you saw in the ERP system. Keep responses concise and structured, and always include the raw numbers.
"""


# ── Tool 定义 ─────────────────────────────────────────────

def query_erp(
    ctx: RunContext[AgentToolsProtocol],
    entity: str,
    time_scope: str,
) -> str:
    """
    Query the ERP mock database for a specific entity and time scope.

    Args:
        entity: Company or customer name to look up (e.g. "Acme Corp")
        time_scope: Time period to query (e.g. "Q1 2026")

    Returns:
        JSON string with ERP data including invoice amount, delivery status, and business rules
    """
    result = build_erp_output(entity, time_scope)
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

        if is_router and not _is_valid_router_system_query(msg.content, "erp"):
            logger.info(
                f"Ignoring Router message {message_id!r} because it is not a valid ERP system query"
            )
            return

        _replied_message_ids.clear()
        if message_id:
            _active_message_ids_by_room[room_id] = message_id

        query_context = _extract_router_query_context(msg.content) if is_router else None

        if query_context is not None:
            _query_context_by_message_id[msg.id] = query_context
            tools.query_entity, tools.query_time_scope = query_context
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
    agent_id = os.getenv("ERP_AGENT_ID")
    api_key = os.getenv("ERP_API_KEY")

    if not agent_id or not api_key:
        raise ValueError(
            "ERP_AGENT_ID and ERP_API_KEY must be set in .env\n"
            "These are the credentials from the Band platform for the AuditFlow ERP agent."
        )

    adapter = TaskOnlyAdapter(
        model="openai:gpt-4o-mini",
        custom_section=ERP_SYSTEM_PROMPT,
        additional_tools=[query_and_reply_erp],
    )

    agent = Agent.create(
        adapter=adapter,
        agent_id=agent_id,
        api_key=api_key,
    )

    logger.info("ERP Agent starting — listening for messages in Band room...")
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
