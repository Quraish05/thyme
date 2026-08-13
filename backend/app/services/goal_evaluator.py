"""Evaluate how a period (today, or the last 7 days) aligns with the health goal.

The Goals dashboard computes its *numbers* deterministically (calorie/protein
tallies from the ``MealLog → FoodItem`` join, alignment from saved daily
summaries) — all free. This service is the one **paid** piece: an on-demand,
quota-charged AI *read* on that data — a score, a prose readout, what's helping vs
hurting, and one adjustment. It mirrors :mod:`app.services.daily_summary`: strict
structured output via :func:`generate_structured`, and a no-data short-circuit
that makes **no** model call (so the route charges no credit).
"""

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.exercise_log import ExerciseLog
from app.models.food import FoodItem
from app.models.health_goal import HealthGoal
from app.models.meal_log import MealLog
from app.models.user import User
from app.schemas.goal_eval import EvalScope, GoalEvaluation
from app.services.ai_client import AIResult, active_model, generate_structured
from app.services.daily_summary import format_goal

_WEEK_DAYS = 7

_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["alignment_score", "verdict", "readout", "helping", "hurting", "adjustment"],
    "properties": {
        "alignment_score": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
            "description": "How well the period aligns with the goal, 0–100.",
        },
        "verdict": {
            "type": "string",
            "description": "A short headline verdict (<=80 chars), e.g. 'On pace, just.'",
        },
        "readout": {
            "type": "string",
            "description": (
                "A short prose read on progress, 2-4 sentences, second person "
                "('You're...'), warm and specific. Ground it in the numbers given."
            ),
        },
        "helping": {
            "type": "array",
            "description": "Up to 3 things working in the goal's favour.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["emoji", "text", "value"],
                "properties": {
                    "emoji": {"type": "string", "description": "One leading emoji."},
                    "text": {"type": "string", "description": "The factor, short phrase."},
                    "value": {"type": "string", "description": "Compact metric, e.g. '90%'."},
                },
            },
        },
        "hurting": {
            "type": "array",
            "description": "Up to 3 things working against the goal.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["emoji", "text", "value"],
                "properties": {
                    "emoji": {"type": "string", "description": "One leading emoji."},
                    "text": {"type": "string", "description": "The factor, short phrase."},
                    "value": {"type": "string", "description": "Compact metric, e.g. '−42 g'."},
                },
            },
        },
        "adjustment": {
            "type": "string",
            "description": (
                "One concrete, doable adjustment to suggest (<=300 chars); never "
                "preachy. Empty string if there's nothing worth changing."
            ),
        },
    },
}

_SYSTEM_PROMPT = """\
You are a concise, encouraging nutrition and fitness coach. Given a user's goal \
and a short window of their logged meals and workouts (with rough per-day calorie \
and protein tallies already computed), judge how well the window aligns with the \
goal and produce a SHORT structured evaluation.

- alignment_score: 0–100, how well the window supports the goal overall.
- verdict: one short headline (e.g. "On pace, just.").
- readout: 2–4 natural sentences, second person, grounded in the tallies you're \
given — what's going well and what the numbers show. Never imply clinical \
precision; these are rough estimates from free text.
- helping / hurting: up to three concrete factors each, every one with a leading \
emoji, a short phrase, and a compact metric value. Omit a list if empty.
- adjustment: ONE concrete, doable change (or empty string). Warm, never preachy.

Respond only via the provided JSON schema.\
"""


@dataclass
class EvalResult(AIResult):
    """The goal evaluation; ``model``/``used_model`` come from :class:`AIResult`."""

    evaluation: GoalEvaluation


def _range_for(scope: EvalScope, today: date) -> tuple[date, date]:
    """Inclusive (start, end) date range for a scope, ending today."""
    if scope == "today":
        return today, today
    return today - timedelta(days=_WEEK_DAYS - 1), today


def _no_data(readout: str) -> EvalResult:
    """A free, no-model evaluation when there's nothing to judge."""
    return EvalResult(
        model=active_model(),
        used_model=False,
        evaluation=GoalEvaluation(
            alignment_score=0,
            verdict="Not enough logged yet.",
            readout=readout,
            helping=[],
            hurting=[],
            adjustment="",
        ),
    )


def _build_user_message(
    *,
    scope: EvalScope,
    start: date,
    end: date,
    goal: HealthGoal,
    meals: list[MealLog],
    exercises: list[ExerciseLog],
    food_by_id: dict[int, FoodItem],
) -> str:
    """Aggregate the goal + per-day tallies + item lists into a plain-text prompt."""
    span = "today" if scope == "today" else f"the 7 days {start.isoformat()} → {end.isoformat()}"
    lines = [f"Goal: {format_goal(goal)}", f"Window: {span}", ""]

    by_day: dict[date, list[MealLog]] = {}
    for m in meals:
        by_day.setdefault(m.log_date, []).append(m)
    ex_by_day: dict[date, list[ExerciseLog]] = {}
    for e in exercises:
        ex_by_day.setdefault(e.log_date, []).append(e)

    for day in sorted(set(by_day) | set(ex_by_day)):
        day_meals = by_day.get(day, [])
        cals = sum(
            (food_by_id[m.food_id].calories or 0)
            for m in day_meals
            if m.food_id is not None and m.food_id in food_by_id
        )
        protein = sum(
            (food_by_id[m.food_id].protein_g or 0)
            for m in day_meals
            if m.food_id is not None and m.food_id in food_by_id
        )
        lines.append(f"{day.isoformat()} — ~{cals} kcal, ~{protein} g protein")
        for m in day_meals:
            portion = f" ({m.note})" if m.note else ""
            lines.append(f"  · {m.slot}: {m.food_name}{portion}")
        for e in ex_by_day.get(day, []):
            detail = f" ({e.note})" if e.note else ""
            lines.append(f"  · workout: {e.name}{detail}")
    return "\n".join(lines)


async def _fetch(
    db: AsyncSession, user_id: int, start: date, end: date
) -> tuple[list[MealLog], list[ExerciseLog]]:
    meals = list(
        await db.scalars(
            select(MealLog).where(
                MealLog.user_id == user_id,
                MealLog.log_date >= start,
                MealLog.log_date <= end,
            )
        )
    )
    exercises = list(
        await db.scalars(
            select(ExerciseLog).where(
                ExerciseLog.user_id == user_id,
                ExerciseLog.log_date >= start,
                ExerciseLog.log_date <= end,
            )
        )
    )
    return meals, exercises


async def evaluate_goal(
    db: AsyncSession, user: User, scope: EvalScope, *, today: date
) -> EvalResult:
    """Evaluate the window against the user's goal; free no-data short-circuit.

    Returns ``used_model=False`` (no API call) when there's no goal or nothing
    logged in the window, so the caller charges no credit.
    """
    goal = await db.scalar(select(HealthGoal).where(HealthGoal.user_id == user.id))
    if goal is None:
        return _no_data("Set a health goal and log a few days, then I can weigh in.")

    start, end = _range_for(scope, today)
    meals, exercises = await _fetch(db, user.id, start, end)
    if not meals and not exercises:
        return _no_data("Nothing logged for this window yet — add some meals or workouts.")

    food_ids = {m.food_id for m in meals if m.food_id is not None}
    food_by_id: dict[int, FoodItem] = {}
    if food_ids:
        foods = await db.scalars(select(FoodItem).where(FoodItem.id.in_(food_ids)))
        food_by_id = {f.id: f for f in foods}

    evaluation, model = await generate_structured(
        system=_SYSTEM_PROMPT,
        user_message=_build_user_message(
            scope=scope,
            start=start,
            end=end,
            goal=goal,
            meals=meals,
            exercises=exercises,
            food_by_id=food_by_id,
        ),
        anthropic_schema=_SCHEMA,
        response_model=GoalEvaluation,
    )
    return EvalResult(model=model, used_model=True, evaluation=evaluation)
