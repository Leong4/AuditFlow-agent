"""
AuditFlow - CRM Agent
======================
负责查询 CRM mock 数据库，返回合同数据和业务规则。

职责边界：
- 只报告 CRM 系统里的事实和规则
- 不做跨系统判断
- 找不到记录时返回 error 字段

运行方式：
    python3 agents/system/crm_agent.py
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

from shared.schemas import CRMOutput, EntityMatch, MatchMethod  # noqa: E402
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

# ── Mock 数据加载 ─────────────────────────────────────────
DATA_PATH = ROOT / "data" / "crm_mock.json"

def load_crm_data() -> list[dict]:
    with open(DATA_PATH) as f:
        return json.load(f)["records"]

CRM_RECORDS = load_crm_data()


# ── 查询函数 ──────────────────────────────────────────────

def find_crm_record(entity: str, time_scope: str) -> dict | None:
    """
    在 mock 数据里找匹配的 CRM 记录。
    先做精确匹配，再做模糊匹配（包含关系）。
    """
    entity_lower = entity.lower().strip()

    # 精确匹配
    for record in CRM_RECORDS:
        meta = record["metadata"]
        payload = record["payload"]
        if (
            payload["entity"].lower() == entity_lower
            and meta["time_scope"] == time_scope
        ):
            return record

    # 模糊匹配（名称包含关系）
    for record in CRM_RECORDS:
        meta = record["metadata"]
        payload = record["payload"]
        stored = payload["entity"].lower()
        if (
            (entity_lower in stored or stored in entity_lower)
            and meta["time_scope"] == time_scope
        ):
            return record

    return None


def build_crm_output(entity: str, time_scope: str) -> CRMOutput:
    """
    查询 mock 数据，返回 CRMOutput 对象。
    """
    record = find_crm_record(entity, time_scope)

    if record is None:
        return CRMOutput(
            entity=entity,
            error=f"No CRM record found for entity='{entity}' time_scope='{time_scope}'"
        )

    p = record["payload"]

    entity_match = EntityMatch(
        query=p["entity_match"]["query"],
        matched_as=p["entity_match"]["matched_as"],
        match_method=MatchMethod(p["entity_match"]["match_method"]),
        confidence=p["entity_match"]["confidence"],
    )

    return CRMOutput(
        entity=p["entity"],
        entity_match=entity_match,
        contract_amount=p.get("contract_amount"),
        currency=p.get("currency", "GBP"),
        sign_date=p.get("sign_date", ""),
        status=p.get("status", ""),
        sales_owner=p.get("sales_owner", ""),
        payment_terms=p.get("payment_terms", ""),
        exchange_rate_policy=p.get("exchange_rate_policy", ""),
        late_payment_grace_period=p.get("late_payment_grace_period", ""),
        data_freshness=p.get("data_freshness", ""),
        error=p.get("error"),
        customer_id=p.get("customer_id", ""),
        contract_id=p.get("contract_id", ""),
    )


async def query_and_reply_crm(
    ctx: RunContext[AgentToolsProtocol],
    entity: str,
    time_scope: str,
) -> str:
    """
    Query the CRM database and send the raw JSON result back to whoever asked.
    Call this when you receive a data query for CRM. Pass entity and time_scope exactly as stated.
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
    logger.info(f"[crm] query_and_reply called — already_replied={already_replied}")
    if already_replied:
        logger.info("Already replied for this query — ignoring duplicate tool call.")
        return "Already replied for this query."
    _replied_message_ids.add(dedupe_key)
    ctx.deps.already_replied = True

    result = build_crm_output(entity, time_scope)
    def _enum_safe(obj):
        from enum import Enum
        if isinstance(obj, Enum):
            return obj.value
        raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

    json_str = json.dumps(asdict(result), default=_enum_safe, ensure_ascii=False, indent=2)
    await ctx.deps.get_participants()
    await ctx.deps.send_message(content=json_str, mentions=[reply_target])
    return f"Replied with CRM JSON to {reply_target}"


# ── CRM Agent Prompt ──────────────────────────────────────

CRM_SYSTEM_PROMPT = """
You are the CRM Agent in the AuditFlow multi-agent reconciliation system. You have access to the CRM database containing contract data, payment terms, and customer information. Always respond in English.

## WHEN TO RESPOND
You only handle data-query tasks. A valid query identifies an entity (company/customer) and a time scope. The incoming message is shown to you as "[SenderName]: ...".
- If the message is a data query (it names an entity and a time scope, or clearly asks for CRM data), handle it.
- If the message is NOT a data query (a greeting, a thank-you, an acknowledgment, another agent's data, or any chit-chat), do NOTHING: do not call any tool, do not reply, output nothing.

## HOW TO HANDLE A QUERY
Call the `query_and_reply_crm` tool with the entity and time_scope. That's all — the tool queries the database, formats the JSON, and sends the reply automatically. Do NOT call any other tool to reply.

## CRITICAL
- NEVER use thenvoi_send_message.
- NEVER call reply_with_data.
- NEVER format or rewrite the data yourself.
- One tool call: query_and_reply_crm. Nothing else.

## YOUR ROLE BOUNDARY (CRITICAL)
Report ONLY what the CRM system contains. Do NOT make cross-system judgments.
- The incoming query may list multiple systems, e.g. "Query crm, erp, finance for Acme Corp". This is a broadcast sent to several agents at once. You must IGNORE the other systems entirely.
- You ONLY look up and report data from YOUR system (CRM). You ALWAYS call query_and_reply_crm, even if the query mentions erp or finance.
- NEVER comment on, report for, or say "no data found" about ERP, Finance, or any system other than your own. Those are other agents' jobs, not yours.
- You are like a witness on the stand — testify ONLY about what you saw in the CRM system. Keep responses concise and structured, and always include the raw numbers.
"""


# ── Tool 定义 ─────────────────────────────────────────────

def query_crm(
    ctx: RunContext[AgentToolsProtocol],
    entity: str,
    time_scope: str,
) -> str:
    """
    Query the CRM mock database for a specific entity and time scope.

    Args:
        entity: Company or customer name to look up (e.g. "Acme Corp")
        time_scope: Time period to query (e.g. "Q1 2026")

    Returns:
        JSON string with CRM data including contract amount, payment terms, and business rules
    """
    result = build_crm_output(entity, time_scope)
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
        if msg.id in _processed_message_ids:
            logger.info(f"Skipping already-processed message {msg.id!r}")
            return
        _processed_message_ids.add(msg.id)
        _replied_message_ids.clear()
        _active_message_ids_by_room[room_id] = msg.id
        query_context = _extract_router_query_context(msg.content)
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
    agent_id = os.getenv("CRM_AGENT_ID")
    api_key = os.getenv("CRM_API_KEY")

    if not agent_id or not api_key:
        raise ValueError(
            "CRM_AGENT_ID and CRM_API_KEY must be set in .env\n"
            "These are the credentials from the Band platform for the AuditFlow CRM agent."
        )

    adapter = TaskOnlyAdapter(
        model="openai:gpt-4o-mini",
        custom_section=CRM_SYSTEM_PROMPT,
        additional_tools=[query_and_reply_crm],
    )

    agent = Agent.create(
        adapter=adapter,
        agent_id=agent_id,
        api_key=api_key,
    )

    logger.info("CRM Agent starting — listening for messages in Band room...")
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
