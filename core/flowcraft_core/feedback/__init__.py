"""FlowCraft Feedback Module — Agent Vent Mode.

Detects user frustration, provides structured venting outlet,
and converts feedback into actionable system improvements.

Exports:
    FrustrationDetector, FrustrationAssessment — sentiment detection
    VentSessionManager, VentSession, VentTemplate — vent session lifecycle
    PhraseLibrary, Phrase — curated vent phrase management
    AgentResponseSanitizer — one-way emotional channel guard
    InsightMapper — feedback -> FailureType mapping
    FeedbackMemoryIntegrator — feedback -> persistent memory
"""

from __future__ import annotations

from flowcraft_core.feedback.sentiment import FrustrationDetector, FrustrationAssessment
from flowcraft_core.feedback.vent_session import VentSessionManager, VentSession, VentTemplate
from flowcraft_core.feedback.phrase_library import PhraseLibrary, Phrase
from flowcraft_core.feedback.agent_response_guard import AgentResponseSanitizer
from flowcraft_core.feedback.insight_mapper import InsightMapper
from flowcraft_core.feedback.feedback_memory_integrator import FeedbackMemoryIntegrator

__all__ = [
    "FrustrationDetector",
    "FrustrationAssessment",
    "VentSessionManager",
    "VentSession",
    "VentTemplate",
    "PhraseLibrary",
    "Phrase",
    "AgentResponseSanitizer",
    "InsightMapper",
    "FeedbackMemoryIntegrator",
]
