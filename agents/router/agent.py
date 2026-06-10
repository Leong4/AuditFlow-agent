"""
AuditFlow - Router Agent
=========================
Receives user questions, classifies routing needs, and coordinates system
agents through Band messages.

Responsibilities:
- Extract entity and time_scope from user questions
- Classify requests as fact_lookup, reconciliation, or anomaly_check
- Delegate to AuditFlow system agents using Band platform tools
- Forward complete system-agent evidence to AuditFlow Reconciliation
- Never query business data directly and never perform reconciliation analysis

Run with:
    python3 agents/router/agent.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from pydantic_ai import RunContext
from thenvoi import Agent
from thenvoi.core.protocols import AgentToolsProtocol
from thenvoi.core.types import PlatformMessage

# Path setup: allow imports from shared/.
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from shared.thenvoi_pydantic_compat import PydanticAIAdapter  # noqa: E402

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# Router Agent Prompt

ROUTER_SYSTEM_PROMPT = """
You are the AuditFlow Router. Your ONLY job is to route user questions to the correct system agents by calling the `query_systems` tool. You never answer questions yourself, never explain data, and never perform analysis. Always respond in English.

## QUERYING SYSTEMS
To query systems, use ONLY the `query_systems(entity, time_scope, systems)` tool.
- `entity`: the company/customer name, extracted EXACTLY as the user wrote it.
- `time_scope`: the time period, extracted EXACTLY as the user wrote it. Never invent, convert, or assume a time period. If the user said "Q1 2026", pass "Q1 2026".
- `systems`: a list containing one or more of "crm", "erp", "finance".

CRITICAL — CALL query_systems EXACTLY ONCE PER USER REQUEST:
- You must call `query_systems` ONE time only per user question, with ALL needed systems in a single list.
- After calling query_systems, STOP. Do not call it again. Do not call any other tool.
- Wait silently for the system agents to reply. You will be notified when data is ready.
- If you feel the urge to call query_systems again, RESIST. One call is always enough.

You never @mention system agents yourself, and forwarding the collected data to Reconciliation happens automatically outside of you. You must never try to contact AuditFlow CRM, ERP, Finance, Reconciliation, or RootCause directly.

## HOW TO CHOOSE `systems`
Decide which systems based on what the question is about:
- "crm"     -> contract amount, payment terms, sign date, customer info
- "erp"     -> invoice amount, due date, delivery status, installment info
- "finance" -> payment received, refunds, bank fees, exchange rate, overdue days

Number of systems depends on the question:
- ONE system: the user asks about a single system's facts only.
    e.g. "What is Acme Corp's contract amount?" -> ["crm"]
    e.g. "When is this invoice due?" -> ["erp"]
    e.g. "How much did Acme pay last quarter?" -> ["finance"]
- TWO systems: the question compares exactly two systems.
    e.g. "Why is the invoice amount different from the payment received?" -> ["erp", "finance"]
    e.g. "Contract is signed but not yet invoiced?" -> ["crm", "erp"]
- THREE systems: full reconciliation or anomaly check across the whole chain.
    e.g. "Why don't the contract, invoice, and payment match?" -> ["crm", "erp", "finance"]
    e.g. "Is there anything abnormal with this Acme transaction?" -> ["crm", "erp", "finance"]
    e.g. "Reconcile Acme Corp for Q1 2026." -> ["crm", "erp", "finance"]

## WHEN THE QUESTION IS TOO VAGUE
If the question is too vague to determine which systems to query (e.g. "Is Acme Corp okay?", "Check this customer", "How are things lately?"), do NOT call `query_systems` and do NOT guess. Instead, ask the user one clarifying question (see next section). If the user's answer is still too vague, keep asking clarifying questions until you have enough to choose the systems. Only call `query_systems` once you know which systems are needed. Never guess the systems.

## REPLYING TO THE USER (clarification only)
To ask the user a clarifying question, use the `thenvoi_send_message(content, mentions)` tool. Use it for this ONE purpose only.
- The incoming user message is shown to you as "[SenderName]: question". To reply, mention that same sender by their display name. Example: if you see "[Dan]: is Acme okay?", call thenvoi_send_message(content="Are you asking about the contract terms, the invoice status, or the payment received?", mentions=["Dan"]).
- Only ever mention the user who just asked. Never mention yourself, system agents, Reconciliation, RootCause, or anyone else with this tool.
- Never use thenvoi_send_message to answer questions, explain data, forward results, or contact system agents. System queries go through query_systems only; forwarding to Reconciliation is automatic.

## MESSAGES YOU MUST IGNORE
You will see messages in the room that are NOT user questions — for example replies or results from AuditFlow Reconciliation, AuditFlow RootCause, or acknowledgments and thank-you messages from other agents. These are NOT tasks for you. When a message is not a user question that requires routing, do nothing: do not call any tool, do not reply, output nothing. Only act on genuine user questions that need routing.

## SUMMARY OF YOUR BEHAVIOR
- User question, clear -> call `query_systems` with the right entity, time_scope, systems.
- User question, too vague -> ask one clarifying question with thenvoi_send_message, mentioning only the user who asked.
- Anything else (agent replies, results, acknowledgments) -> do nothing.
- Never answer, explain, analyze, or summarize data yourself. Always respond in English.
"""

pending_queries: dict[str, dict[str, object]] = {}

# System agent display names, used by the counting adapter (added later) to
# identify which incoming replies are system-agent data responses.
SYSTEM_AGENT_NAMES_SET = {"AuditFlow CRM", "AuditFlow ERP", "AuditFlow Finance"}


async def query_systems(
    ctx: RunContext[AgentToolsProtocol],
    entity: str,
    time_scope: str,
    systems: list[str],
) -> str:
    """
    Query one or more AuditFlow system agents.

    Choose systems based on what the question is about:
    - "crm"     : contract amount, payment terms, sign date, customer info
    - "erp"     : invoice amount, due date, delivery status, installment info
    - "finance" : payment received, refunds, bank fees, exchange rate, overdue days

    Pass only the systems relevant to the question:
    - fact_lookup touching one system      -> one system, e.g. ["crm"]
    - cross-system discrepancy (2 systems) -> e.g. ["erp", "finance"]
    - full reconciliation or anomaly check -> ["crm", "erp", "finance"]
    """
    key = f"{entity}_{time_scope}"
    if key in pending_queries and len(pending_queries[key]["received"]) < pending_queries[key]["expected"]:
        logger.info(f"query_systems called again for existing pending query {key!r} — skipping duplicate")
        return f"Query already in progress for {entity}, {time_scope}"

    await ctx.deps.get_participants()
    SYSTEM_AGENT_NAMES = {
        "crm": "AuditFlow CRM",
        "erp": "AuditFlow ERP",
        "finance": "AuditFlow Finance",
    }
    mentions = [SYSTEM_AGENT_NAMES[system] for system in systems]
    await ctx.deps.send_message(
        content=(
            "AuditFlow system query\n"
            f"Entity: {entity}\n"
            f"Time scope: {time_scope}\n"
            f"Systems: {', '.join(systems)}"
        ),
        mentions=mentions,
    )
    existing = pending_queries.get(key)
    if existing is not None and len(existing["received"]) < existing["expected"]:
        # An active query already exists for this key — merge instead of overwrite,
        # so multiple query_systems calls in one turn don't shrink the expected count.
        existing["expected"] = max(existing["expected"], len(existing["received"]) + len(mentions))
    else:
        pending_queries[key] = {
            "expected": len(systems),
            "received": {},
            "entity": entity,
            "time_scope": time_scope,
        }
    return f"Query sent to: {mentions}"


class CountingAdapter(PydanticAIAdapter):
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
        logger.info(f"on_message from: {msg.sender_name!r}")
        if msg.sender_name in SYSTEM_AGENT_NAMES_SET:
            active_key = None
            active_entry = None
            for key, entry in pending_queries.items():
                if len(entry["received"]) < entry["expected"]:
                    active_key = key
                    active_entry = entry
                    break

            if active_entry is None:
                logger.warning("Received system-agent reply but no pending query is active")
                return

            active_entry["received"][msg.sender_name] = msg.content
            logger.info(
                f"Received reply from {msg.sender_name} "
                f"({len(active_entry['received'])}/{active_entry['expected']})"
            )

            if len(active_entry["received"]) == active_entry["expected"]:
                forward_content = (
                    f"Reconcile the following data for {active_entry['entity']}, {active_entry['time_scope']}:\n\n"
                    + "\n\n".join(
                        f"[{name}]:\n{content}"
                        for name, content in active_entry["received"].items()
                    )
                )
                await tools.get_participants()
                await tools.send_message(
                    content=forward_content,
                    mentions=["AuditFlow Reconciliation"],
                )
                logger.info(
                    f"Forwarded to Reconciliation for {active_entry['entity']}, "
                    f"{active_entry['time_scope']}"
                )
                del pending_queries[active_key]

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


# Agent startup

async def main() -> None:
    agent_id = os.getenv("ROUTER_AGENT_ID")
    api_key = os.getenv("ROUTER_API_KEY")

    if not agent_id or not api_key:
        raise ValueError(
            "ROUTER_AGENT_ID and ROUTER_API_KEY must be set in .env\n"
            "These are the credentials from the Band platform for the AuditFlow Router agent."
        )

    print(ROUTER_SYSTEM_PROMPT)

    adapter = CountingAdapter(
        model="openai:gpt-4o-mini",
        custom_section=ROUTER_SYSTEM_PROMPT,
        additional_tools=[query_systems],
    )

    agent = Agent.create(
        adapter=adapter,
        agent_id=agent_id,
        api_key=api_key,
    )

    logger.info("Router Agent starting - listening for messages in Band room...")
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
