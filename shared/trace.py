"""
AuditFlow - Decision Trace (auditable decision chain)
=====================================================
Records every decision made by each Agent throughout the reconciliation flow.

This is a key differentiator for AuditFlow:
- A generic AI returns a conclusion without showing how it got there.
- AuditFlow records each reasoning step, so failures can be traced to the
  exact Agent and step that caused the issue.

Usage:
    from shared.trace import AuditTrace, TraceStep, new_trace, add_step, finish_trace

Typical flow:
    trace = new_trace(entity="Acme Corp", raw_query="Why do the contract and payment not match?")
    add_step(trace, TraceStep(agent="router", ...))
    add_step(trace, TraceStep(agent="crm_agent", ...))
    ...
    finish_trace(trace)
    print(trace.to_dict())  # Output the full JSON
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
import uuid


# ─────────────────────────────────────────────
# Single Trace Step
# ─────────────────────────────────────────────

@dataclass
class TraceStep:
    """
    A single decision record from one Agent.
    Each Agent appends one step after it finishes.
    """
    agent: str          # Agent name, e.g. "router", "crm_agent", "reconciliation", "root_cause"
    layer: str          # Layer, e.g. "routing", "data", "analysis", "diagnosis"
    decision: str       # One-sentence description of the decision made in this step
    reason: str = ""    # Rationale for the decision

    # Optional supplemental fields filled by different agents.
    confidence: Optional[float] = None         # Confidence score, 0.0 to 1.0
    data_freshness: Optional[str] = None       # Data cutoff date, filled by System Agents
    rules_reported: list[str] = field(default_factory=list)    # Business rules reported by System Agents
    discrepancies_found: int = 0               # Number of discrepancies found by Reconciliation Agent
    anomaly_status: Optional[str] = None       # Judgment produced by Root-Cause Agent
    error: Optional[str] = None                # Error details if this step failed

    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None and v != [] and v != 0}


# ─────────────────────────────────────────────
# Complete Trace
# ─────────────────────────────────────────────

@dataclass
class AuditTrace:
    """
    Decision chain for a complete reconciliation request.
    Includes every step from the user's question to the final conclusion.
    """
    trace_id: str                               # Unique ID, generated automatically
    entity: str                                 # Queried customer/company name
    raw_query: str                              # Original user question

    steps: list[TraceStep] = field(default_factory=list)

    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: Optional[str] = None
    total_duration_ms: Optional[int] = None     # Total elapsed time in milliseconds
    status: str = "in_progress"                 # "in_progress" | "completed" | "failed"

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "entity": self.entity,
            "raw_query": self.raw_query,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "total_duration_ms": self.total_duration_ms,
            "steps": [step.to_dict() for step in self.steps],
        }


# ─────────────────────────────────────────────
# Helper functions for direct use by teammates
# ─────────────────────────────────────────────

def new_trace(entity: str, raw_query: str) -> AuditTrace:
    """
    Start a new reconciliation trace.
    Called when the Router Agent receives a user question.

    Example:
        trace = new_trace(entity="Acme Corp", raw_query="Why do the contract and payment not match?")
    """
    return AuditTrace(
        trace_id=f"audit_{uuid.uuid4().hex[:8]}",
        entity=entity,
        raw_query=raw_query,
    )


def add_step(trace: AuditTrace, step: TraceStep) -> None:
    """
    Add one step to the trace.
    Called once after each Agent finishes.

    Example:
        add_step(trace, TraceStep(
            agent="crm_agent",
            layer="data",
            decision="Return contract data and payment terms",
            data_freshness="2026-03-31",
            rules_reported=["payment_terms: 3 installments 40%+40%+20%"],
        ))
    """
    trace.steps.append(step)


def finish_trace(trace: AuditTrace) -> None:
    """
    Mark the trace as complete and calculate total elapsed time.
    Called after the Root-Cause Agent outputs the final conclusion.

    Example:
        finish_trace(trace)
        print(trace.to_dict())
    """
    finished = datetime.now(timezone.utc)
    trace.finished_at = finished.isoformat()
    trace.status = "completed"

    started = datetime.fromisoformat(trace.started_at)
    delta_ms = int((finished - started).total_seconds() * 1000)
    trace.total_duration_ms = delta_ms


def fail_trace(trace: AuditTrace, reason: str) -> None:
    """
    Mark the trace as failed.
    Called when any Agent encounters an unrecoverable error.

    Example:
        fail_trace(trace, reason="CRM Agent query timed out")
    """
    trace.finished_at = datetime.now(timezone.utc).isoformat()
    trace.status = "failed"
    add_step(trace, TraceStep(
        agent="system",
        layer="error",
        decision="流程中止",
        reason=reason,
        error=reason,
    ))
