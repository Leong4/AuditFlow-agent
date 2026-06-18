import asyncio

from agents.rootcause.agent import (
    _maybe_send_fallback_reply,
    _replied_message_ids,
    _rootcause_output_by_message_id,
    format_rootcause_reply,
    reply_to_user,
)
from shared.schemas import (
    AnomalyAnalysis,
    AnomalyStatus,
    ReconciliationSummary,
    RiskLevel,
    RootCauseOutput,
)


class FakeTools:
    def __init__(self, participants=None):
        self.participants = participants or [
            {"name": "Test User", "type": "User"},
            {"name": "AuditFlow RootCause", "type": "Agent"},
        ]
        self.sent_messages = []

    async def get_participants(self):
        return self.participants

    async def send_message(self, content, mentions):
        self.sent_messages.append({
            "content": content,
            "mentions": mentions,
        })


class FakeContext:
    def __init__(self, deps):
        self.deps = deps


def reset_rootcause_fallback_state():
    _replied_message_ids.clear()
    _rootcause_output_by_message_id.clear()


def build_anomaly_output(entity="Lakeside Manufacturing"):
    return RootCauseOutput(
        entity=entity,
        anomalies=[
            AnomalyAnalysis(
                field_pair="invoice_amount vs adjusted_payment_amount",
                probable_cause="underpayment",
                confidence=0.92,
                evidence=[
                    "ERP invoice amount is 70000",
                    "Finance adjusted payment is 65000",
                ],
                status=AnomalyStatus.ANOMALY,
                risk_level=RiskLevel.HIGH,
                recommended_action="Investigate the payment shortfall.",
            )
        ],
        summary=ReconciliationSummary(
            total_discrepancies=1,
            normal=0,
            anomaly=1,
            watch=0,
        ),
    )


def test_fallback_fires_when_not_yet_replied():
    reset_rootcause_fallback_state()
    tools = FakeTools()
    output = build_anomaly_output()
    _rootcause_output_by_message_id["msg1"] = output

    sent = asyncio.run(_maybe_send_fallback_reply(tools, "msg1"))

    assert sent is True
    assert len(tools.sent_messages) == 1
    assert tools.sent_messages[0]["content"] == format_rootcause_reply(output)
    assert tools.sent_messages[0]["mentions"] == ["Test User"]
    assert "msg1" in _replied_message_ids


def test_reply_to_user_mode_keeps_existing_user_mentions():
    reset_rootcause_fallback_state()
    tools = FakeTools()
    ctx = FakeContext(tools)

    result = asyncio.run(reply_to_user(ctx, "done", reply_mode="user"))

    assert result == "Sent to user(s): ['Test User']"
    assert tools.sent_messages == [{
        "content": "done",
        "mentions": ["Test User"],
    }]


def test_reply_to_agent_mode_mentions_demo_user_without_user_participant():
    reset_rootcause_fallback_state()
    tools = FakeTools(participants=[
        {"name": "AuditFlow CRM", "type": "Agent"},
        {"name": "AuditFlow RootCause", "type": "Agent"},
    ])
    ctx = FakeContext(tools)

    result = asyncio.run(reply_to_user(ctx, "done", reply_mode="agent"))

    assert result == "Sent to user(s): ['AuditFlow Demo User']"
    assert tools.sent_messages == [{
        "content": "done",
        "mentions": ["AuditFlow Demo User"],
    }]


def test_fallback_does_not_fire_when_already_replied():
    reset_rootcause_fallback_state()
    tools = FakeTools()
    _rootcause_output_by_message_id["msg2"] = build_anomaly_output()
    _replied_message_ids.add("msg2")

    sent = asyncio.run(_maybe_send_fallback_reply(tools, "msg2"))

    assert sent is False
    assert tools.sent_messages == []


def test_fallback_does_not_raise_without_cached_output():
    reset_rootcause_fallback_state()
    tools = FakeTools()

    sent = asyncio.run(_maybe_send_fallback_reply(tools, "msg3"))

    assert sent is False
    assert tools.sent_messages == []


def test_formatter_clean_case_content():
    clean_output = RootCauseOutput(
        entity="Acme Corp",
        anomalies=[],
        summary=ReconciliationSummary(
            total_discrepancies=0,
            normal=0,
            anomaly=0,
            watch=0,
        ),
    )

    text = format_rootcause_reply(clean_output)

    assert "**AuditFlow Root Cause Analysis — Acme Corp**" in text
    assert "Acme Corp" in text
    assert (
        "All fields are consistent across CRM, ERP, and Finance. "
        "No action required."
    ) in text


def test_formatter_anomaly_case_content():
    output = build_anomaly_output(entity="Lakeside Manufacturing")

    text = format_rootcause_reply(output)

    assert "Lakeside Manufacturing" in text
    assert "invoice_amount vs adjusted_payment_amount" in text
    assert "underpayment" in text
    assert "Investigate the payment shortfall." in text
