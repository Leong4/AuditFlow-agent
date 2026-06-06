"""
Compatibility helpers for the installed thenvoi PydanticAIAdapter.

The installed adapter currently creates pydantic_ai.Agent with a null output
type, but the installed pydantic_ai version requires a real output type. This
wrapper keeps the public Thenvoi adapter usage unchanged while forcing string
output.
"""

from __future__ import annotations

from typing import Any

import thenvoi.adapters.pydantic_ai as thenvoi_pydantic_ai
from thenvoi.adapters import PydanticAIAdapter as BasePydanticAIAdapter


class PydanticAIAdapter(BasePydanticAIAdapter):
    """PydanticAIAdapter variant that creates string-returning agents."""

    def _create_agent(self) -> Any:
        original_agent = thenvoi_pydantic_ai.Agent

        def string_output_agent(*args: Any, **kwargs: Any) -> Any:
            if kwargs.get("output_type") is None:
                kwargs["output_type"] = str
            return original_agent(*args, **kwargs)

        thenvoi_pydantic_ai.Agent = string_output_agent
        try:
            return super()._create_agent()
        finally:
            thenvoi_pydantic_ai.Agent = original_agent
