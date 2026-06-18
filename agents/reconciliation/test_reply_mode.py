import asyncio

from agents.reconciliation.agent import reply_to_user


class FakeTools:
    def __init__(self):
        self.current_reply_mode = "agent"
        self.participants = [
            {"name": "AuditFlow CRM", "type": "Agent"},
            {"name": "AuditFlow Reconciliation", "type": "Agent"},
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


def test_single_system_reply_mode_agent_mentions_demo_user_without_user_participant():
    tools = FakeTools()
    ctx = FakeContext(tools)

    result = asyncio.run(reply_to_user(ctx, "CRM data for Acme Corp: contract_amount: 120000"))

    assert result == "Sent to user(s): ['AuditFlow Demo User']"
    assert tools.sent_messages == [{
        "content": "CRM data for Acme Corp: contract_amount: 120000",
        "mentions": ["AuditFlow Demo User"],
    }]
