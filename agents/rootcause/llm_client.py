from __future__ import annotations

import json
import os
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

from shared.schemas import (
    AnomalyStatus,
    RiskLevel,
)


load_dotenv()


DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

"""prompt文件地址"""
PROMPT_PATH = (
    Path(__file__).parent
    / "prompts"
    / "root_cause_prompt.txt"
)


class LLMClientError(Exception):
    """Error raised by Root-Cause LLM client."""


def _json_safe(obj: Any) -> Any:
    """
    Convert Python objects into JSON-safe data.

    Supports:
    - dataclass
    - enum
    - dict
    - list
    - primitive values
    """

    if obj is None:
        return None

    if isinstance(obj, Enum):
        return obj.value

    if is_dataclass(obj):
        return _json_safe(asdict(obj))

    if isinstance(obj, dict):
        return {str(key): _json_safe(value) for key, value in obj.items()}

    if isinstance(obj, list):
        return [_json_safe(item) for item in obj]

    return obj


def _load_prompt_template() -> str:
    """
    Load prompt template from local txt file.
    """

    if not PROMPT_PATH.exists():
        raise LLMClientError(f"Prompt file not found: {PROMPT_PATH}")

    return PROMPT_PATH.read_text(encoding="utf-8")


def _build_prompt(payload: dict[str, Any]) -> str:
    """
    Insert runtime payload into prompt template.
    """

    template = _load_prompt_template()

    payload_json = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
    )

    return template.replace("{{payload}}", payload_json)


def _safe_status(value: str) -> AnomalyStatus:
    """
    Convert string to AnomalyStatus safely.
    """

    try:
        return AnomalyStatus(value)
    except ValueError:
        return AnomalyStatus.WATCH


def _safe_risk_level(value: str) -> RiskLevel:
    """
    Convert string to RiskLevel safely.
    """

    try:
        return RiskLevel(value)
    except ValueError:
        return RiskLevel.MEDIUM


class RootCauseLLMClient:
    """
    OpenAI client for Root-Cause Agent.

    This class only does three things:
    1. Build prompt.
    2. Call OpenAI.
    3. Return parsed JSON result.

    It should NOT:
    - Query CRM / ERP / Finance.
    - Recalculate reconciliation differences.
    - Control Band room routing.
    """

    def __init__(self, model: str = DEFAULT_MODEL):
        api_key = os.getenv("OPENAI_API_KEY")

        if not api_key:
            raise LLMClientError(
                "Missing OPENAI_API_KEY. "
                "Please add it to your local .env file."
            )

        self.model = model
        self.client = OpenAI(api_key=api_key)

    def analyze(
        self,
        *,
        reconciliation_output: Any,
        crm_output: Any | None = None,
        erp_output: Any | None = None,
        finance_output: Any | None = None,
        trace_id: str = "",
    ) -> dict[str, Any]:
        """
        Analyze reconciliation discrepancies with OpenAI.

        Returns:
            A parsed JSON dict from the LLM.

        Later, agent.py can convert this dict into RootCauseOutput
        if your shared.schemas requires that.
        """

        payload = {
            "trace_id": trace_id,
            "reconciliation_output": _json_safe(reconciliation_output),
            "crm_output": _json_safe(crm_output),
            "erp_output": _json_safe(erp_output),
            "finance_output": _json_safe(finance_output),
        }

        prompt = _build_prompt(payload)

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                temperature=0.1,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a strict financial audit root-cause analyst. "
                            "Return valid JSON only."
                        ),
                    },
                    {
                        "role": "user",
                        "content": prompt,
                    },
                ],
            )
        except Exception as exc:
            raise LLMClientError(f"OpenAI API call failed: {exc}") from exc

        content = response.choices[0].message.content

        if not content:
            raise LLMClientError("OpenAI returned empty response.")

        try:
            result = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMClientError(
                f"OpenAI returned invalid JSON: {content}"
            ) from exc

        return self._normalize_result(result)

    def _normalize_result(self, result: dict[str, Any]) -> dict[str, Any]:
        """
        Clean and normalize LLM JSON result.

        This step prevents invalid enum values from breaking the pipeline.
        """

        anomalies = result.get("anomalies", [])

        if not isinstance(anomalies, list):
            anomalies = []

        normalized_anomalies = []

        for item in anomalies:
            if not isinstance(item, dict):
                continue

            status = _safe_status(item.get("status", "watch"))
            risk_level = _safe_risk_level(item.get("risk_level", "medium"))

            normalized_anomalies.append(
                {
                    "field_pair": item.get("field_pair", ""),
                    "probable_cause": item.get("probable_cause", "unknown"),
                    "confidence": float(item.get("confidence", 0.0)),
                    "evidence": item.get("evidence", []),
                    "alternative_causes": item.get("alternative_causes", []),
                    "status": status.value,
                    "risk_level": risk_level.value,
                    "requires_human": bool(item.get("requires_human", True)),
                    "recommended_action": item.get("recommended_action", ""),
                }
            )

        summary = result.get("summary", {})

        if not isinstance(summary, dict):
            summary = {}

        normalized_result = {
            "anomalies": normalized_anomalies,
            "summary": {
                "total_discrepancies": int(
                    summary.get(
                        "total_discrepancies",
                        len(normalized_anomalies),
                    )
                ),
                "normal": int(summary.get("normal", 0)),
                "watch": int(summary.get("watch", 0)),
                "anomaly": int(summary.get("anomaly", 0)),
            },
        }

        return normalized_result