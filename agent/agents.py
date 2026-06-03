"""RouteRun Agentic Coaching — Agent definitions.

Architecture: Supervisor (Head Coach) + Sub-agents (Periodization, Readiness) + Evaluator
Pattern: B (Supervisor multi-agent) + D (in-loop Evaluator)

Model selection (R1 confirmed 2026-06-03, §8 Q2 決定):
  PLANNER = gemini-2.5-pro  — Head Coach・Periodization の推論・計画
  FAST    = gemini-2.5-flash   — Readiness (thinking=medium)・Evaluator（GA・最安）
  Note: プランに記載の gemini-3.5-flash は存在しないため gemini-2.5-flash を採用
"""

import os
from google.adk.agents import Agent
from google.adk.tools.agent_tool import AgentTool
from google.genai import types

from .tools import (
    get_athlete_state,
    suggest_route_areas,
    recommend_races,
    chat_with_coach,
    write_training_plan,
)
from .instructions import (
    HEAD_COACH_INSTRUCTION,
    PERIODIZATION,
    READINESS,
    EVALUATOR,
)

PLANNER = os.getenv("PLANNER_MODEL", "gemini-2.5-pro")
FAST = os.getenv("FAST_MODEL", "gemini-2.5-flash")

# thinking=medium: Readiness は HRV/睡眠データの解釈が必要なため中程度の推論を許可
# budget 1024 = medium level (low=0, medium≈1024, high≈8192)
_THINKING_MEDIUM = types.GenerateContentConfig(
    thinking_config=types.ThinkingConfig(thinking_budget=1024)
)

# ── Sub-agents ─────────────────────────────────────────────────────────────

periodization = Agent(
    name="periodization",
    model=PLANNER,
    instruction=PERIODIZATION,
    tools=[get_athlete_state],
)

readiness = Agent(
    name="readiness",
    model=FAST,
    instruction=READINESS,
    tools=[get_athlete_state],
    generate_content_config=_THINKING_MEDIUM,
)

evaluator = Agent(
    name="evaluator",
    model=FAST,
    instruction=EVALUATOR,
)

# ── Head Coach (Supervisor) ────────────────────────────────────────────────

head_coach = Agent(
    name="ayumu_head_coach",
    model=PLANNER,
    instruction=HEAD_COACH_INSTRUCTION,
    tools=[
        # Sub-agents (AgentTool で委譲)
        AgentTool(agent=periodization),
        AgentTool(agent=readiness),
        AgentTool(agent=evaluator),
        # Direct tools
        get_athlete_state,
        suggest_route_areas,
        recommend_races,
        chat_with_coach,
        write_training_plan,
    ],
)
