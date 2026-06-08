from __future__ import annotations

from typing import Optional

from shared.schemas import (
    ReconciliationOutput,
    RootCauseOutput,
    ReconciliationSummary,
)

from agents.rootcause.rules_handle import run_root_cause_agent
from agents.rootcause.llm_client import RootCauseLLMClient, LLMClientError


class RootCauseAgent:
    """
    Top-level Root-Cause Agent.

    This class is the external entry point for the Root-Cause module.

    Responsibilities:
    - receive ReconciliationOutput from Reconciliation Agent
    - decide whether to enable LLM enhancement
    - create RootCauseLLMClient if needed
    - call rule-based Root-Cause Agent
    - return RootCauseOutput

    It does NOT:
    - detect discrepancies
    - recalculate reconciliation results
    - directly call OpenAI
    - build prompts
    """

    def __init__(
        self,
        *,
        use_llm: bool = True,
        llm_client: Optional[RootCauseLLMClient] = None,
    ):
        self.use_llm = use_llm
        self.llm_client = llm_client

    def run(
        self,
        reconciliation_output: ReconciliationOutput,
        trace_id: str = "",
    ) -> RootCauseOutput:
        """
        Run Root-Cause Agent from ReconciliationOutput to RootCauseOutput.
        """

        if reconciliation_output.error:
            return RootCauseOutput(
                entity=reconciliation_output.entity,
                anomalies=[],
                summary=ReconciliationSummary(),
                trace_id=trace_id,
                error=reconciliation_output.error,
            )

        llm_client = self._get_llm_client()

        try:
            return run_root_cause_agent(
                reconciliation=reconciliation_output,
                trace_id=trace_id,
                llm_client=llm_client,
            )
        except Exception as exc:
            return RootCauseOutput(
                entity=reconciliation_output.entity,
                anomalies=[],
                summary=ReconciliationSummary(),
                trace_id=trace_id,
                error=f"Root-Cause Agent failed: {exc}",
            )

    def _get_llm_client(self) -> Optional[RootCauseLLMClient]:
        """
        Return an LLM client if LLM enhancement is enabled.

        If use_llm=False:
            return None

        If an llm_client was injected:
            reuse it

        Otherwise:
            create RootCauseLLMClient from .env
        """

        if not self.use_llm:
            return None

        if self.llm_client is not None:
            return self.llm_client

        try:
            self.llm_client = RootCauseLLMClient()
            return self.llm_client
        except LLMClientError:
            # If OPENAI_API_KEY is missing or client init fails,
            # fallback to rule-only mode instead of breaking the pipeline.
            return None