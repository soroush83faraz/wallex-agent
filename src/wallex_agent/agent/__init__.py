"""Adaptive decision-making agent: features -> tools -> hypotheses -> bandit-gated plan -> reflection.

Deterministic given ``config.seed``; no exchange I/O except through the ``BookSource``/tools given.
"""
from .brain import AgentBrain
from .features import PriceHistory, compute_features
from .memory import AgentMemory
from .planner import Observation, PlanContext, Planner
from .reflection import Reflection, reflect, render_trace, trace_record
from .strategies import Hypothesis
from .tools import TOOLS, Tool, ToolResult

__all__ = ["AgentBrain", "AgentMemory", "Hypothesis", "Observation", "PlanContext", "Planner", "PriceHistory",
           "Reflection", "TOOLS", "Tool", "ToolResult", "compute_features", "reflect", "render_trace",
           "trace_record"]
