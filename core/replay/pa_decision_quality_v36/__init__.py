"""Offline, evidence-first V36 price-action decision research.

This package is intentionally disconnected from production order submission.
"""

from .context import CausalContext, build_context
from .experiments import EXPERIMENTS

__all__ = ["EXPERIMENTS", "CausalContext", "build_context"]
