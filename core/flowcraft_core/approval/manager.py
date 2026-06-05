from __future__ import annotations

from flowcraft_core.domain.enums import RiskLevel
from flowcraft_core.domain.schemas import ApprovalRequest, PolicyDecision


class ApprovalManager:
    def create_from_policy(self, decision: PolicyDecision, title: str, description: str) -> ApprovalRequest:
        return ApprovalRequest(
            task_id=decision.task_id,
            step_id=decision.step_id,
            action_title=title,
            action_description=description,
            risk_level=decision.risk_level or RiskLevel.MEDIUM,
            impact_preview=[decision.reason],
        )

