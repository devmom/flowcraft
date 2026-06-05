from __future__ import annotations

import asyncio as _asyncio
import logging

from flowcraft_core.domain.schemas import AgentRequest, TaskBrief
from flowcraft_core.models.gateway import ModelGateway

logger = logging.getLogger(__name__)

INTENT_TIMEOUT = 20  # seconds


class IntentEngine:
    def __init__(self, model_gateway: ModelGateway) -> None:
        self.model_gateway = model_gateway

    async def recognize(self, task_id: str, request: AgentRequest) -> TaskBrief:
        try:
            payload = await _asyncio.wait_for(
                self.model_gateway.generate_structured(request.raw_input, "TaskBrief"),
                timeout=INTENT_TIMEOUT,
            )
        except _asyncio.TimeoutError:
            logger.warning("Intent recognition timed out (%ds)", INTENT_TIMEOUT)
            # Fallback: heuristic intent
            payload = self.model_gateway._heuristic_task_brief(request.raw_input)
        except Exception as exc:
            logger.warning("Intent recognition failed: %s, using heuristic", exc)
            payload = self.model_gateway._heuristic_task_brief(request.raw_input)
        return TaskBrief(task_id=task_id, **payload)

