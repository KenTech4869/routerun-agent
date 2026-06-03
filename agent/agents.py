"""RouteRun Agentic Coaching — Agent definitions.

Architecture: Supervisor (Head Coach) + Sub-agents (Periodization, Readiness) + Evaluator
Pattern: B (Supervisor multi-agent) + D (in-loop Evaluator)

Model selection (R2 updated 2026-06-04):
  PLANNER = gemini-2.5-pro  — Head Coach・Periodization（thinking 有効）
  FAST    = gemini-2.5-flash — Readiness（thinking=medium, HRV 解釈）
  LITE    = gemini-2.5-flash-lite — Evaluator（パターンマッチのみ、pro 不要）

Thinking budgets:
  Head Coach    : 2048 — supervisor 判断 + 出力 JSON 組み立ての品質向上
  Periodization : 4096 — メソ/マイクロサイクル設計は最重要推論タスク
  Readiness     : 1024 — HRV/睡眠シグナル解釈に中程度の推論
  Evaluator     : 0    — "slow/遅い" 検出・ACWR 数値チェックはパターンマッチで十分
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
LITE = os.getenv("LITE_MODEL", "gemini-2.5-flash-lite")

# Head Coach: supervisor 判断 + JSON 組み立て品質向上
_THINKING_HEAD = types.GenerateContentConfig(
    thinking_config=types.ThinkingConfig(thinking_budget=2048)
)

# Periodization: メソ/マイクロサイクル設計は最も推論負荷が高いタスク
_THINKING_PLAN = types.GenerateContentConfig(
    thinking_config=types.ThinkingConfig(thinking_budget=4096)
)

# Readiness: HRV/睡眠データの多変数解釈に中程度の推論
_THINKING_MEDIUM = types.GenerateContentConfig(
    thinking_config=types.ThinkingConfig(thinking_budget=1024)
)

# ── Sub-agents ─────────────────────────────────────────────────────────────

periodization = Agent(
    name="periodization",
    model=PLANNER,
    instruction=PERIODIZATION,
    tools=[get_athlete_state],
    generate_content_config=_THINKING_PLAN,
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
    model=LITE,
    instruction=EVALUATOR,
)

# ── Head Coach (Supervisor) ────────────────────────────────────────────────

head_coach = Agent(
    name="ayumu_head_coach",
    model=PLANNER,
    instruction=HEAD_COACH_INSTRUCTION,
    generate_content_config=_THINKING_HEAD,
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
