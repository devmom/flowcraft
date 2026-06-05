"""Rule-based evaluators for deterministic quality checks.

Each evaluator follows the interface:
    class Evaluator:
        name: str
        def evaluate(self, traces: list[dict], case: dict) -> dict
"""

from .state_transition import StateTransitionEvaluator
from .structure import StructureEvaluator
from .security import SecurityEvaluator
from .chain import ChainEvaluator
from .efficiency import EfficiencyEvaluator

__all__ = [
    "StateTransitionEvaluator",
    "StructureEvaluator",
    "SecurityEvaluator",
    "ChainEvaluator",
    "EfficiencyEvaluator",
]
