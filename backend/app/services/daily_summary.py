"""Summarize a single day's meals + workouts against the user's health goal.

Reuses the shared structured-output plumbing (:mod:`app.services.ai_client`).
Calorie figures are *rough estimates* from free-text meal/exercise descriptions —
the prompt is explicit about that. Days with nothing logged short-circuit to a
``no_data`` summary without spending an API call.
"""

from datetime import date

from app.models.exercise_log import ExerciseLog
from app.models.health_goal import HealthGoal
from app.models.meal_log import MealLog
from app.schemas.health_ai import DailySummary
from app.services.ai_client import active_model, generate_structured

# Strict output contract — every field required, assessment constrained to the
# three allowed values, target nullable so the model doesn't invent one.
_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "calories_in",
        "calories_out",
        "target_calories",
        "assessment",
        "headline",
        "tip",
        "narrative",
    ],
    "properties": {
        "calories_in": {
            "type": "integer",
            "minimum": 0,
            "description": "Estimated total kcal eaten, from the meals.",
        },
        "calories_out": {
            "type": "integer",
            "minimum": 0,
            "description": "Estimated kcal burned by the logged exercises (0 if none).",
        },
        "target_calories": {
            "type": ["integer", "null"],
            "description": (
                "A rough daily calorie target for the user's goal, or null when "
                "there is no goal/profile to base one on."
            ),
        },
        "assessment": {
            "type": "string",
            "enum": ["on_track", "off_track", "no_data"],
            "description": (
                "on_track if the day aligns with the goal; off_track if it clearly "
                "works against it; no_data if there is no goal or too little to judge."
            ),
        },
        "headline": {
            "type": "string",
            "description": "One short sentence (<=140 chars) on the day vs the goal.",
        },
        "tip": {
            "type": "string",
            "description": (
                "One short, concrete, actionable suggestion (<=140 chars); empty "
                "string if there is nothing useful to add."
            ),
        },
        "narrative": {
            "type": "string",
            "description": (
                "A short, friendly prose summary of the day in 2-4 sentences, "
                "written in second person ('You had...'). Mention what they ate "
                "and how they moved, and how it felt relative to the goal. This is "
                "the editable summary the user sees, so make it read naturally — "
                "not a list, not clinical."
            ),
        },
    },
}

_SYSTEM_PROMPT = """\
You are a concise, encouraging nutrition and fitness coach. Given one day's \
logged meals and workouts plus the user's goal, produce a SHORT structured \
summary of how the day went relative to that goal.

Estimate calories:
- calories_in: sum a reasonable kcal estimate for each meal from its food name, \
amount/portion note, and typical serving sizes. Be pragmatic, not precise.
- calories_out: estimate kcal burned from the exercises (name + note like \
duration or sets), scaled by the user's body weight when known. 0 if none.
- target_calories: a rough daily calorie target that fits the goal type and \
profile (weight, height, activity, timeframe). Use null if there is no goal or \
not enough profile to ground a number.

Assess:
- on_track: the day broadly supports the goal (e.g. a modest deficit for weight \
loss, enough protein/calories for muscle gain).
- off_track: the day clearly works against the goal.
- no_data: no goal is set, or there is too little logged to judge on-track-ness.

headline: one short sentence. tip: one concrete, doable suggestion (or empty \
string). Keep both under ~140 characters, warm and specific, never preachy. \
narrative: 2-4 natural sentences recapping the day (what they ate, how they \
moved, how it went vs the goal) in second person — this is the summary the user \
reads and edits, so make it flow. These are rough estimates from free text — \
never imply clinical precision. Respond only via the provided JSON schema.\
"""


def format_goal(goal: HealthGoal | None) -> str:
    """One-line goal + profile description for the prompt, or 'No goal set.'.

    Public because the goal evaluator reuses it — the shared way to render a
    :class:`~app.models.health_goal.HealthGoal` into prompt text.
    """
    if goal is None:
        return "No goal set."
    parts = [f"type {goal.goal_type}"]
    if goal.current_weight_kg is not None:
        parts.append(f"current weight {goal.current_weight_kg}kg")
    if goal.target_weight_kg is not None:
        parts.append(f"target weight {goal.target_weight_kg}kg")
    if goal.height_cm is not None:
        parts.append(f"height {goal.height_cm}cm")
    if goal.activity_level:
        parts.append(f"activity {goal.activity_level}")
    if goal.timeframe_weeks is not None:
        parts.append(f"over {goal.timeframe_weeks} weeks")
    if goal.note:
        parts.append(f"note: {goal.note}")
    return "; ".join(parts)


def _build_user_message(
    *,
    on_date: date,
    goal: HealthGoal | None,
    meals: list[MealLog],
    exercises: list[ExerciseLog],
) -> str:
    lines = [f"Date: {on_date.isoformat()}", f"Goal: {format_goal(goal)}", "", "Meals:"]
    if meals:
        for m in meals:
            portion = f" ({m.note})" if m.note else ""
            lines.append(f"- {m.slot}: {m.food_name}{portion}")
    else:
        lines.append("- (none logged)")
    lines.append("")
    lines.append("Workouts:")
    if exercises:
        for e in exercises:
            detail = f" ({e.note})" if e.note else ""
            lines.append(f"- {e.name}{detail}")
    else:
        lines.append("- (none logged)")
    return "\n".join(lines)


async def summarize_day(
    *,
    on_date: date,
    goal: HealthGoal | None,
    meals: list[MealLog],
    exercises: list[ExerciseLog],
) -> tuple[DailySummary, str]:
    """Summarize the day vs the goal, with the model that produced it.

    Short-circuits to a ``no_data`` summary (no API call) when nothing is logged.
    Raises :class:`~app.services.ai_client.AINotConfiguredError` /
    :class:`~app.services.ai_client.AIError` on provider problems.
    """
    if not meals and not exercises:
        return (
            DailySummary(
                calories_in=0,
                calories_out=0,
                target_calories=None,
                assessment="no_data",
                headline="Nothing logged for this day yet.",
                tip="Log a meal or a workout to get a summary.",
                narrative=(
                    "Nothing's logged for this day yet — add a meal or a workout "
                    "and I can summarize it for you."
                ),
            ),
            active_model(),
        )

    return await generate_structured(
        system=_SYSTEM_PROMPT,
        user_message=_build_user_message(
            on_date=on_date, goal=goal, meals=meals, exercises=exercises
        ),
        anthropic_schema=_SCHEMA,
        response_model=DailySummary,
    )
