import json
from pathlib import Path

from agents.rootcause.llm_client import RootCauseLLMClient

"""
    测试用例：
    CRM contract_amount = 150000
    ERP invoice_amount = 60000
    Finance payment_amount = 60000
    CRM payment_terms = 3 installments: 40%, 40%, 20%
"""

def load_json(file_path: Path):
    with file_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def find_record_by_case_id(data: dict, case_id: str) -> dict:
    for record in data["records"]:
        if record["metadata"]["case_id"] == case_id:
            return record

    raise ValueError(f"Case not found: {case_id}")


def main():
    project_root = Path(__file__).resolve().parents[3]
    data_dir = project_root / "data"

    crm_data = load_json(data_dir / "crm_mock.json")
    erp_data = load_json(data_dir / "erp_mock.json")
    finance_data = load_json(data_dir / "finance_mock.json")

    case_id = "case_002_clean_installment_first"

    crm_record = find_record_by_case_id(crm_data, case_id)
    erp_record = find_record_by_case_id(erp_data, case_id)
    finance_record = find_record_by_case_id(finance_data, case_id)

    crm_payload = crm_record["payload"]
    erp_payload = erp_record["payload"]
    finance_payload = finance_record["payload"]

    contract_amount = crm_payload["contract_amount"]
    invoice_amount = erp_payload["invoice_amount"]
    payment_amount = finance_payload["payment_amount"]

    mock_reconciliation_output = {
        "entity": crm_payload["entity"],
        "case_id": case_id,
        "discrepancies": [
            {
                "field_pair": "contract_amount vs invoice_amount",
                "crm_value": contract_amount,
                "erp_value": invoice_amount,
                "difference": contract_amount - invoice_amount,
                "note": "CRM contract amount is larger than ERP invoice amount.",
            },
            {
                "field_pair": "contract_amount vs payment_amount",
                "crm_value": contract_amount,
                "finance_value": payment_amount,
                "difference": contract_amount - payment_amount,
                "note": "CRM contract amount is larger than finance payment amount.",
            },
        ],
    }

    client = RootCauseLLMClient()

    result = client.analyze(
        reconciliation_output=mock_reconciliation_output,
        crm_output=crm_record,
        erp_output=erp_record,
        finance_output=finance_record,
        trace_id="test_case_002_installment",
    )

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()