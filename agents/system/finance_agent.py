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

from shared.schemas import FinanceOutput, EntityMatch, MatchMethod  # noqa: E402
from shared.thenvoi_pydantic_compat import PydanticAIAdapter  # noqa: E402

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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


# ── Finance Agent Prompt ─────────────────────────────────

FINANCE_SYSTEM_PROMPT = """
You are the Finance Agent in the AuditFlow multi-agent reconciliation system.

YOUR ROLE:
- You have access to the Finance database containing payment records, payment methods, exchange-rate details, deductions, refunds, overdue data, and Finance business rules.
- When asked about a specific entity and time scope, you query the Finance database and report the facts.
- You report payment records and Finance business rules only. You do NOT make cross-system judgments.

HOW TO RESPOND:
When you receive a query, use the query_finance tool to look up the data, then respond with:
1. The payment ID, payment amount, and currency
2. Payment date and payment method
3. Exchange rate, refund amount, tax deduction, and overdue days
4. Finance business rules
5. Any entity name matching details
6. Data freshness date
7. If no record is found, clearly state that

Keep responses concise and structured. Always include the raw numbers.
You are like a witness on the stand — report only what you saw in the Finance system.
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

async def main() -> None:
    agent_id = os.getenv("FINANCE_AGENT_ID")
    api_key = os.getenv("FINANCE_API_KEY")

    if not agent_id or not api_key:
        raise ValueError(
            "FINANCE_AGENT_ID and FINANCE_API_KEY must be set in .env\n"
            "These are the credentials from the Band platform for the AuditFlow Finance agent."
        )

    adapter = PydanticAIAdapter(
        model="openai:gpt-4o-mini",
        custom_section=FINANCE_SYSTEM_PROMPT,
        additional_tools=[query_finance],
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
