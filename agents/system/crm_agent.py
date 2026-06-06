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
import sys
from pathlib import Path

from dotenv import load_dotenv
from pydantic_ai import RunContext
from thenvoi import Agent
from thenvoi.core.protocols import AgentToolsProtocol

# ── 路径设置 ──────────────────────────────────────────────
# 让 Python 能找到 shared/ 目录
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from shared.schemas import CRMOutput, EntityMatch, MatchMethod  # noqa: E402
from shared.thenvoi_pydantic_compat import PydanticAIAdapter  # noqa: E402

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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


# ── CRM Agent Prompt ──────────────────────────────────────

CRM_SYSTEM_PROMPT = """
You are the CRM Agent in the AuditFlow multi-agent reconciliation system.

YOUR ROLE:
- You have access to the CRM database containing contract data, payment terms, and customer information.
- When asked about a specific entity and time scope, you query the CRM database and report the facts.
- You report ONLY what the CRM system contains. You do NOT make cross-system judgments.

HOW TO RESPOND:
When you receive a query, use the query_crm tool to look up the data, then respond with:
1. The contract amount and currency
2. Payment terms and business rules
3. Any entity name matching details
4. Data freshness date
5. If no record is found, clearly state that

Keep responses concise and structured. Always include the raw numbers.
You are like a witness on the stand — report only what you saw in the CRM system.
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

async def main() -> None:
    agent_id = os.getenv("CRM_AGENT_ID")
    api_key = os.getenv("CRM_API_KEY")

    if not agent_id or not api_key:
        raise ValueError(
            "CRM_AGENT_ID and CRM_API_KEY must be set in .env\n"
            "These are the credentials from the Band platform for the AuditFlow CRM agent."
        )

    adapter = PydanticAIAdapter(
        model="openai:gpt-4o-mini",
        custom_section=CRM_SYSTEM_PROMPT,
        additional_tools=[query_crm],
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
