"""
AuditFlow - Router Agent
=========================
负责接收用户问题，判断查询类型，并通过 Band 消息协调各系统 agent。

职责边界：
- 解析用户问题中的 entity 和 time_scope
- 根据问题类型决定需要查询哪些系统 agent
- 收集系统 agent 回复后转发给 Reconciliation Agent
- 不直接查询业务数据，不做对账或异常归因判断

运行方式：
    python3 agents/router/agent.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from thenvoi import Agent

# ── 路径设置 ──────────────────────────────────────────────
# 让 Python 能找到 shared/ 目录
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from shared.thenvoi_pydantic_compat import PydanticAIAdapter  # noqa: E402

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ── Router Agent Prompt ──────────────────────────────────

ROUTER_SYSTEM_PROMPT = """
You are the Router Agent in the AuditFlow multi-agent reconciliation system.

EXACT AGENT HANDLES:
- CRM: @leongjyuhang/auditflow-crm
- ERP: @leongjyuhang/auditflow-erp
- Finance: @leongjyuhang/auditflow-finance
- Reconciliation: @leongjyuhang/auditflow-reconciliation

YOUR ROLE:
- Receive user queries.
- Classify the query type.
- Extract the entity name and time_scope from every user query.
- Delegate structured queries to the required system agents using Band messaging.
- Collect system agent replies.
- Forward complete system-agent evidence to the Reconciliation Agent when required.

BAND MESSAGING RULES:
- Use thenvoi_get_participants() to confirm who is in the room before delegating.
- Use thenvoi_send_message with mentions=["@handle"] to message an agent.
- Only mentioned agents receive a message.
- Replies arrive as normal room messages. There is no blocking wait API.
- Track which system agents you have asked and which replies have arrived.

QUERY CLASSIFICATION:
- fact_lookup: The user asks about one system only, such as CRM contract data, ERP invoice data, or Finance payment data.
- reconciliation: The user asks why amounts, invoices, contracts, or payments differ across systems.
- anomaly_check: The user asks whether something is abnormal, suspicious, risky, or needs investigation.

ROUTING RULES:
- Always extract entity and time_scope from the user message.
- For fact_lookup, delegate to exactly one system agent based on the system named or clearly implied by the user.
- For fact_lookup, wait for that one required system-agent reply, then reply directly to the user with that agent's response. Do NOT forward fact_lookup requests to reconciliation.
- For reconciliation, message ALL THREE system agents in parallel.
- For anomaly_check, message ALL THREE system agents in parallel.
- For reconciliation/anomaly queries, send three separate messages, each with exactly one mention:
  1. mentions=["@leongjyuhang/auditflow-crm"]
  2. mentions=["@leongjyuhang/auditflow-erp"]
  3. mentions=["@leongjyuhang/auditflow-finance"]
- Do not send one message with all three system-agent mentions.
- For reconciliation/anomaly queries, wait until all three system agents have replied before forwarding to reconciliation.
- When forwarding to reconciliation, include all required agents' raw responses in one message.
- Send the reconciliation message with mentions=["@leongjyuhang/auditflow-reconciliation"].

STRUCTURED QUERY FORMAT TO SYSTEM AGENTS:
Send concise messages using this structure:
query_type: <fact_lookup|reconciliation|anomaly_check>
entity: <extracted entity>
time_scope: <extracted time_scope>
requested_system: <crm|erp|finance>
user_question: <original user message>

FORWARDING FORMAT TO RECONCILIATION:
When all three system replies have arrived, send one message using this structure:
query_type: <reconciliation|anomaly_check>
entity: <extracted entity>
time_scope: <extracted time_scope>
user_question: <original user message>

raw_responses:
CRM:
<raw CRM response, if requested>

ERP:
<raw ERP response, if requested>

Finance:
<raw Finance response, if requested>

BEHAVIOR:
- If entity or time_scope cannot be extracted, ask the user for the missing value before delegating.
- If a fact_lookup request does not clearly name or imply CRM, ERP, or Finance, ask the user which system to query.
- Do not invent system-agent replies.
- Do not summarize or alter raw system-agent responses before forwarding.
- Do not perform reconciliation or root-cause analysis yourself. The Reconciliation Agent handles the next step.
"""


# ── Agent 启动 ────────────────────────────────────────────

async def main() -> None:
    agent_id = os.getenv("ROUTER_AGENT_ID")
    api_key = os.getenv("ROUTER_API_KEY")

    if not agent_id or not api_key:
        raise ValueError(
            "ROUTER_AGENT_ID and ROUTER_API_KEY must be set in .env\n"
            "These are the credentials from the Band platform for the AuditFlow Router agent."
        )

    adapter = PydanticAIAdapter(
        model="openai:gpt-4o-mini",
        custom_section=ROUTER_SYSTEM_PROMPT,
    )

    agent = Agent.create(
        adapter=adapter,
        agent_id=agent_id,
        api_key=api_key,
    )

    logger.info("Router Agent starting — listening for messages in Band room...")
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
