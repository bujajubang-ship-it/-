"""Shared strategy-brain primitives.

This package is intentionally not wired to production routes yet.  The legacy
Analyzer remains the active implementation until each mode passes its rollout
gate.
"""

from .brain import StrategyBrain
from .config import BrainSettings
from .contracts import BrainRequest, BrainResult, EvidenceEnvelope, StrategyMode

__all__ = [
    "BrainRequest",
    "BrainResult",
    "BrainSettings",
    "EvidenceEnvelope",
    "StrategyBrain",
    "StrategyMode",
]
