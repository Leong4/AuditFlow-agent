from __future__ import annotations

from typing import Any

from agents.rootcause.llm_client import RootCauseLLMClient, LLMClientError
from shared.schemas import (
    AnomalyAnalysis,
    AnomalyStatus,
    ReconciliationSummary,
    RootCauseOutput,
    RiskLevel,
)


class RootCauseAgent:
    """
    Root-Cause Agent.

    Responsibility:
    - Receive reconciliation result and system facts.
    - Call LLM client to analyze root causes.
    - Convert LLM dict result into RootCauseOutput.

    It should NOT:
    - Query CRM / ERP / Finance data.
    - Recalculate reconciliation differences.
    - Modify shared.schemas.
    """

    def __init__(self, llm_client: RootCauseLLMClient | None = None):
        self.llm_client = llm_client or RootCauseLLMClient()

    def run(
        self,
        *,
        reconciliation_output: Any,
        crm_output: Any | None = None,
        erp_output: Any | None = None,
        finance_output: Any | None = None,
        trace_id: str = "",
    ) -> RootCauseOutput:
        """
        Main entry point for Root-Cause Agent.

        Args:
            reconciliation_output:
                Output from Reconciliation Agent.
            crm_output:
                Output from CRM Agent.
            erp_output:
                Output from ERP Agent.
            finance_output:
                Output from Finance Agent.
            trace_id:
                Trace id from the pipeline.

        Returns:
            RootCauseOutput
        """

        entity = self._get_entity(reconciliation_output)

        try:
            llm_result = self.llm_client.analyze(
                reconciliation_output=reconciliation_output,
                crm_output=crm_output,
                erp_output=erp_output,
                finance_output=finance_output,
                trace_id=trace_id,
            )

            return self._to_root_cause_output(
                llm_result=llm_result,
                entity=entity,
                trace_id=trace_id,
            )

        except Exception as exc:
            return RootCauseOutput(
                entity=entity,
                anomalies=[],
                summary=ReconciliationSummary(
                    total_discrepancies=0,
                    normal=0,
                    anomaly=0,
                    watch=0,
                ),
                trace_id=trace_id,
                error=f"RootCauseAgent failed: {exc}",
            )

    def _to_root_cause_output(
        self,
        *,
        llm_result: dict[str, Any],
        entity: str,
        trace_id: str,
    ) -> RootCauseOutput:
        """
        Convert LLM dict result into shared.schemas.RootCauseOutput.
        """

        anomalies_data = llm_result.get("anomalies", [])

        anomalies: list[AnomalyAnalysis] = []

        for item in anomalies_data:
            if not isinstance(item, dict):
                continue

            anomaly = AnomalyAnalysis(
                field_pair=item.get("field_pair", ""),
                probable_cause=item.get("probable_cause", "unknown"),
                confidence=float(item.get("confidence", 0.0)),
                evidence=item.get("evidence", []),
                alternative_causes=item.get("alternative_causes", []),
                status=self._safe_status(item.get("status", "watch")),
                risk_level=self._safe_risk_level(item.get("risk_level", "medium")),
                requires_human=bool(item.get("requires_human", True)),
                recommended_action=item.get("recommended_action", ""),
            )

            anomalies.append(anomaly)

        summary_data = llm_result.get("summary", {})

        summary = ReconciliationSummary(
            total_discrepancies=int(
                summary_data.get("total_discrepancies", len(anomalies))
            ),
            normal=int(summary_data.get("normal", 0)),
            anomaly=int(summary_data.get("anomaly", 0)),
            watch=int(summary_data.get("watch", 0)),
        )

        return RootCauseOutput(
            entity=entity,
            anomalies=anomalies,
            summary=summary,
            trace_id=trace_id,
            error=None,
        )

    def _get_entity(self, reconciliation_output: Any) -> str:
        """
        Get entity name from ReconciliationOutput or dict.
        """

        if reconciliation_output is None:
            return ""

        if isinstance(reconciliation_output, dict):
            return reconciliation_output.get("entity", "")

        return getattr(reconciliation_output, "entity", "")

    def _safe_status(self, value: Any) -> AnomalyStatus:
        """
        Convert string to AnomalyStatus safely.
        """

        try:
            return AnomalyStatus(value)
        except ValueError:
            return AnomalyStatus.WATCH

    def _safe_risk_level(self, value: Any) -> RiskLevel:
        """
        Convert string to RiskLevel safely.
        """

        try:
            return RiskLevel(value)
        except ValueError:
            return RiskLevel.MEDIUM


def run_rootcause_agent(
    *,
    reconciliation_output: Any,
    crm_output: Any | None = None,
    erp_output: Any | None = None,
    finance_output: Any | None = None,
    trace_id: str = "",
) -> RootCauseOutput:
    """
    Function-style entry point.

    This is convenient for other agents or pipeline code.
    """

    agent = RootCauseAgent()

    return agent.run(
        reconciliation_output=reconciliation_output,
        crm_output=crm_output,
        erp_output=erp_output,
        finance_output=finance_output,
        trace_id=trace_id,
    )