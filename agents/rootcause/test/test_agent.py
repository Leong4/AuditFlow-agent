import json
from dataclasses import asdict, is_dataclass
from pathlib import Path

from agents.rootcause.agent import run_rootcause_agent


def load_json(file_path: Path):
    with file_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def find_record_by_case_id(data: dict, case_id: str) -> dict:
    for record in data["records"]:
        if record["metadata"]["case_id"] == case_id:
            return record

    raise ValueError(f"Case not found: {case_id}")


def json_safe(obj):
    if is_dataclass(obj):
        return asdict(obj)

    if isinstance(obj, dict):
        return {key: json_safe(value) for key, value in obj.items()}

    if isinstance(obj, list):
        return [json_safe(item) for item in obj]

    return obj


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

    reconciliation_output = {
        "entity": crm_payload["entity"],
        "case_id": case_id,
        "discrepancies": [
            {
                "field_pair": "contract_amount vs invoice_amount",
                "values": {
                    "crm": contract_amount,
                    "erp": invoice_amount,
                },
                "difference": contract_amount - invoice_amount,
                "direction": "erp_lower",
            },
            {
                "field_pair": "contract_amount vs payment_amount",
                "values": {
                    "crm": contract_amount,
                    "finance": payment_amount,
                },
                "difference": contract_amount - payment_amount,
                "direction": "finance_lower",
            },
        ],
    }

    result = run_rootcause_agent(
        reconciliation_output=reconciliation_output,
        crm_output=crm_record,
        erp_output=erp_record,
        finance_output=finance_record,
        trace_id="test_agent_case_002",
    )

    print(json.dumps(json_safe(result), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()