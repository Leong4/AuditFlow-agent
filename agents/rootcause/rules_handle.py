"""
Rule-based Root-Cause Agent for AuditFlow.

This version does NOT call an LLM.
It only handles discrepancies that can be explained by deterministic business rules.

Input:
    ReconciliationOutput

Output:
    RootCauseOutput
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

from shared.schemas import (
    ReconciliationOutput,
    RootCauseOutput,
    AnomalyAnalysis,
    ReconciliationSummary,
    AnomalyStatus,
    RiskLevel,
    Discrepancy,
)

from typing import Callable, Optional
from typing import Optional
from agents.rootcause.llm_client import RootCauseLLMClient
from agents.rootcause.llm_enhancer import maybe_enhance_with_llm

LLMCall = Callable[[str], str]

AMOUNT_TOLERANCE = 0.01


# ---------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------

def run_root_cause_agent(
    reconciliation: ReconciliationOutput,
    trace_id: str = "",
    llm_client: Optional[RootCauseLLMClient] = None,
) -> RootCauseOutput:
    """
    Run Root-Cause Agent.

    Rule-based analysis always runs.
    LLM enhancement is optional and only runs when needed.
    """

    if reconciliation.error:
        return RootCauseOutput(
            entity=reconciliation.entity,
            anomalies=[],
            summary=ReconciliationSummary(),
            trace_id=trace_id,
            error=reconciliation.error,
        )

    analyses: list[AnomalyAnalysis] = []

    for discrepancy in reconciliation.discrepancies:
        rule_analysis = analyze_discrepancy(discrepancy)

        final_analysis = maybe_enhance_with_llm(
            rule_analysis=rule_analysis,
            discrepancy=discrepancy,
            reconciliation=reconciliation,
            llm_client=llm_client,
            trace_id=trace_id,
        )

        analyses.append(final_analysis)

    summary = build_summary(analyses)

    return RootCauseOutput(
        entity=reconciliation.entity,
        anomalies=analyses,
        summary=summary,
        trace_id=trace_id,
        error=None,
    )


# ---------------------------------------------------------------------
# Core dispatcher
# ---------------------------------------------------------------------

def analyze_discrepancy(discrepancy: Discrepancy) -> AnomalyAnalysis:
    """
    Dispatch one discrepancy to a rule-based handler.
    """

    field_pair = (discrepancy.field_pair or "").lower()
    values = discrepancy.values or {}

    if is_missing_required_field(discrepancy):
        return analyze_missing_required_field(discrepancy)

    if is_id_mismatch(field_pair):
        return analyze_id_mismatch(discrepancy)

    if is_date_issue(field_pair, values):
        return analyze_date_issue(discrepancy)

    if is_entity_issue(field_pair, values):
        return analyze_entity_issue(discrepancy)

    if is_amount_issue(field_pair, values):
        return analyze_amount_issue(discrepancy)

    if is_fx_issue(field_pair, values):
        return analyze_fx_issue(discrepancy)

    return make_watch_analysis(
        discrepancy=discrepancy,
        probable_cause="unknown_discrepancy",
        evidence=[
            f"Field pair: {discrepancy.field_pair}",
            f"Values: {values}",
            "No deterministic rule matched this discrepancy.",
        ],
        recommended_action=(
            "Review the discrepancy manually or send it to the LLM explanation layer."
        ),
    )


# ---------------------------------------------------------------------
# Rule category checks 以下审计异常情况由代码直接判断
# ---------------------------------------------------------------------

# 判断id是否无法匹配
def is_id_mismatch(field_pair: str) -> bool:
    return any(
        key in field_pair
        for key in ["customer_id", "contract_id", "invoice_id", "payment_id"]
    )

# 判断是否缺失字段，缺失直接判断异常。
def is_missing_required_field(discrepancy: Discrepancy) -> bool:
    field_pair = (discrepancy.field_pair or "").lower()

    if "missing" in field_pair or "required" in field_pair:
        return True

    for key, value in (discrepancy.values or {}).items():
        if value is None:
            return True
        if isinstance(value, str) and value.strip() == "":
            return True

    return False

# 判断是否有日期问题：逾期直接判定异常，否则返回发票时间、支付时间和预期时间
def is_date_issue(field_pair: str, values: dict[str, Any]) -> bool:
    if "date" in field_pair or "overdue" in field_pair:
        return True

    return has_any_key(values, ["invoice_date", "payment_date", "due_date"])

# 判断公司名称问题，如果提供了实体差异，直接判定异常，否则返回公司名，匹配规则和置信度
def is_entity_issue(field_pair: str, values: dict[str, Any]) -> bool:
    if "entity" in field_pair or "name" in field_pair:
        return True

    return has_any_key(values, ["entity", "match_method", "confidence"])

# 判断金额问题，如果不同系统中存在差异，直接判定异常，否则返回合同金额、支票金额、支付金额、税额减免、银行手续费、退款金额
def is_amount_issue(field_pair: str, values: dict[str, Any]) -> bool:
    if "amount" in field_pair or "payment" in field_pair or "invoice" in field_pair:
        return True

    return has_any_key(
        values,
        [
            "contract_amount",
            "invoice_amount",
            "payment_amount",
            "tax_deduction",
            "bank_fee",
            "refund_amount",
        ],
    )

# 判断汇率问题，存在汇率差异直接判定异常，否则返回汇率、原始货币金额、汇率日期和汇率政策。
def is_fx_issue(field_pair: str, values: dict[str, Any]) -> bool:
    if "fx" in field_pair or "exchange" in field_pair or "currency" in field_pair:
        return True

    return has_any_key(
        values,
        [
            "exchange_rate",
            "original_currency_amount",
            "exchange_rate_date",
            "exchange_rate_policy",
        ],
    )


# ---------------------------------------------------------------------
# Handlers 分析原因
# ---------------------------------------------------------------------

# 分析id异常原因：
def analyze_id_mismatch(discrepancy: Discrepancy) -> AnomalyAnalysis:
    field_pair = (discrepancy.field_pair or "").lower()
    values = discrepancy.values or {}

    # 金额和支票是否对应同一客户
    if "customer_id" in field_pair:
        cause = "customer_id_mismatch"
        action = "Check whether the invoice or payment was linked to the wrong customer."
    # 支票和支付记录是否对应同一合同
    elif "contract_id" in field_pair:
        cause = "contract_id_mismatch"
        action = "Check whether the ERP invoice or Finance payment was linked to the wrong contract."
    # 支付记录是否对应错误的支票
    elif "invoice_id" in field_pair:
        cause = "invoice_id_mismatch"
        action = "Check whether the Finance payment was matched to the wrong ERP invoice."
    # 检查付款记录是否重复或引用错误
    elif "payment_id" in field_pair:
        cause = "payment_id_mismatch"
        action = "Check whether the payment record was duplicated or incorrectly referenced."
    # 要求检查各系统中不一致的标识符
    else:
        cause = "id_mismatch"
        action = "Check the inconsistent identifier across systems."

    evidence = [
        f"Field pair: {discrepancy.field_pair}",
        f"Values: {values}",
        "Identifier mismatches are not explainable by normal payment business rules.",
    ]

    return AnomalyAnalysis(
        field_pair=discrepancy.field_pair,
        probable_cause=cause,
        confidence=0.95,
        evidence=evidence,
        alternative_causes=[
            {"cause": "wrong_record_linkage", "confidence": 0.75},
            {"cause": "manual_data_entry_error", "confidence": 0.60},
        ],
        status=AnomalyStatus.ANOMALY,
        risk_level=RiskLevel.HIGH,
        requires_human=True,
        recommended_action=action,
    )

# 分析缺失字段
def analyze_missing_required_field(discrepancy: Discrepancy) -> AnomalyAnalysis:
    values = discrepancy.values or {}

    missing_fields = []
    for key, value in values.items():
        if value is None or (isinstance(value, str) and value.strip() == ""):
            missing_fields.append(key)

    evidence = [
        f"Field pair: {discrepancy.field_pair}",
        f"Values: {values}",
    ]

    if missing_fields:
        evidence.append(f"Missing or empty fields: {', '.join(missing_fields)}")
    else:
        evidence.append("The discrepancy indicates a required field is missing.")

    return AnomalyAnalysis(
        field_pair=discrepancy.field_pair,
        probable_cause="missing_required_field",
        confidence=0.95,
        evidence=evidence,
        alternative_causes=[
            {"cause": "source_system_incomplete_record", "confidence": 0.75},
            {"cause": "data_extraction_error", "confidence": 0.55},
        ],
        status=AnomalyStatus.ANOMALY,
        risk_level=RiskLevel.HIGH,
        requires_human=True,
        recommended_action=(
            "Complete the missing required field in the source system and rerun reconciliation."
        ),
    )

# 分析日期问题
def analyze_date_issue(discrepancy: Discrepancy) -> AnomalyAnalysis:
    values = discrepancy.values or {}

    invoice_date = parse_date(get_value(values, "erp.invoice_date", "invoice_date"))
    payment_date = parse_date(get_value(values, "finance.payment_date", "payment_date"))
    due_date = parse_date(get_value(values, "erp.due_date", "due_date"))

    overdue_days = to_int(get_value(values, "finance.overdue_days", "overdue_days"))
    grace_period_text = get_value(
        values,
        "crm.late_payment_grace_period",
        "late_payment_grace_period",
    )
    grace_days = parse_days(grace_period_text)

    evidence = [
        f"Field pair: {discrepancy.field_pair}",
        f"Values: {values}",
    ]
    # 财务付款日期早于ERP发票日期
    if invoice_date and payment_date and payment_date < invoice_date:
        evidence.append("Finance payment_date is earlier than ERP invoice_date.")

        return AnomalyAnalysis(
            field_pair=discrepancy.field_pair,
            probable_cause="payment_before_invoice",
            confidence=0.98,
            evidence=evidence,
            alternative_causes=[
                {"cause": "wrong_invoice_linkage", "confidence": 0.70},
                {"cause": "incorrect_payment_date_entry", "confidence": 0.65},
            ],
            status=AnomalyStatus.ANOMALY,
            risk_level=RiskLevel.HIGH,
            requires_human=True,
            recommended_action=(
                "Verify whether the payment was linked to the correct invoice and check the recorded payment date."
            ),
        )
    # 支付逾期
    if due_date and payment_date and payment_date > due_date:
        if overdue_days is None:
            overdue_days = (payment_date - due_date).days

        evidence.append(f"Payment is {overdue_days} days after due_date.")
        # 预期天数在宽限期限
        if grace_days is not None and overdue_days <= grace_days:
            evidence.append(
                f"Overdue days are within the grace period of {grace_days} days."
            )

            return AnomalyAnalysis(
                field_pair=discrepancy.field_pair,
                probable_cause="late_payment_within_grace_period",
                confidence=0.90,
                evidence=evidence,
                alternative_causes=[],
                status=AnomalyStatus.NORMAL,
                risk_level=RiskLevel.LOW,
                requires_human=False,
                recommended_action=(
                    "No immediate action required. Keep the grace period evidence for audit trail."
                ),
            )

        evidence.append("Payment is late and not proven to be within the grace period.")

        return AnomalyAnalysis(
            field_pair=discrepancy.field_pair,
            probable_cause="late_payment_beyond_grace_period",
            confidence=0.88,
            evidence=evidence,
            alternative_causes=[
                {"cause": "client_late_payment", "confidence": 0.70},
                {"cause": "incorrect_due_date", "confidence": 0.45},
            ],
            status=AnomalyStatus.ANOMALY,
            risk_level=RiskLevel.MEDIUM,
            requires_human=True,
            recommended_action=(
                "Check whether late payment follow-up is required and verify the due date rule."
            ),
        )

    return make_watch_analysis(
        discrepancy=discrepancy,
        probable_cause="date_discrepancy_unclear",
        evidence=evidence + ["Date fields are insufficient for deterministic judgment."],
        recommended_action=(
            "Confirm invoice_date, due_date, payment_date, and grace period before final judgment."
        ),
    )

# 分析公司名称问题
def analyze_entity_issue(discrepancy: Discrepancy) -> AnomalyAnalysis:
    values = discrepancy.values or {}

    match_method = str(
        get_value(values, "match_method", "entity_match.match_method", "alignment_method") or ""
    ).lower()

    confidence = to_float(
        get_value(values, "confidence", "entity_match.confidence")
    )

    evidence = [
        f"Field pair: {discrepancy.field_pair}",
        f"Values: {values}",
    ]

    if match_method in {"exact", "alias", "fuzzy"} and confidence is not None:
        evidence.append(f"Entity match method is {match_method}.")
        evidence.append(f"Entity match confidence is {confidence}.")
        # 存在匹配方法，并且置信度大于0.95，无异常。
        if match_method in {"exact", "alias"} and confidence >= 0.95:
            return AnomalyAnalysis(
                field_pair=discrepancy.field_pair,
                probable_cause="entity_alias_accepted",
                confidence=0.95,
                evidence=evidence,
                alternative_causes=[],
                status=AnomalyStatus.NORMAL,
                risk_level=RiskLevel.LOW,
                requires_human=False,
                recommended_action=(
                    "No action required. Keep the alias or exact-match evidence for audit trail."
                ),
            )
        # 无匹配方法，置信度大于0.85，要求用户检查
        if confidence >= 0.85:
            return AnomalyAnalysis(
                field_pair=discrepancy.field_pair,
                probable_cause="possible_entity_alias",
                confidence=0.80,
                evidence=evidence,
                alternative_causes=[
                    {"cause": "valid_company_alias", "confidence": 0.70},
                    {"cause": "possible_entity_mismatch", "confidence": 0.35},
                ],
                status=AnomalyStatus.WATCH,
                risk_level=RiskLevel.MEDIUM,
                requires_human=True,
                recommended_action=(
                    "Confirm the customer alias table or master data mapping."
                ),
            )

    return AnomalyAnalysis(
        field_pair=discrepancy.field_pair,
        probable_cause="possible_entity_mismatch",
        confidence=0.80,
        evidence=evidence + ["Entity match confidence is missing or too low."],
        alternative_causes=[
            {"cause": "valid_unregistered_alias", "confidence": 0.40},
            {"cause": "wrong_customer_selected", "confidence": 0.60},
        ],
        status=AnomalyStatus.ANOMALY,
        risk_level=RiskLevel.HIGH,
        requires_human=True,
        recommended_action=(
            "Manually verify whether the CRM, ERP, and Finance records refer to the same legal entity."
        ),
    )

# 分析金额问题
def analyze_amount_issue(discrepancy: Discrepancy) -> AnomalyAnalysis:
    values = discrepancy.values or {}
    field_pair = (discrepancy.field_pair or "").lower()

    if "contract" in field_pair and "invoice" in field_pair:
        result = try_explain_installment(discrepancy)
        if result:
            return result

    if has_any_key(values, ["exchange_rate", "original_currency_amount"]):
        result = try_explain_fx(discrepancy)
        if result:
            return result

    result = try_explain_payment_adjustment(discrepancy)
    if result:
        return result

    evidence = [
        f"Field pair: {discrepancy.field_pair}",
        f"Values: {values}",
        f"Difference: {discrepancy.difference}",
        "The amount difference cannot be explained by installment, tax deduction, bank fee, refund, or FX conversion.",
    ]

    return AnomalyAnalysis(
        field_pair=discrepancy.field_pair,
        probable_cause="unexplained_amount_mismatch",
        confidence=0.90,
        evidence=evidence,
        alternative_causes=[
            {"cause": "client_underpayment", "confidence": 0.70},
            {"cause": "missing_adjustment_record", "confidence": 0.65},
            {"cause": "manual_amount_entry_error", "confidence": 0.55},
        ],
        status=AnomalyStatus.ANOMALY,
        risk_level=RiskLevel.HIGH,
        requires_human=True,
        recommended_action=(
            "Review the payment record, check for missing adjustments, and confirm whether the client underpaid."
        ),
    )


def analyze_fx_issue(discrepancy: Discrepancy) -> AnomalyAnalysis:
    result = try_explain_fx(discrepancy)
    if result:
        return result

    return make_watch_analysis(
        discrepancy=discrepancy,
        probable_cause="fx_context_incomplete",
        evidence=[
            f"Field pair: {discrepancy.field_pair}",
            f"Values: {discrepancy.values}",
            "FX-related fields are incomplete, so the conversion cannot be checked deterministically.",
        ],
        recommended_action=(
            "Confirm original currency amount, payment currency, exchange rate, and exchange rate date."
        ),
    )


# ---------------------------------------------------------------------
# Specific business rules
# ---------------------------------------------------------------------

def try_explain_installment(discrepancy: Discrepancy) -> Optional[AnomalyAnalysis]:
    values = discrepancy.values or {}

    contract_amount = to_float(
        get_value(values, "crm.contract_amount", "contract_amount")
    )
    invoice_amount = to_float(
        get_value(values, "erp.invoice_amount", "invoice_amount")
    )
    payment_terms = str(
        get_value(values, "crm.payment_terms", "payment_terms") or ""
    )
    installment_number = to_int(
        get_value(values, "erp.installment_number", "installment_number")
    )

    evidence = [
        f"Field pair: {discrepancy.field_pair}",
        f"Values: {values}",
    ]

    if contract_amount is None or invoice_amount is None:
        return None

    percentages = parse_installment_percentages(payment_terms)

    if not percentages or installment_number is None:
        return None

    index = installment_number - 1
    if index < 0 or index >= len(percentages):
        return None

    expected_invoice = contract_amount * percentages[index]

    evidence.append(f"Contract amount = {contract_amount}")
    evidence.append(f"Invoice amount = {invoice_amount}")
    evidence.append(f"Payment terms = {payment_terms}")
    evidence.append(f"Installment number = {installment_number}")
    evidence.append(f"Expected invoice amount = {expected_invoice}")

    if amounts_close(invoice_amount, expected_invoice):
        return AnomalyAnalysis(
            field_pair=discrepancy.field_pair,
            probable_cause="installment_schedule",
            confidence=0.97,
            evidence=evidence + [
                "Invoice amount matches the expected installment amount."
            ],
            alternative_causes=[],
            status=AnomalyStatus.NORMAL,
            risk_level=RiskLevel.LOW,
            requires_human=False,
            recommended_action=(
                "No action required. Keep the installment schedule as audit evidence."
            ),
        )

    return AnomalyAnalysis(
        field_pair=discrepancy.field_pair,
        probable_cause="installment_schedule_mismatch",
        confidence=0.90,
        evidence=evidence + [
            "Invoice amount does not match the expected installment amount."
        ],
        alternative_causes=[
            {"cause": "wrong_installment_percentage", "confidence": 0.65},
            {"cause": "manual_invoice_amount_error", "confidence": 0.60},
        ],
        status=AnomalyStatus.ANOMALY,
        risk_level=RiskLevel.HIGH,
        requires_human=True,
        recommended_action=(
            "Check the agreed installment schedule and verify the ERP invoice amount."
        ),
    )


def try_explain_payment_adjustment(discrepancy: Discrepancy) -> Optional[AnomalyAnalysis]:
    values = discrepancy.values or {}

    invoice_amount = to_float(
        get_value(values, "erp.invoice_amount", "invoice_amount")
    )
    payment_amount = to_float(
        get_value(values, "finance.payment_amount", "payment_amount")
    )

    if invoice_amount is None or payment_amount is None:
        return None

    tax_deduction = to_float(
        get_value(values, "finance.tax_deduction", "tax_deduction")
    ) or 0.0
    bank_fee = to_float(
        get_value(values, "finance.bank_fee", "bank_fee")
    ) or 0.0
    refund_amount = to_float(
        get_value(values, "finance.refund_amount", "refund_amount")
    ) or 0.0

    adjusted_payment = payment_amount + tax_deduction + bank_fee + refund_amount

    evidence = [
        f"ERP invoice_amount = {invoice_amount}",
        f"Finance payment_amount = {payment_amount}",
        f"Finance tax_deduction = {tax_deduction}",
        f"Finance bank_fee = {bank_fee}",
        f"Finance refund_amount = {refund_amount}",
        f"Adjusted payment = {adjusted_payment}",
    ]

    if not amounts_close(adjusted_payment, invoice_amount):
        return None

    if tax_deduction > 0:
        cause = "tax_deduction"
        action = "No action required. Keep tax deduction evidence for audit trail."
    elif bank_fee > 0:
        cause = "bank_fee"
        action = "No action required. Keep bank fee evidence for audit trail."
    elif refund_amount > 0:
        cause = "refund_adjustment"
        action = "Confirm the refund record is properly approved and documented."
    else:
        cause = "amount_reconciled"
        action = "No action required."

    return AnomalyAnalysis(
        field_pair=discrepancy.field_pair,
        probable_cause=cause,
        confidence=0.97,
        evidence=evidence + [
            "Adjusted payment equals ERP invoice amount."
        ],
        alternative_causes=[],
        status=AnomalyStatus.NORMAL,
        risk_level=RiskLevel.LOW,
        requires_human=False,
        recommended_action=action,
    )


def try_explain_fx(discrepancy: Discrepancy) -> Optional[AnomalyAnalysis]:
    values = discrepancy.values or {}

    original_currency_amount = to_float(
        get_value(values, "finance.original_currency_amount", "original_currency_amount")
    )
    exchange_rate = to_float(
        get_value(values, "finance.exchange_rate", "exchange_rate")
    )
    payment_amount = to_float(
        get_value(values, "finance.payment_amount", "payment_amount")
    )

    if (
        original_currency_amount is None
        or exchange_rate is None
        or payment_amount is None
    ):
        return None

    expected_payment = original_currency_amount * exchange_rate

    evidence = [
        f"Original currency amount = {original_currency_amount}",
        f"Exchange rate = {exchange_rate}",
        f"Expected payment amount = {expected_payment}",
        f"Actual payment amount = {payment_amount}",
    ]

    exchange_rate_policy = get_value(
        values,
        "crm.exchange_rate_policy",
        "finance.exchange_rate_policy",
        "exchange_rate_policy",
    )
    exchange_rate_date = get_value(
        values,
        "finance.exchange_rate_date",
        "exchange_rate_date",
    )

    if exchange_rate_policy:
        evidence.append(f"Exchange rate policy = {exchange_rate_policy}")
    if exchange_rate_date:
        evidence.append(f"Exchange rate date = {exchange_rate_date}")

    if amounts_close(expected_payment, payment_amount):
        return AnomalyAnalysis(
            field_pair=discrepancy.field_pair,
            probable_cause="valid_fx_conversion",
            confidence=0.97,
            evidence=evidence + [
                "Payment amount matches the expected converted amount."
            ],
            alternative_causes=[],
            status=AnomalyStatus.NORMAL,
            risk_level=RiskLevel.LOW,
            requires_human=False,
            recommended_action=(
                "No action required. Keep the exchange rate and conversion evidence for audit trail."
            ),
        )

    return AnomalyAnalysis(
        field_pair=discrepancy.field_pair,
        probable_cause="fx_conversion_mismatch",
        confidence=0.95,
        evidence=evidence + [
            "Payment amount does not match the expected converted amount."
        ],
        alternative_causes=[
            {"cause": "wrong_exchange_rate_used", "confidence": 0.70},
            {"cause": "payment_amount_entry_error", "confidence": 0.65},
            {"cause": "missing_adjustment_record", "confidence": 0.50},
        ],
        status=AnomalyStatus.ANOMALY,
        risk_level=RiskLevel.HIGH,
        requires_human=True,
        recommended_action=(
            "Verify the exchange rate, exchange rate date, and recorded payment amount."
        ),
    )


# ---------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------

def build_summary(analyses: list[AnomalyAnalysis]) -> ReconciliationSummary:
    return ReconciliationSummary(
        total_discrepancies=len(analyses),
        normal=sum(1 for item in analyses if item.status == AnomalyStatus.NORMAL),
        anomaly=sum(1 for item in analyses if item.status == AnomalyStatus.ANOMALY),
        watch=sum(1 for item in analyses if item.status == AnomalyStatus.WATCH),
    )


def make_watch_analysis(
    discrepancy: Discrepancy,
    probable_cause: str,
    evidence: list[str],
    recommended_action: str,
) -> AnomalyAnalysis:
    return AnomalyAnalysis(
        field_pair=discrepancy.field_pair,
        probable_cause=probable_cause,
        confidence=0.60,
        evidence=evidence,
        alternative_causes=[
            {"cause": "insufficient_context", "confidence": 0.70}
        ],
        status=AnomalyStatus.WATCH,
        risk_level=RiskLevel.MEDIUM,
        requires_human=True,
        recommended_action=recommended_action,
    )


# ---------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------

def get_value(values: dict[str, Any], *candidate_keys: str) -> Any:
    """
    Flexible value getter.

    Supports:
    - exact key: "erp.invoice_amount"
    - suffix key: "invoice_amount"
    """

    for key in candidate_keys:
        if key in values:
            return values[key]

    for key in candidate_keys:
        for actual_key, actual_value in values.items():
            if actual_key.endswith(key):
                return actual_value

    return None


def has_any_key(values: dict[str, Any], keys: list[str]) -> bool:
    for key in keys:
        if get_value(values, key) is not None:
            return True
    return False


def to_float(value: Any) -> Optional[float]:
    if value is None:
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_int(value: Any) -> Optional[int]:
    if value is None:
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_date(value: Any) -> Optional[datetime]:
    if not value:
        return None

    if isinstance(value, datetime):
        return value

    if not isinstance(value, str):
        return None

    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def parse_days(text: Any) -> Optional[int]:
    """
    Parse strings like:
    - "15 days"
    - "10 days"
    """

    if text is None:
        return None

    match = re.search(r"(\d+)", str(text))
    if not match:
        return None

    return int(match.group(1))


def parse_installment_percentages(payment_terms: str) -> list[float]:
    """
    Parse strings like:
    - "3 installments: 40%, 40%, 20%"

    Returns:
    - [0.4, 0.4, 0.2]
    """

    if not payment_terms:
        return []

    percentages = re.findall(r"(\d+(?:\.\d+)?)\s*%", payment_terms)

    return [float(p) / 100 for p in percentages]


def amounts_close(a: float, b: float, tolerance: float = AMOUNT_TOLERANCE) -> bool:
    return abs(a - b) <= tolerance