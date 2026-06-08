# Reconciliation Agent 核心逻辑文件
# 职责边界：只发现跨系统字段是否一致或存在差异，不负责解释差异原因。
# 后续再由Root-Cause Agent 做原因分析和风险判断。

from shared.schemas import (
    CRMOutput,
    ERPOutput,
    FinanceOutput,
    ReconciliationOutput,
    Discrepancy,
    MatchedField,
    EntityConsistency,
)

from shared.trace import AuditTrace, TraceStep, add_step

import re
from datetime import date


# 将已经确认一致的字段加入 matched 列表，方便最终输出统一管理。
def _add_matched(matched: list[MatchedField], field: str, value, note: str = "") -> None:
    matched.append(MatchedField(
        field=field,
        value=value,
        consistent=True,
        note=note
    ))


# 将发现的差异加入 discrepancies 列表。
# difference 统一取绝对值，direction 用来说明是哪一边偏高、偏低或不一致。
def _add_discrepancy(
    discrepancies: list[Discrepancy],
    field_pair: str,
    values: dict,
    difference: float,
    direction: str
) -> None:
    discrepancies.append(Discrepancy(
        field_pair=field_pair,
        values=values,
        difference=abs(difference),
        direction=direction
    ))


# 将 ISO 日期字符串转换为 date 对象。
# 如果字段为空或格式不合法，返回 None，避免日期比较时报错。
def _parse_date(value: str) -> date | None:
    if not value:
        return None

    try:
        return date.fromisoformat(value)
    except ValueError:
        return None

# 金额比较时允许极小误差，主要用于 FX 换算后的浮点数比较。
def _amounts_close(left: float, right: float, tolerance: float = 0.01) -> bool:
    return abs(left - right) <= tolerance

# 检查三个系统输出中是否缺少关键字段。
# 如果关键字段缺失，Reconciliation 不应该强行判断为 clean。
def _check_required_fields(
    crm: CRMOutput,
    erp: ERPOutput,
    finance: FinanceOutput,
    discrepancies: list[Discrepancy]
) -> None:
    missing_fields = {}

    crm_required = {
        "entity": crm.entity,
        "customer_id": crm.customer_id,
        "contract_id": crm.contract_id,
        "contract_amount": crm.contract_amount,
        "currency": crm.currency,
        "payment_terms": crm.payment_terms,
    }

    erp_required = {
        "entity": erp.entity,
        "customer_id": erp.customer_id,
        "contract_id": erp.contract_id,
        "invoice_id": erp.invoice_id,
        "invoice_amount": erp.invoice_amount,
        "currency": erp.currency,
        "invoice_date": erp.invoice_date,
        "due_date": erp.due_date,
    }

    finance_required = {
        "entity": finance.entity,
        "customer_id": finance.customer_id,
        "contract_id": finance.contract_id,
        "invoice_id": finance.invoice_id,
        "payment_amount": finance.payment_amount,
        "currency": finance.currency,
        "payment_date": finance.payment_date,
    }

    for system, fields in {
        "crm": crm_required,
        "erp": erp_required,
        "finance": finance_required,
    }.items():
        missing = [
            field
            for field, value in fields.items()
            if value is None or value == ""
        ]

        if missing:
            missing_fields[system] = missing

    if missing_fields:
        _add_discrepancy(
            discrepancies,
            field_pair="required_fields",
            values=missing_fields,
            difference=0.0,
            direction="missing_required_fields"
        )


# 检查 entity_match 的置信度。
# 如果某个系统缺少 entity_match，或匹配置信度过低，就记录为潜在匹配问题。
def _check_entity_match_confidence(
    crm: CRMOutput,
    erp: ERPOutput,
    finance: FinanceOutput,
    discrepancies: list[Discrepancy],
    threshold: float = 0.85
) -> None:
    values = {}

    for system, output in {
        "crm": crm,
        "erp": erp,
        "finance": finance,
    }.items():
        match = output.entity_match

        if match is None:
            values[system] = {
                "entity": output.entity,
                "issue": "missing_entity_match"
            }
        elif match.confidence < threshold:
            values[system] = {
                "entity": output.entity,
                "matched_as": match.matched_as,
                "match_method": match.match_method.value,
                "confidence": match.confidence
            }

    if values:
        _add_discrepancy(
            discrepancies,
            field_pair="entity_match_confidence",
            values=values,
            difference=0.0,
            direction="low_or_missing_entity_match_confidence"
        )


# 检查日期相关信号，例如付款日期是否早于发票日期、是否逾期。
# 这里仍然只记录差异，不解释具体业务原因。
def _check_date_signals(
    erp: ERPOutput,
    finance: FinanceOutput,
    discrepancies: list[Discrepancy]
) -> None:
    invoice_date = _parse_date(erp.invoice_date)
    payment_date = _parse_date(finance.payment_date)

    if invoice_date is not None and payment_date is not None:
        if payment_date < invoice_date:
            _add_discrepancy(
                discrepancies,
                field_pair="invoice_date vs payment_date",
                values={
                    "erp_invoice_date": erp.invoice_date,
                    "finance_payment_date": finance.payment_date
                },
                difference=0.0,
                direction="payment_before_invoice"
            )

    if finance.overdue_days > 0:
        _add_discrepancy(
            discrepancies,
            field_pair="due_date vs payment_date",
            values={
                "erp_due_date": erp.due_date,
                "finance_payment_date": finance.payment_date,
                "overdue_days": finance.overdue_days
            },
            difference=float(finance.overdue_days),
            direction="payment_overdue"
        )

# 判断当前记录是否属于 FX 换算场景。
# 条件：ERP 与 Finance 币种不同，并且 Finance 提供了原币金额和汇率。
def _is_fx_conversion_case(erp: ERPOutput, finance: FinanceOutput) -> bool:
    return (
        erp.currency != finance.currency
        and finance.original_currency_amount is not None
        and finance.exchange_rate is not None
    )


# 处理 FX 换算金额对账。
# 如果是 FX 场景，则用 original_currency_amount 和 exchange_rate 计算应收金额。
# 返回 True 表示 FX 逻辑已经处理完，不再继续普通同币种金额比较。
def _compare_fx_amounts(
    erp: ERPOutput,
    finance: FinanceOutput,
    matched: list[MatchedField],
    discrepancies: list[Discrepancy]
) -> bool:
    """
    Handle FX conversion cases.

    Returns True if this is an FX conversion case and has been handled.
    Returns False if normal same-currency amount comparison should continue.
    """

    if not _is_fx_conversion_case(erp, finance):
        return False

    if finance.payment_amount is None:
        return False

    expected_converted_payment = finance.original_currency_amount * finance.exchange_rate
    adjusted_payment = (
        finance.payment_amount
        + finance.tax_deduction
        + finance.bank_fee
        - finance.refund_amount
    )

    original_amount_matches_invoice = _amounts_close(
        finance.original_currency_amount,
        erp.invoice_amount
    )

    converted_amount_matches_payment = _amounts_close(
        expected_converted_payment,
        adjusted_payment
    )

    if original_amount_matches_invoice and converted_amount_matches_payment:
        _add_matched(
            matched,
            field="fx_converted_payment_amount",
            value=adjusted_payment,
            note="Finance payment matches ERP invoice after FX conversion using the recorded exchange rate."
        )
    else:
        direction = (
            "finance_lower"
            if adjusted_payment < expected_converted_payment
            else "finance_higher"
        )

        _add_discrepancy(
            discrepancies,
            field_pair="fx_converted_amount vs adjusted_payment_amount",
            values={
                "erp_invoice_amount": erp.invoice_amount,
                "erp_currency": erp.currency,
                "finance_currency": finance.currency,
                "original_currency_amount": finance.original_currency_amount,
                "exchange_rate": finance.exchange_rate,
                "exchange_rate_date": finance.exchange_rate_date,
                "expected_converted_payment": expected_converted_payment,
                "finance_payment": finance.payment_amount,
                "tax_deduction": finance.tax_deduction,
                "bank_fee": finance.bank_fee,
                "refund_amount": finance.refund_amount,
                "adjusted_finance": adjusted_payment
            },
            difference=expected_converted_payment - adjusted_payment,
            direction=direction
        )

    return True

# 检查 customer_id 和 contract_id 是否在 CRM、ERP、Finance 三个系统中一致。
# 这比只看公司名更可靠，可以发现客户或合同被错误匹配的情况。
def _check_customer_and_contract_ids(
    crm: CRMOutput,
    erp: ERPOutput,
    finance: FinanceOutput,
    matched: list[MatchedField],
    discrepancies: list[Discrepancy]
) -> None:
    customer_values = {
        "crm": crm.customer_id,
        "erp": erp.customer_id,
        "finance": finance.customer_id,
    }

    if crm.customer_id and erp.customer_id and finance.customer_id:
        if crm.customer_id == erp.customer_id == finance.customer_id:
            _add_matched(
                matched,
                field="customer_id",
                value=crm.customer_id,
                note="Customer ID is consistent across CRM, ERP and Finance."
            )
        else:
            _add_discrepancy(
                discrepancies,
                field_pair="customer_id across systems",
                values=customer_values,
                difference=0.0,
                direction="customer_id_mismatch"
            )

    contract_values = {
        "crm": crm.contract_id,
        "erp": erp.contract_id,
        "finance": finance.contract_id,
    }

    if crm.contract_id and erp.contract_id and finance.contract_id:
        if crm.contract_id == erp.contract_id == finance.contract_id:
            _add_matched(
                matched,
                field="contract_id",
                value=crm.contract_id,
                note="Contract ID is consistent across CRM, ERP and Finance."
            )
        else:
            _add_discrepancy(
                discrepancies,
                field_pair="contract_id across systems",
                values=contract_values,
                difference=0.0,
                direction="contract_id_mismatch"
            )


# 检查 ERP 发票 ID 与 Finance 付款记录中的 invoice_id 是否一致。
# 即使金额一致，如果 invoice_id 不一致，也说明付款可能关联到了错误发票。
def _check_invoice_linking(
    erp: ERPOutput,
    finance: FinanceOutput,
    matched: list[MatchedField],
    discrepancies: list[Discrepancy]
) -> None:
    if erp.invoice_id and finance.invoice_id:
        if erp.invoice_id == finance.invoice_id:
            _add_matched(
                matched,
                field="invoice_id",
                value=erp.invoice_id,
                note="Finance payment is linked to the same ERP invoice ID."
            )
        else:
            _add_discrepancy(
                discrepancies,
                field_pair="erp_invoice_id vs finance_invoice_id",
                values={
                    "erp": erp.invoice_id,
                    "finance": finance.invoice_id
                },
                difference=0.0,
                direction="invoice_id_mismatch"
            )


# 比较三个系统的币种。
# 如果 CRM/ERP 使用原始发票币种，而 Finance 使用换算后的付款币种，则交给 FX 逻辑处理。
def _compare_currency(
    crm: CRMOutput,
    erp: ERPOutput,
    finance: FinanceOutput,
    matched: list[MatchedField],
    discrepancies: list[Discrepancy]
) -> None:
    values = {
        "crm": crm.currency,
        "erp": erp.currency,
        "finance": finance.currency
    }

    if crm.currency == erp.currency == finance.currency:
        _add_matched(
            matched,
            field="currency",
            value=crm.currency,
            note="Currency is consistent across CRM, ERP and Finance."
        )
    elif crm.currency == erp.currency and _is_fx_conversion_case(erp, finance):
        _add_matched(
            matched,
            field="currency_fx_conversion",
            value={
                "source_currency": erp.currency,
                "payment_currency": finance.currency,
                "exchange_rate": finance.exchange_rate,
                "exchange_rate_date": finance.exchange_rate_date
            },
            note="CRM and ERP use the invoice currency, while Finance uses a converted payment currency."
        )
    else:
        _add_discrepancy(
            discrepancies,
            field_pair="currency across systems",
            values=values,
            difference=0.0,
            direction="currency_mismatch"
        )


# 比较合同金额、发票金额和财务回款金额。
# 这里包含分期付款、税款扣除、银行手续费、退款和 FX 换算等基础规则。
def _compare_amounts(
    crm: CRMOutput,
    erp: ERPOutput,
    finance: FinanceOutput,
    matched: list[MatchedField],
    discrepancies: list[Discrepancy]
) -> None:
    """
    Compare amount fields across CRM, ERP and Finance.

    Logic:
    - If the CRM payment_terms include installment percentages and ERP has an installment_number,
      compare ERP invoice_amount with the expected installment amount.
    - Otherwise, compare CRM contract_amount directly with ERP invoice_amount.
    - Then compare ERP invoice_amount with Finance adjusted payment:
      payment_amount + tax_deduction + bank_fee - refund_amount.
    """

    if crm.contract_amount is not None and erp.invoice_amount is not None:
        expected_installment_amount = _expected_installment_amount(
            contract_amount=crm.contract_amount,
            payment_terms=crm.payment_terms,
            installment_number=erp.installment_number
        )

        if expected_installment_amount is not None:
            if expected_installment_amount == erp.invoice_amount:
                _add_matched(
                    matched,
                    field="expected_installment_amount vs invoice_amount",
                    value=erp.invoice_amount,
                    note="ERP invoice amount matches the expected installment amount from CRM payment terms."
                )
            else:
                direction = (
                    "erp_lower"
                    if erp.invoice_amount < expected_installment_amount
                    else "erp_higher"
                )
                _add_discrepancy(
                    discrepancies,
                    field_pair="expected_installment_amount vs invoice_amount",
                    values={
                        "crm_contract_amount": crm.contract_amount,
                        "payment_terms": crm.payment_terms,
                        "installment_number": erp.installment_number,
                        "expected_installment_amount": expected_installment_amount,
                        "erp": erp.invoice_amount
                    },
                    difference=expected_installment_amount - erp.invoice_amount,
                    direction=direction
                )

        elif crm.contract_amount == erp.invoice_amount:
            _add_matched(
                matched,
                field="contract_amount vs invoice_amount",
                value=crm.contract_amount,
                note="CRM contract amount matches ERP invoice amount."
            )
        else:
            direction = "erp_lower" if erp.invoice_amount < crm.contract_amount else "erp_higher"
            _add_discrepancy(
                discrepancies,
                field_pair="contract_amount vs invoice_amount",
                values={
                    "crm": crm.contract_amount,
                    "erp": erp.invoice_amount
                },
                difference=crm.contract_amount - erp.invoice_amount,
                direction=direction
            )

    if erp.invoice_amount is not None and finance.payment_amount is not None:
        if _compare_fx_amounts(erp, finance, matched, discrepancies):
            return
        
        adjusted_payment = (
            finance.payment_amount
            + finance.tax_deduction
            + finance.bank_fee
            - finance.refund_amount
        )

        if adjusted_payment == erp.invoice_amount:
            _add_matched(
                matched,
                field="invoice_amount vs adjusted_payment_amount",
                value=adjusted_payment,
                note="Finance payment matches ERP invoice after tax deduction, bank fee and refund adjustment."
            )
        else:
            direction = "finance_lower" if adjusted_payment < erp.invoice_amount else "finance_higher"
            _add_discrepancy(
                discrepancies,
                field_pair="invoice_amount vs adjusted_payment_amount",
                values={
                    "erp": erp.invoice_amount,
                    "finance_payment": finance.payment_amount,
                    "tax_deduction": finance.tax_deduction,
                    "bank_fee": finance.bank_fee,
                    "refund_amount": finance.refund_amount,
                    "adjusted_finance": adjusted_payment
                },
                difference=erp.invoice_amount - adjusted_payment,
                direction=direction
            )


# 构造实体一致性摘要，用于记录三个系统中的实体名称和最终对齐名称。
def _build_entity_consistency(
    crm: CRMOutput,
    erp: ERPOutput,
    finance: FinanceOutput
) -> EntityConsistency:
    return EntityConsistency(
        crm=crm.entity,
        erp=erp.entity,
        finance=finance.entity,
        aligned_name=crm.entity_match.query if crm.entity_match else crm.entity,
        alignment_method="based on system-provided entity_match fields"
    )


# Reconciliation Agent 的主入口函数。
# 输入三个系统的结构化输出，返回 ReconciliationOutput。
def reconcile(
    crm: CRMOutput,
    erp: ERPOutput,
    finance: FinanceOutput,
    trace: AuditTrace | None = None
) -> ReconciliationOutput:
    """
    Reconciliation Agent core logic.

    This function only finds matched fields and discrepancies.
    It does not explain the reasons behind discrepancies.
    Root-Cause Agent should handle explanation later.
    """

    matched: list[MatchedField] = []
    discrepancies: list[Discrepancy] = []

    try:
        entity_consistency = _build_entity_consistency(crm, erp, finance)

        _check_required_fields(crm, erp, finance, discrepancies)
        _check_entity_match_confidence(crm, erp, finance, discrepancies)
        _check_date_signals(erp, finance, discrepancies)
        _check_customer_and_contract_ids(crm, erp, finance, matched, discrepancies)
        _check_invoice_linking(erp, finance, matched, discrepancies)
        _compare_currency(crm, erp, finance, matched, discrepancies)
        _compare_amounts(crm, erp, finance, matched, discrepancies)

        output = ReconciliationOutput(
            entity=entity_consistency.aligned_name,
            entity_consistency=entity_consistency,
            discrepancies=discrepancies,
            matched=matched,
            error=None
        )

        if trace is not None:
            discrepancy_count = len(discrepancies)

            if discrepancy_count == 0:
                decision = "Compared CRM, ERP and Finance outputs and found no discrepancies."
            elif discrepancy_count == 1:
                decision = "Compared CRM, ERP and Finance outputs and found 1 discrepancy."
            else:
                decision = f"Compared CRM, ERP and Finance outputs and found {discrepancy_count} discrepancies."
    
            add_step(trace, TraceStep(
                agent="reconciliation",
                layer="analysis",
                decision=decision,
                reason="Used rule-based checks for required fields, entity match confidence, customer/contract IDs, invoice linking, date signals, currency consistency, installment amount, adjusted payment amount, bank fee, tax deduction, refund adjustment, and FX conversion.",
                confidence=0.9,
                discrepancies_found=discrepancy_count
            ))

        return output

    except Exception as e:
        if trace is not None:
            add_step(trace, TraceStep(
                agent="reconciliation",
                layer="analysis",
                decision="Failed to reconcile system outputs.",
                reason=str(e),
                error=str(e)
            ))

        return ReconciliationOutput(
            entity=crm.entity if crm.entity else "",
            error=str(e)
        )


# 从 payment_terms 文本中提取百分比，用于分期付款金额计算。
def _extract_installment_percentages(payment_terms: str) -> list[float]:
    matches = re.findall(r"(\d+(?:\.\d+)?)\s*%", payment_terms)
    return [float(value) / 100 for value in matches]


# 根据合同总金额、付款条款和当前期数，计算当前期理论应开票金额。
def _expected_installment_amount(
    contract_amount: float,
    payment_terms: str,
    installment_number: int | None
) -> float | None:
    if installment_number is None:
        return None

    percentages = _extract_installment_percentages(payment_terms)

    if not percentages:
        return None

    index = installment_number - 1

    if index < 0 or index >= len(percentages):
        return None

    return contract_amount * percentages[index]