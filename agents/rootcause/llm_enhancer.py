from __future__ import annotations

from typing import Optional

from shared.schemas import (
    AnomalyAnalysis,
    AnomalyStatus,
    Discrepancy,
    ReconciliationOutput,
)

from agents.rootcause.llm_client import RootCauseLLMClient, LLMClientError


def should_call_llm(rule_analysis: AnomalyAnalysis) -> bool:
    """
    Decide whether this rule-based result needs LLM enhancement.

    The LLM should only be used when:
    - the rule result is uncertain
    - the root cause is unknown / possible / unclear
    - the case is a confirmed anomaly but needs better explanation
    - the recommended action is missing

    The LLM should not be called for simple deterministic cases such as:
    - tax deduction
    - bank fee
    - valid FX conversion
    - exact installment match
    - ID mismatch
    - missing required field
    - payment before invoice
    """

    cause = (rule_analysis.probable_cause or "").lower()

    # 1. WATCH means rule layer is not fully certain.
    if rule_analysis.status == AnomalyStatus.WATCH:
        return True

    # 2. Unknown / possible / unclear causes need LLM explanation.
    if cause.startswith("unknown"):
        return True

    if cause.startswith("possible"):
        return True

    if "unclear" in cause:
        return True

    # 3. These deterministic anomalies benefit from alternative causes
    # and better audit-style recommended actions.
    if cause in {
        "unexplained_amount_mismatch",
        "fx_context_incomplete",
        "date_discrepancy_unclear",
        "unknown_discrepancy",
        "possible_entity_mismatch",
        "possible_entity_alias",
    }:
        return True

    # 4. If rule layer did not provide action, ask LLM to write one.
    if not rule_analysis.recommended_action:
        return True

    return False


def maybe_enhance_with_llm(
    *,
    rule_analysis: AnomalyAnalysis,
    discrepancy: Discrepancy,
    reconciliation: ReconciliationOutput,
    llm_client: Optional[RootCauseLLMClient] = None,
    trace_id: str = "",
) -> AnomalyAnalysis:
    """
    Enhance one rule-based AnomalyAnalysis with LLM if needed.

    If llm_client is None:
        return the rule-based result.

    If LLM fails:
        keep the rule-based result and append failure info to evidence.

    Important:
        The LLM output is only allowed to update:
        - evidence: append explanation
        - alternative_causes
        - recommended_action

        The LLM output must NOT update:
        - field_pair
        - probable_cause
        - confidence
        - status
        - risk_level
        - requires_human
    """

    if llm_client is None:
        return rule_analysis

    if not should_call_llm(rule_analysis):
        return rule_analysis

    try:
        llm_result = llm_client.enhance_analysis(
            discrepancy=discrepancy,
            rule_analysis=rule_analysis,
            matched=reconciliation.matched,
            entity_consistency=reconciliation.entity_consistency,
            trace_id=trace_id,
        )
    except LLMClientError as exc:
        return append_llm_failure(
            rule_analysis=rule_analysis,
            error_message=str(exc),
        )

    return merge_llm_result(
        rule_analysis=rule_analysis,
        llm_result=llm_result,
    )


def merge_llm_result(
    *,
    rule_analysis: AnomalyAnalysis,
    llm_result: dict,
) -> AnomalyAnalysis:
    """
    Merge LLM enhancement result into rule-based AnomalyAnalysis.

    Expected llm_result:
    {
        "explanation": str,
        "alternative_causes": list[dict],
        "recommended_action": str
    }

    This function intentionally preserves the rule-based judgment fields.
    """

    explanation = llm_result.get("explanation", "")
    alternative_causes = llm_result.get("alternative_causes", [])
    recommended_action = llm_result.get("recommended_action", "")

    if isinstance(explanation, str) and explanation.strip():
        rule_analysis.evidence.append(
            f"Detailed explanation: {explanation.strip()}"
        )

    if is_valid_alternative_causes(alternative_causes):
        rule_analysis.alternative_causes = alternative_causes

    if isinstance(recommended_action, str) and recommended_action.strip():
        rule_analysis.recommended_action = recommended_action.strip()

    return rule_analysis


def append_llm_failure(
    *,
    rule_analysis: AnomalyAnalysis,
    error_message: str,
) -> AnomalyAnalysis:
    """
    Keep rule-based result when LLM enhancement fails.

    This makes the pipeline robust:
    - no API key
    - API timeout
    - invalid JSON
    - prompt file missing
    will not break the whole Root-Cause Agent.
    """

    rule_analysis.evidence.append(
        "LLM enhancement failed; rule-based result preserved. "
        f"Error: {error_message}"
    )

    return rule_analysis


def is_valid_alternative_causes(value: object) -> bool:
    """
    Validate alternative_causes.

    Expected:
    [
        {"cause": "client_underpayment", "confidence": 0.7}
    ]
    """

    if not isinstance(value, list):
        return False

    for item in value:
        if not isinstance(item, dict):
            return False

        cause = item.get("cause")
        confidence = item.get("confidence")

        if not isinstance(cause, str) or not cause.strip():
            return False

        try:
            confidence_float = float(confidence)
        except (TypeError, ValueError):
            return False

        if not 0.0 <= confidence_float <= 1.0:
            return False

    return True