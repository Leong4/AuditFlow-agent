
# Local test script used only for quick development-time validation of
# reconciliation logic without relying on external services or APIs.
# Reads the CRM / ERP / Finance mock JSON files from data/, assembles inputs by
# shared case_id, and calls the reconciliation agent for local validation.

import json
from pathlib import Path

from shared.schemas import (
    CRMOutput,
    ERPOutput,
    FinanceOutput,
    EntityMatch,
    MatchMethod,
)

from shared.trace import new_trace
from agents.reconciliation.agent import reconcile


# Locate the project root and data directory so mock data can be found regardless
# of where the module is run from.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"


# Read the specified mock JSON file and return its records list.
def load_records(filename: str) -> list[dict]:
    path = DATA_DIR / filename

    with open(path, "r", encoding="utf-8") as file:
        data = json.load(file)

    return data["records"]


# Index records by case_id so data from the three systems can be aligned by case.
def index_by_case_id(records: list[dict]) -> dict[str, dict]:
    return {
        record["metadata"]["case_id"]: record
        for record in records
    }


# Convert the JSON entity_match field into the EntityMatch object defined in schemas.
def build_entity_match(data: dict | None) -> EntityMatch | None:
    if data is None:
        return None

    return EntityMatch(
        query=data["query"],
        matched_as=data["matched_as"],
        match_method=MatchMethod(data["match_method"]),
        confidence=data["confidence"],
    )


# Convert a CRM mock payload into CRMOutput.
# This only maps fields and does not make business judgments.
def build_crm_output(payload: dict) -> CRMOutput:
    return CRMOutput(
        system=payload.get("system", "crm"),
        entity=payload.get("entity", ""),
        entity_match=build_entity_match(payload.get("entity_match")),

        contract_amount=payload.get("contract_amount"),
        currency=payload.get("currency", "GBP"),
        sign_date=payload.get("sign_date", ""),
        status=payload.get("status", ""),
        sales_owner=payload.get("sales_owner", ""),

        payment_terms=payload.get("payment_terms", ""),
        exchange_rate_policy=payload.get("exchange_rate_policy", ""),
        late_payment_grace_period=payload.get("late_payment_grace_period", ""),

        data_freshness=payload.get("data_freshness", ""),
        error=payload.get("error"),

        customer_id=payload.get("customer_id", ""),
        contract_id=payload.get("contract_id", ""),
    )


# Convert an ERP mock payload into ERPOutput.
def build_erp_output(payload: dict) -> ERPOutput:
    return ERPOutput(
        system=payload.get("system", "erp"),
        entity=payload.get("entity", ""),
        entity_match=build_entity_match(payload.get("entity_match")),

        invoice_id=payload.get("invoice_id", ""),
        invoice_amount=payload.get("invoice_amount"),
        currency=payload.get("currency", "GBP"),
        invoice_date=payload.get("invoice_date", ""),
        due_date=payload.get("due_date", ""),
        delivery_status=payload.get("delivery_status", ""),
        installment_number=payload.get("installment_number"),

        invoice_rules=payload.get("invoice_rules", ""),

        data_freshness=payload.get("data_freshness", ""),
        error=payload.get("error"),

        customer_id=payload.get("customer_id", ""),
        contract_id=payload.get("contract_id", ""),
    )


# Convert a Finance mock payload into FinanceOutput.
# Includes payment amount, tax deduction, bank fee, FX fields, and related data.
def build_finance_output(payload: dict) -> FinanceOutput:
    return FinanceOutput(
        system=payload.get("system", "finance"),
        entity=payload.get("entity", ""),
        entity_match=build_entity_match(payload.get("entity_match")),

        payment_id=payload.get("payment_id", ""),
        payment_amount=payload.get("payment_amount"),
        currency=payload.get("currency", "GBP"),
        payment_date=payload.get("payment_date", ""),
        payment_method=payload.get("payment_method", ""),
        exchange_rate=payload.get("exchange_rate"),
        refund_amount=payload.get("refund_amount", 0.0),
        tax_deduction=payload.get("tax_deduction", 0.0),
        overdue_days=payload.get("overdue_days", 0),

        exchange_rate_policy=payload.get("exchange_rate_policy", ""),

        data_freshness=payload.get("data_freshness", ""),
        error=payload.get("error"),

        customer_id=payload.get("customer_id", ""),
        contract_id=payload.get("contract_id", ""),
        invoice_id=payload.get("invoice_id", ""),
        bank_fee=payload.get("bank_fee", 0.0),
        original_currency_amount=payload.get("original_currency_amount"),
        exchange_rate_date=payload.get("exchange_rate_date", ""),
    )


# Run one test case: build the three system outputs, create a trace, and call reconcile().
def run_case(case_id: str, crm_record: dict, erp_record: dict, finance_record: dict) -> None:
    crm = build_crm_output(crm_record["payload"])
    erp = build_erp_output(erp_record["payload"])
    finance = build_finance_output(finance_record["payload"])

    trace = new_trace(
        entity=crm_record["metadata"].get("case_id", case_id),
        raw_query=f"Local test for {case_id}",
    )

    result = reconcile(crm, erp, finance, trace)

    print("=" * 80)
    print(f"CASE: {case_id}")
    print(f"DESCRIPTION: {crm_record['metadata'].get('description', '')}")
    print("-" * 80)

    print("Discrepancies:")
    if result.discrepancies:
        for item in result.discrepancies:
            print(f"  - {item}")
    else:
        print("  None")

    print("\nMatched fields:")
    if result.matched:
        for item in result.matched:
            print(f"  - {item}")
    else:
        print("  None")

    print("\nTrace:")
    print(trace.to_dict())
    print()


# Main function: read the three mock files, find case_ids shared by all three
# systems, and run each test case.
def main():
    crm_records = index_by_case_id(load_records("crm_mock.json"))
    erp_records = index_by_case_id(load_records("erp_mock.json"))
    finance_records = index_by_case_id(load_records("finance_mock.json"))

    common_case_ids = sorted(
        set(crm_records.keys())
        & set(erp_records.keys())
        & set(finance_records.keys())
    )

    if not common_case_ids:
        print("No matching case_id found across CRM, ERP and Finance mock data.")
        return

    for case_id in common_case_ids:
        run_case(
            case_id=case_id,
            crm_record=crm_records[case_id],
            erp_record=erp_records[case_id],
            finance_record=finance_records[case_id],
        )


if __name__ == "__main__":
    main()
