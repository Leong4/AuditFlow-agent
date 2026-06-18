"""
AuditFlow - Agent input/output contracts
========================================
This file is the shared "format contract" between all agents.
- Only Ah Hang should modify this file.
- Teammates should import and use these definitions directly instead of
  redefining their own formats.
- Raise any requested changes in the group so Ah Hang can update them centrally.

Usage:
    from shared.schemas import RouterOutput, CRMOutput, ReconciliationOutput, RootCauseOutput
"""

from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


# ─────────────────────────────────────────────
# Shared enums
# ─────────────────────────────────────────────

class QueryType(str, Enum):
    FACT_LOOKUP     = "fact_lookup"      # Single-system fact lookup
    RECONCILIATION  = "reconciliation"   # Cross-system reconciliation
    ANOMALY_CHECK   = "anomaly_check"    # Anomaly root-cause analysis


class AnomalyStatus(str, Enum):
    NORMAL  = "normal"   # Difference is fully explained by business rules
    ANOMALY = "anomaly"  # Difference cannot be explained by known rules and needs human review
    WATCH   = "watch"    # Difference may be reasonable but needs follow-up confirmation


class RiskLevel(str, Enum):
    LOW    = "low"
    MEDIUM = "medium"
    HIGH   = "high"


class MatchMethod(str, Enum):
    EXACT  = "exact"
    FUZZY  = "fuzzy"
    ALIAS  = "alias"


# ─────────────────────────────────────────────
# Layer 1: Router Agent
# ─────────────────────────────────────────────

@dataclass
class RouterOutput:
    """
    Output from the Router Agent.
    All downstream System Agent inputs are based on this structure.
    """
    query_type: QueryType               # Query type
    entity: str                         # Customer/company name, e.g. "Acme Corp"
    fields_to_compare: list[str]        # Fields to compare, e.g. ["contract_amount", "invoice_amount"]
    time_scope: str                     # Time range, e.g. "Q1 2026"
    systems_needed: list[str]           # Systems to query, e.g. ["crm", "erp", "finance"]
    raw_query: str = ""                 # Original user question, retained for trace


# ─────────────────────────────────────────────
# Layer 2: System Agents (CRM / ERP / Finance)
# ─────────────────────────────────────────────

@dataclass
class EntityMatch:
    """
    Entity matching result, recording the difference between the queried name
    and the actual matched name.
    Used to handle customer name format differences across systems.
    """
    query: str                  # Query name passed by Router, e.g. "Acme Corp"
    matched_as: str             # Actual name in the database, e.g. "Acme Corporation"
    match_method: MatchMethod   # Matching method
    confidence: float           # Matching confidence, 0.0 to 1.0


@dataclass
class CRMOutput:
    """
    Output from the CRM Agent.
    Reports only contract facts and CRM business rules; does not make
    cross-system judgments.
    """
    system: str = "crm"
    entity: str = ""
    entity_match: Optional[EntityMatch] = None

    # Contract data
    contract_amount: Optional[float] = None
    currency: str = "GBP"
    sign_date: str = ""             # ISO format, e.g. "2026-01-15"
    status: str = ""                # Contract status, e.g. "active"
    sales_owner: str = ""

    # CRM business rules. These rules travel with the data instead of being separated out.
    payment_terms: str = ""             # E.g. "3 installments: 40%, 40%, 20%"
    exchange_rate_policy: str = ""      # E.g. "rate at sign date"
    late_payment_grace_period: str = "" # E.g. "15 days"

    data_freshness: str = ""        # Data cutoff date, e.g. "2026-03-31"
    error: Optional[str] = None     # Error details when a query fails
    customer_id: str = ""
    contract_id: str = ""
    query_id: str = ""
    reply_mode: str = "user"


@dataclass
class ERPOutput:
    """
    Output from the ERP Agent.
    Reports only invoice facts and ERP business rules.
    """
    system: str = "erp"
    entity: str = ""
    entity_match: Optional[EntityMatch] = None

    # Invoice data
    invoice_id: str = ""
    invoice_amount: Optional[float] = None
    currency: str = "GBP"
    invoice_date: str = ""          # ISO format
    due_date: str = ""              # Due date
    delivery_status: str = ""       # Delivery status, e.g. "delivered"
    installment_number: Optional[int] = None  # Current installment number

    # ERP business rules
    invoice_rules: str = ""         # E.g. "net 30 days from invoice_date"

    data_freshness: str = ""
    error: Optional[str] = None
    customer_id: str = ""
    contract_id: str = ""
    query_id: str = ""
    reply_mode: str = "user"


@dataclass
class FinanceOutput:
    """
    Output from the Finance Agent.
    Reports only payment facts and Finance business rules.
    """
    system: str = "finance"
    entity: str = ""
    entity_match: Optional[EntityMatch] = None

    # Payment data
    payment_id: str = ""
    payment_amount: Optional[float] = None
    currency: str = "GBP"
    payment_date: str = ""          # ISO format
    payment_method: str = ""        # E.g. "bank_transfer"
    exchange_rate: Optional[float] = None
    refund_amount: float = 0.0
    tax_deduction: float = 0.0
    overdue_days: int = 0

    # Finance business rules
    exchange_rate_policy: str = ""  # E.g. "rate at sign date"

    data_freshness: str = ""
    error: Optional[str] = None
    customer_id: str = ""
    contract_id: str = ""
    invoice_id: str = ""
    bank_fee: float = 0.0
    original_currency_amount: Optional[float] = None
    exchange_rate_date: str = ""
    query_id: str = ""
    reply_mode: str = "user"


# ─────────────────────────────────────────────
# Layer 3: Reconciliation Agent
# ─────────────────────────────────────────────

@dataclass
class Discrepancy:
    """
    A single discrepancy record.
    Describes only what differs, by how much, and which side is higher or lower;
    it does not explain the reason.
    """
    field_pair: str             # E.g. "contract_amount vs invoice_amount"
    values: dict                # E.g. {"crm": 120000, "erp": 96000}
    difference: float           # Difference as an absolute value
    direction: str              # E.g. "erp_lower" or "finance_lower"


@dataclass
class MatchedField:
    """
    Record of a field that is consistent across systems.
    """
    field: str
    value: object
    consistent: bool
    note: str = ""


@dataclass
class EntityConsistency:
    """
    Cross-system entity name consistency result.
    """
    crm: str = ""
    erp: str = ""
    finance: str = ""
    aligned_name: str = ""
    alignment_method: str = ""  # E.g. "fuzzy match + manual alias table"


@dataclass
class ReconciliationOutput:
    """
    Output from the Reconciliation Agent.
    Responsibility boundary: finds discrepancies only and does not explain them.
    The Root-Cause Agent is responsible for explanations.
    """
    entity: str = ""
    entity_consistency: Optional[EntityConsistency] = None
    discrepancies: list[Discrepancy] = field(default_factory=list)
    matched: list[MatchedField] = field(default_factory=list)
    error: Optional[str] = None
    query_id: str = ""
    reply_mode: str = "user"


# ─────────────────────────────────────────────
# Layer 4: Root-Cause Agent
# ─────────────────────────────────────────────

@dataclass
class AnomalyAnalysis:
    """
    Root-cause analysis result for a single discrepancy.
    """
    field_pair: str                     # Corresponds to Discrepancy.field_pair
    probable_cause: str                 # Primary cause, e.g. "installment_schedule"
    confidence: float                   # Confidence score, 0.0 to 1.0
    evidence: list[str] = field(default_factory=list)       # Supporting evidence list
    alternative_causes: list[dict] = field(default_factory=list)  # Alternative causes

    status: AnomalyStatus = AnomalyStatus.WATCH
    risk_level: RiskLevel = RiskLevel.MEDIUM
    requires_human: bool = False
    recommended_action: str = ""


@dataclass
class ReconciliationSummary:
    """
    Overall reconciliation summary statistics.
    """
    total_discrepancies: int = 0
    normal: int = 0
    anomaly: int = 0
    watch: int = 0


@dataclass
class RootCauseOutput:
    """
    Output from the Root-Cause Agent.
    Responsibilities: explain discrepancies, output evidence chains, assess risk,
    and recommend actions.
    """
    entity: str = ""
    anomalies: list[AnomalyAnalysis] = field(default_factory=list)
    summary: Optional[ReconciliationSummary] = None
    trace_id: str = ""              # Corresponds to trace_id in trace.py
    query_id: str = ""
    error: Optional[str] = None
    reply_mode: str = "user"
