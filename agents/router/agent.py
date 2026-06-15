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
import time

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
To query systems, use ONLY the `query_systems(entity, time_scope, is_reconciliation, fields_mentioned)` tool.
- `entity`: the company/customer name, extracted EXACTLY as the user wrote it.
- `time_scope`: the time period, extracted EXACTLY as the user wrote it. Never invent, convert, or assume a time period. If the user said "Q1 2026", pass "Q1 2026".
- `is_reconciliation`: set to true if the user wants a reconciliation, an anomaly/consistency check, or any comparison across systems. Set to false if the user is asking for a single specific fact.
- `fields_mentioned`: the specific business field names the user asked about. Only needed when is_reconciliation is false.

CRITICAL — CALL query_systems EXACTLY ONCE PER USER REQUEST:
- You must call `query_systems` ONE time only per user question.
- A repeated reconciliation request is still a new user request. If the user asks "Reconcile X for Y" again, you must call `query_systems` again. Do NOT decide from conversation history that the process is still ongoing.
- After calling query_systems, STOP. Do not call it again. Do not call any other tool.
- Wait silently for the system agents to reply. You will be notified when data is ready.
- Never send progress or status messages such as "the process is ongoing", "already in progress", "the process has been initiated", or "queries have been sent".
- Tool return values are internal. Do not repeat, explain, or paraphrase them to the user.
- If you feel the urge to call query_systems again within the same user request, RESIST. One call is always enough.

You never @mention system agents yourself, and forwarding the collected data to Reconciliation happens automatically outside of you. You must never try to contact AuditFlow CRM, ERP, Finance, Reconciliation, or RootCause directly.

## HOW TO CLASSIFY THE REQUEST
Your job is to fill is_reconciliation and fields_mentioned correctly.

Set `is_reconciliation = true` when the user wants systems compared or checked together:
- "Reconcile Acme Corp for Q1 2026."
- "Why don't the contract, invoice, and payment match?"
- "Is there anything abnormal with this Acme transaction?"
- "Does the invoice match what was paid?"
For these, leave fields_mentioned empty. Code will query all three systems.

Set `is_reconciliation = false` when the user asks for ONE specific fact:
- "What is Acme Corp's contract amount?" -> fields_mentioned=["contract_amount"]
- "When is this invoice due?" -> fields_mentioned=["due_date"]
- "How much did Acme pay last quarter?" -> fields_mentioned=["payment_amount"]
- "What is the invoice amount for Acme?" -> fields_mentioned=["invoice_amount"]

Use these exact field names where possible:
- CRM fields: contract_amount, payment_terms, sign_date, sales_owner, status
- ERP fields: invoice_amount, invoice_id, invoice_date, due_date, delivery_status
- Finance fields: payment_amount, payment_date, bank_fee, tax_deduction, refund_amount, exchange_rate, overdue_days

If the user asks for a single fact but you cannot map it to any field name above, still set is_reconciliation=false and pass your best guess of the field name. If you truly cannot tell what they want, the system will ask them to clarify.

## WHEN THE QUESTION IS TOO VAGUE
If the question is too vague to determine which systems to query (e.g. "Is Acme Corp okay?", "Check this customer", "How are things lately?"), do NOT call `query_systems` and do NOT guess. Instead, ask the user one clarifying question (see next section). If the user's answer is still too vague, keep asking clarifying questions until you have enough to choose the systems. Only call `query_systems` once you know which systems are needed. Never guess the systems.

## REPLYING TO THE USER (clarification only)
To ask the user a clarifying question, use the `ask_user_clarification(content)` tool. Use it for this ONE purpose only.
- Do NOT ask clarification for clear reconciliation requests, repeated reconciliation requests, progress updates, status updates, or "already in progress" messages.
- For clear or repeated reconciliation requests, call `query_systems` again.
- The incoming user message is shown to you as "[SenderName]: question". To reply, mention that same sender by their display name. Example: if you see "[Dan]: is Acme okay?", call ask_user_clarification(content="Are you asking about the contract terms, the invoice status, or the payment received?")
- Only ever mention the user who just asked. Never mention yourself, system agents, Reconciliation, RootCause, or anyone else with this tool.
- Never use thenvoi_send_message to answer questions, explain data, forward results, or contact system agents. System queries go through query_systems only; forwarding to Reconciliation is automatic.

## MESSAGES YOU MUST IGNORE
You will see messages in the room that are NOT user questions — for example replies or results from AuditFlow Reconciliation, AuditFlow RootCause, or acknowledgments and thank-you messages from other agents. These are NOT tasks for you. When a message is not a user question that requires routing, do nothing: do not call any tool, do not reply, output nothing. Only act on genuine user questions that need routing.

## SUMMARY OF YOUR BEHAVIOR
- User question, clear -> call `query_systems` with entity, time_scope, is_reconciliation, and fields_mentioned.
- User question, too vague -> ask one clarifying question with ask_user_clarification, mentioning only the user who asked.
- Anything else (agent replies, results, acknowledgments) -> do nothing.
- Never answer, explain, analyze, or summarize data yourself. Always respond in English.
"""

pending_queries: dict[str, dict[str, object]] = {}
pending_queries_lock = asyncio.Lock()
PENDING_QUERY_TTL_SECONDS = int(
    os.getenv("ROUTER_PENDING_QUERY_TTL_SECONDS", "60")
)

##加入 pending query timeout 机制，用于防止单次 query 在部分 System Agent 不返回时永久等待
def cleanup_stale_pending_queries_locked() -> None:
    """
    Remove pending queries that have been waiting too long.

    This function must be called while holding pending_queries_lock.
    It prevents one failed or incomplete query from blocking all future
    queries forever with "already in progress".
    """
    now = time.monotonic()
    stale_keys: list[str] = []

    for key, entry in list(pending_queries.items()):
        created_at = entry.get("created_at")

        # Backward-compatible safety: if an old entry has no created_at,
        # start counting from now rather than deleting it immediately.
        if not isinstance(created_at, (int, float)):
            entry["created_at"] = now
            continue

        age = now - float(created_at)
        if age > PENDING_QUERY_TTL_SECONDS:
            stale_keys.append(key)

    for key in stale_keys:
        entry = pending_queries.pop(key, None)
        if entry is None:
            continue

        expected_systems = entry.get("expected_systems", set())
        received = entry.get("received", {})

        logger.warning(
            f"Removed stale pending query {key!r} after "
            f"{PENDING_QUERY_TTL_SECONDS}s timeout. "
            f"received={sorted(received.keys()) if isinstance(received, dict) else received}, "
            f"expected={sorted(expected_systems) if isinstance(expected_systems, set) else expected_systems}"
        )

SYSTEM_AGENT_HANDLES = {
    "crm": os.getenv("CRM_MENTION_HANDLE", "AuditFlow CRM"),
    "erp": os.getenv("ERP_MENTION_HANDLE", "AuditFlow ERP"),
    "finance": os.getenv("FINANCE_MENTION_HANDLE", "AuditFlow Finance"),
}

SYSTEM_AGENT_DISPLAY_NAMES = {
    "crm": "AuditFlow CRM",
    "erp": "AuditFlow ERP",
    "finance": "AuditFlow Finance",
}

RECONCILIATION_HANDLE = os.getenv(
    "RECONCILIATION_MENTION_HANDLE",
    "AuditFlow Reconciliation",
)

def system_from_sender(sender_name: str) -> str | None:
    """
    Convert incoming sender names/handles into canonical system names:
    crm / erp / finance.
    """
    sender = sender_name.strip()

    for system, display_name in SYSTEM_AGENT_DISPLAY_NAMES.items():
        if sender == display_name:
            return system

    for system, handle in SYSTEM_AGENT_HANDLES.items():
        if sender == handle:
            return system

    normalized = (
        sender.lower()
        .replace("@", "")
        .replace("_", "-")
        .replace(" ", "-")
    )

    if normalized.endswith("/auditflow-crm") or normalized == "auditflow-crm":
        return "crm"
    if normalized.endswith("/auditflow-erp") or normalized == "auditflow-erp":
        return "erp"
    if normalized.endswith("/auditflow-finance") or normalized == "auditflow-finance":
        return "finance"

    return None

def extract_json_payload(content: str) -> str:
    """
    Remove Band mention tokens from a system-agent reply.

    System agents reply like:
        @AuditFlow Router { ...json... }

    In raw msg.content, the mention may appear as:
        @[[agent_id]] { ...json... }

    For Reconciliation, we only want the JSON payload.
    """
    start = content.find("{")
    end = content.rfind("}")

    if start != -1 and end != -1 and end > start:
        return content[start : end + 1].strip()

    return content.strip()

def normalize_requested_systems(systems: list[str]) -> list[str]:
    """
    Normalize LLM-provided system names into canonical names:
    crm / erp / finance.

    The tool prompt asks the LLM to pass ["crm", "erp", "finance"], but in
    practice it may pass values like "auditflow-erp" or "AuditFlow Finance".
    """
    normalized: list[str] = []
    saw_reconciliation = False

    for raw_system in systems:
        raw = str(raw_system).strip().lower()
        raw = raw.replace("@", "")
        raw = raw.replace("_", "-")
        raw = raw.replace(" ", "-")

        if "reconciliation" in raw or "rootcause" in raw:
            saw_reconciliation = True
            continue

        if "crm" in raw:
            canonical = "crm"
        elif "erp" in raw:
            canonical = "erp"
        elif "finance" in raw:
            canonical = "finance"
        else:
            canonical = None

        if canonical and canonical not in normalized:
            normalized.append(canonical)

    # If the LLM wrongly includes Reconciliation as a system, treat that as
    # a full reconciliation request and query all three source systems.
    if saw_reconciliation:
        return ["crm", "erp", "finance"]

    preferred_order = ["crm", "erp", "finance"]
    return [system for system in preferred_order if system in normalized]

def _looks_like_router_status_message(content: str) -> bool:
    """
    Detect progress/status messages that Router should never send to the user.
    Router may ask clarification questions, but it must not report workflow status.
    """
    text = content.lower()

    blocked_phrases = [
        "already been initiated",
        "already initiated",
        "currently awaiting",
        "awaiting responses",
        "waiting for responses",
        "process is still ongoing",
        "process is currently active",
        "the process has been initiated",
        "i've initiated",
        "i have initiated",
        "query has been initiated",
        "queries have been sent",
        "i've sent queries",
        "i have sent queries",
        "relevant systems",
        "finance, crm, and erp",
        "crm, erp, and finance",
    ]

    return any(phrase in text for phrase in blocked_phrases)

def systems_from_fields(fields_mentioned: list[str]) -> list[str]:
    """
    Map business field names to the systems that own them.
    Returns canonical system names (crm/erp/finance) in preferred order.
    """
    FIELD_TO_SYSTEM: dict[str, str] = {
        "invoice_amount": "erp",
        "invoice_id": "erp",
        "invoice_date": "erp",
        "due_date": "erp",
        "delivery_status": "erp",
        "installment_number": "erp",
        "payment_amount": "finance",
        "payment_received": "finance",
        "payment_date": "finance",
        "bank_fee": "finance",
        "tax_deduction": "finance",
        "refund_amount": "finance",
        "exchange_rate": "finance",
        "overdue_days": "finance",
        "contract_amount": "crm",
        "payment_terms": "crm",
        "sign_date": "crm",
        "sales_owner": "crm",
        "status": "crm",
    }
    found: list[str] = []
    for field in fields_mentioned:
        key = field.strip().lower().replace(" ", "_")
        system = FIELD_TO_SYSTEM.get(key)
        if system and system not in found:
            found.append(system)
    preferred_order = ["crm", "erp", "finance"]
    return [s for s in preferred_order if s in found]

async def query_systems(
    ctx: RunContext[AgentToolsProtocol],
    entity: str,
    time_scope: str,
    is_reconciliation: bool,
    fields_mentioned: list[str] = [],
) -> str:
    """
    Query AuditFlow system agents for data about an entity.

    Args:
        entity: Company/customer name, exactly as the user wrote it.
        time_scope: Time period, exactly as the user wrote it.
        is_reconciliation: True if the user wants a full reconciliation, anomaly
            check, or cross-system comparison (anything needing multiple systems
            compared). False if the user asks about a single specific fact.
        fields_mentioned: The specific business field names the user asked about,
            e.g. ["invoice_amount"] or ["contract_amount"]. Only used when
            is_reconciliation is False. Use exact field names like invoice_amount,
            contract_amount, payment_amount, due_date, etc.
    """
    key = f"{entity}_{time_scope}"

    if is_reconciliation:
        normalized_systems = ["crm", "erp", "finance"]
        logger.info("Reconciliation request -> querying all three systems.")
    else:
        normalized_systems = systems_from_fields(fields_mentioned)
        logger.info(
            f"Fact lookup: fields={fields_mentioned!r} -> systems={normalized_systems!r}"
        )
        if not normalized_systems:
            logger.info("No systems could be determined from fields; clarification needed.")
            return (
                "NEEDS_CLARIFICATION: Could not determine which systems to query. "
                "Call ask_user_clarification to ask the user whether they want "
                "contract details (CRM), invoice details (ERP), or payment details (Finance)."
            )

    async with pending_queries_lock:
        cleanup_stale_pending_queries_locked()

        existing = pending_queries.get(key)

        if existing is None:

            pending_queries[key] = {
                "expected_systems": set(normalized_systems),
                "received": {},
                "entity": entity,
                "time_scope": time_scope,
                "created_at": time.monotonic(),
            }
            systems_to_send = normalized_systems
            logger.info(
                f"Created pending query {key!r} with expected systems: "
                f"{sorted(pending_queries[key]['expected_systems'])}"
            )
        else:
            expected_systems = existing["expected_systems"]
            before = set(expected_systems)

            expected_systems.update(normalized_systems)

            systems_to_send = [
                system
                for system in normalized_systems
                if system not in before
            ]

            logger.info(
                f"Merged pending query {key!r}: before={sorted(before)}, "
                f"after={sorted(expected_systems)}, new={systems_to_send}"
            )

    mentions_to_send = [SYSTEM_AGENT_HANDLES[system] for system in systems_to_send]

    if not mentions_to_send:
    
        logger.info(
            f"Duplicate query_systems call ignored for {entity}, {time_scope}; "
            "all requested systems are already pending."
        )
        return (
            "INTERNAL_STATUS: duplicate query ignored. "
            "Do not send any message to the user. Stop now."
        )

    await ctx.deps.get_participants()
    await ctx.deps.send_message(
        content=(
            "AuditFlow system query\n"
            f"Entity: {entity}\n"
            f"Time scope: {time_scope}\n"
            f"Systems: {', '.join(normalized_systems)}"
        ),
        mentions=mentions_to_send,
    )

    return f"Query sent to: {mentions_to_send}"

async def ask_user_clarification(
    ctx: RunContext[AgentToolsProtocol],
    content: str,
) -> str:
    """
    Ask the user one clarifying question when the request is too vague.

    Use this only when the Router genuinely cannot determine which systems
    to query. Do not use it for progress updates, status messages, or
    "already in progress" messages.
    """
    if _looks_like_router_status_message(content):
        logger.warning(
            f"Blocked Router status/progress message attempted via "
            f"ask_user_clarification: {content!r}"
        )
        return (
            "INTERNAL_STATUS: blocked. Do not send progress or status messages. "
            "For clear reconciliation requests, call query_systems instead."
        )

    reply_target = getattr(ctx.deps, "reply_target", None)
    if not reply_target:
        await ctx.deps.get_participants()
        user_mentions = [
            p["name"] for p in ctx.deps.participants
            if p.get("type") == "User"
        ]
    else:
        user_mentions = [reply_target]

    if not user_mentions:
        return "Error: no user found for clarification."

    await ctx.deps.send_message(content=content, mentions=user_mentions)
    return f"Clarification sent to user(s): {user_mentions}"

class CountingAdapter(PydanticAIAdapter):
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
        logger.info(f"on_message from: {msg.sender_name!r}")
        sender_system = system_from_sender(msg.sender_name)

        if sender_system is not None:
            active_key = None
            active_entry = None

            async with pending_queries_lock:

                cleanup_stale_pending_queries_locked()

                for key, entry in pending_queries.items():
                    expected_systems = entry["expected_systems"]
                    received = entry["received"]

                    if (
                        sender_system in expected_systems
                        and sender_system not in received
                    ):
                        active_key = key
                        active_entry = entry
                        break

                if active_entry is None:
                    logger.warning(
                        f"Received system-agent reply from {msg.sender_name!r} "
                        f"as {sender_system!r}, but no matching pending query is active"
                    )
                    return

                active_entry["received"][sender_system] = extract_json_payload(msg.content)

                expected_systems = active_entry["expected_systems"]
                received = active_entry["received"]

                logger.info(
                    f"Received reply from {msg.sender_name} as {sender_system} "
                    f"({len(received)}/{len(expected_systems)}), "
                    f"expected={sorted(expected_systems)}"
                )

                is_complete = set(received.keys()) >= set(expected_systems)

            if is_complete:
                await asyncio.sleep(1.0)

                async with pending_queries_lock:
                    latest_entry = pending_queries.get(active_key)

                    if latest_entry is None:
                        return

                    expected_systems = latest_entry["expected_systems"]
                    received = latest_entry["received"]

                    if not (set(received.keys()) >= set(expected_systems)):
                        logger.info(
                            f"Pending query {active_key!r} is no longer complete after merge: "
                            f"{len(received)}/{len(expected_systems)}"
                        )
                        return

                    received_systems = list(received.keys())
                    if len(received_systems) == 1:
                        prefix = (
                            f"Single-system data lookup result for "
                            f"{latest_entry['entity']}, {latest_entry['time_scope']}. "
                            f"Do NOT reconcile. Summarize this data in plain English "
                            f"and reply to the user directly using reply_to_user."
                        )
                    else:
                        prefix = (
                            f"Reconcile the following data for "
                            f"{latest_entry['entity']}, {latest_entry['time_scope']}:"
                        )

                    forward_content = (
                        prefix + "\n\n"
                        + "\n\n".join(
                            f"[{SYSTEM_AGENT_DISPLAY_NAMES.get(system, system)}]:\n{content}"
                            for system, content in received.items()
                        )
                    )

                    del pending_queries[active_key]

                await tools.get_participants()
                await tools.send_message(
                    content=forward_content,
                    mentions=[RECONCILIATION_HANDLE],
                )
                logger.info(
                    f"Forwarded to Reconciliation for {latest_entry['entity']}, "
                    f"{latest_entry['time_scope']}"
                )

            return
        if msg.sender_type == "User":
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
        additional_tools=[query_systems, ask_user_clarification],
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
