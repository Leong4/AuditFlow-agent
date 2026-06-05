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

def _add_matched(matched: list[MatchedField], field: str, value, note: str = "") -> None:
    matched.append(MatchedField(
        field=field,
        value=value,
        consistent=True,
        note=note
    ))


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

def _parse_date(value: str) -> date | None:
    if not value:
        return None

    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _check_required_fields(
    crm: CRMOutput,
    erp: ERPOutput,
    finance: FinanceOutput,
    discrepancies: list[Discrepancy]
) -> None:
    missing_fields = {}

    crm_required = {
        "entity": crm.entity,
        "contract_amount": crm.contract_amount,
        "currency": crm.currency,
        "payment_terms": crm.payment_terms,
    }

    erp_required = {
        "entity": erp.entity,
        "invoice_amount": erp.invoice_amount,
        "currency": erp.currency,
        "invoice_date": erp.invoice_date,
        "due_date": erp.due_date,
    }

    finance_required = {
        "entity": finance.entity,
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
    else:
        _add_discrepancy(
            discrepancies,
            field_pair="currency across systems",
            values=values,
            difference=0.0,
            direction="currency_mismatch"
        )


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
      payment_amount + tax_deduction - refund_amount.
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
        adjusted_payment = finance.payment_amount + finance.tax_deduction - finance.refund_amount

        if adjusted_payment == erp.invoice_amount:
            _add_matched(
                matched,
                field="invoice_amount vs adjusted_payment_amount",
                value=adjusted_payment,
                note="Finance payment matches ERP invoice after tax deduction and refund adjustment."
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
                    "refund_amount": finance.refund_amount,
                    "adjusted_finance": adjusted_payment
                },
                difference=erp.invoice_amount - adjusted_payment,
                direction=direction
            )


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
        else:
            decision = f"Compared CRM, ERP and Finance outputs and found {discrepancy_count} discrepancy/discrepancies."

        add_step(trace, TraceStep(
            agent="reconciliation",
            layer="analysis",
            decision=decision,
            reason="Used rule-based checks for required fields, entity match confidence, date signals, currency consistency, installment amount, and adjusted payment amount.",
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
    
def _extract_installment_percentages(payment_terms: str) -> list[float]:
    matches = re.findall(r"(\d+(?:\.\d+)?)\s*%", payment_terms)
    return [float(value) / 100 for value in matches]


def _expected_installment_amount(contract_amount: float, payment_terms: str, installment_number: int | None) -> float | None:
    if installment_number is None:
        return None

    percentages = _extract_installment_percentages(payment_terms)

    if not percentages:
        return None

    index = installment_number - 1

    if index < 0 or index >= len(percentages):
        return None

    return contract_amount * percentages[index]