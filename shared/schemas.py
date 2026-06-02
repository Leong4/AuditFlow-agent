"""
AuditFlow - Agent 输入/输出契约
================================
这个文件是所有 agent 之间的"格式合同"。
- 只有阿恆能修改这个文件
- 队友直接 import 使用，不要自己定义格式
- 有任何改动需求请在群里说，阿恆统一修改

使用方式:
    from shared.schemas import RouterOutput, CRMOutput, ReconciliationOutput, RootCauseOutput
"""

from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


# ─────────────────────────────────────────────
# 共用枚举
# ─────────────────────────────────────────────

class QueryType(str, Enum):
    FACT_LOOKUP     = "fact_lookup"      # 单系统事实查询
    RECONCILIATION  = "reconciliation"   # 跨系统对账
    ANOMALY_CHECK   = "anomaly_check"    # 异常归因分析


class AnomalyStatus(str, Enum):
    NORMAL  = "normal"   # 差异可被业务规则完全解释
    ANOMALY = "anomaly"  # 差异无法被任何已知规则解释，需人工介入
    WATCH   = "watch"    # 差异可能合理，需后续跟进确认


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
    Router Agent 的输出。
    后续所有 System Agent 的输入都基于这个结构。
    """
    query_type: QueryType               # 查询类型
    entity: str                         # 客户/公司名称，例如 "Acme Corp"
    fields_to_compare: list[str]        # 需要对比的字段，例如 ["contract_amount", "invoice_amount"]
    time_scope: str                     # 时间范围，例如 "Q1 2026"
    systems_needed: list[str]           # 需要查询的系统，例如 ["crm", "erp", "finance"]
    raw_query: str = ""                 # 用户原始问题，保留用于 trace


# ─────────────────────────────────────────────
# Layer 2: System Agents（CRM / ERP / Finance）
# ─────────────────────────────────────────────

@dataclass
class EntityMatch:
    """
    实体匹配结果——记录查询名称和实际匹配名称的差异。
    用于解决不同系统中客户名称格式不一致的问题。
    """
    query: str                  # Router 传入的查询名称，例如 "Acme Corp"
    matched_as: str             # 数据库里实际的名称，例如 "Acme Corporation"
    match_method: MatchMethod   # 匹配方式
    confidence: float           # 匹配置信度，0.0 ~ 1.0


@dataclass
class CRMOutput:
    """
    CRM Agent 的输出。
    只报告合同事实和 CRM 系统的业务规则，不做跨系统判断。
    """
    system: str = "crm"
    entity: str = ""
    entity_match: Optional[EntityMatch] = None

    # 合同数据
    contract_amount: Optional[float] = None
    currency: str = "GBP"
    sign_date: str = ""             # ISO 格式，例如 "2026-01-15"
    status: str = ""                # 合同状态，例如 "active"
    sales_owner: str = ""

    # CRM 系统业务规则（这些规则跟数据一起走，不单独抽离）
    payment_terms: str = ""             # 例如 "3 installments: 40%, 40%, 20%"
    exchange_rate_policy: str = ""      # 例如 "rate at sign date"
    late_payment_grace_period: str = "" # 例如 "15 days"

    data_freshness: str = ""        # 数据截止日期，例如 "2026-03-31"
    error: Optional[str] = None     # 查询失败时填写错误信息


@dataclass
class ERPOutput:
    """
    ERP Agent 的输出。
    只报告发票事实和 ERP 系统的业务规则。
    """
    system: str = "erp"
    entity: str = ""
    entity_match: Optional[EntityMatch] = None

    # 发票数据
    invoice_id: str = ""
    invoice_amount: Optional[float] = None
    currency: str = "GBP"
    invoice_date: str = ""          # ISO 格式
    due_date: str = ""              # 应付日期
    delivery_status: str = ""       # 交付状态，例如 "delivered"
    installment_number: Optional[int] = None  # 当前是第几期

    # ERP 系统业务规则
    invoice_rules: str = ""         # 例如 "net 30 days from invoice_date"

    data_freshness: str = ""
    error: Optional[str] = None


@dataclass
class FinanceOutput:
    """
    Finance Agent 的输出。
    只报告回款事实和 Finance 系统的业务规则。
    """
    system: str = "finance"
    entity: str = ""
    entity_match: Optional[EntityMatch] = None

    # 回款数据
    payment_id: str = ""
    payment_amount: Optional[float] = None
    currency: str = "GBP"
    payment_date: str = ""          # ISO 格式
    payment_method: str = ""        # 例如 "bank_transfer"
    exchange_rate: Optional[float] = None
    refund_amount: float = 0.0
    tax_deduction: float = 0.0
    overdue_days: int = 0

    # Finance 系统业务规则
    exchange_rate_policy: str = ""  # 例如 "rate at sign date"

    data_freshness: str = ""
    error: Optional[str] = None


# ─────────────────────────────────────────────
# Layer 3: Reconciliation Agent
# ─────────────────────────────────────────────

@dataclass
class Discrepancy:
    """
    单条差异记录。
    只描述"哪里不一样、差多少、谁高谁低"，不解释原因。
    """
    field_pair: str             # 例如 "contract_amount vs invoice_amount"
    values: dict                # 例如 {"crm": 120000, "erp": 96000}
    difference: float           # 差值（绝对值）
    direction: str              # 例如 "erp_lower" 或 "finance_lower"


@dataclass
class MatchedField:
    """
    各系统间一致的字段记录。
    """
    field: str
    value: object
    consistent: bool
    note: str = ""


@dataclass
class EntityConsistency:
    """
    跨系统实体名称一致性结果。
    """
    crm: str = ""
    erp: str = ""
    finance: str = ""
    aligned_name: str = ""
    alignment_method: str = ""  # 例如 "fuzzy match + manual alias table"


@dataclass
class ReconciliationOutput:
    """
    Reconciliation Agent 的输出。
    职责边界：只发现差异，不解释差异。
    Root-Cause Agent 才负责解释。
    """
    entity: str = ""
    entity_consistency: Optional[EntityConsistency] = None
    discrepancies: list[Discrepancy] = field(default_factory=list)
    matched: list[MatchedField] = field(default_factory=list)
    error: Optional[str] = None


# ─────────────────────────────────────────────
# Layer 4: Root-Cause Agent
# ─────────────────────────────────────────────

@dataclass
class AnomalyAnalysis:
    """
    单条差异的归因分析结果。
    """
    field_pair: str                     # 对应 Discrepancy.field_pair
    probable_cause: str                 # 主要原因，例如 "installment_schedule"
    confidence: float                   # 置信度，0.0 ~ 1.0
    evidence: list[str] = field(default_factory=list)       # 支撑证据列表
    alternative_causes: list[dict] = field(default_factory=list)  # 备选原因

    status: AnomalyStatus = AnomalyStatus.WATCH
    risk_level: RiskLevel = RiskLevel.MEDIUM
    requires_human: bool = False
    recommended_action: str = ""


@dataclass
class ReconciliationSummary:
    """
    整体对账摘要统计。
    """
    total_discrepancies: int = 0
    normal: int = 0
    anomaly: int = 0
    watch: int = 0


@dataclass
class RootCauseOutput:
    """
    Root-Cause Agent 的输出。
    职责：解释差异 + 输出证据链 + 风险判定 + 行动建议。
    """
    entity: str = ""
    anomalies: list[AnomalyAnalysis] = field(default_factory=list)
    summary: Optional[ReconciliationSummary] = None
    trace_id: str = ""              # 对应 trace.py 里的 trace_id
    error: Optional[str] = None