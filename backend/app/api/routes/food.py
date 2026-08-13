from fastapi import APIRouter, status
from sqlalchemy import func, select

from app.api.ai_errors import ai_errors_as_http
from app.api.ai_quota import (
    QUOTA_EXCEEDED_RESPONSE,
    enforce_ai_quota,
    record_ai_usage,
)
from app.api.crud import apply_validated_patch, get_owned_or_404
from app.api.deps import UNAUTHORIZED_RESPONSE, CurrentUser, DbSession
from app.api.responses import not_found_response
from app.models.food import FoodItem
from app.models.meal_log import MealLog
from app.schemas.food import (
    FoodActivity,
    FoodItemBase,
    FoodItemCreate,
    FoodItemRead,
    FoodItemUpdate,
    FrequentFood,
)
from app.schemas.food_ai import NutritionEstimateRequest, NutritionEstimateResponse
from app.services.nutrition_estimation import estimate_nutrition

# Fields that make up a food item's editable body (used to merge partial updates).
_FOOD_FIELDS = (
    "name",
    "recipe_md",
    "ingredients",
    "calories",
    "protein_g",
    "carbs_g",
    "fat_g",
)

# How many recent logs the activity panel shows.
_RECENT_LOG_LIMIT = 5

# How many "log again" shortcuts the log page's rail shows.
_FREQUENT_LIMIT = 8

router = APIRouter(prefix="/food", tags=["food"])

FOOD_NOT_FOUND = "Food not found."

_NOT_FOUND = not_found_response("No such food for this user", FOOD_NOT_FOUND)


async def _get_owned_food(food_id: int, user: CurrentUser, db: DbSession) -> FoodItem:
    """Fetch a food item owned by the current user, or raise 404."""
    return await get_owned_or_404(db, FoodItem, food_id, user.id, FOOD_NOT_FOUND)


@router.get(
    "",
    response_model=list[FoodItemRead],
    summary="List the current user's food items",
    responses={**UNAUTHORIZED_RESPONSE},
)
async def list_food(current_user: CurrentUser, db: DbSession) -> list[FoodItem]:
    """Return all of the user's food items, most-recently updated first."""
    result = await db.scalars(
        select(FoodItem)
        .where(FoodItem.user_id == current_user.id)
        .order_by(FoodItem.updated_at.desc())
    )
    return list(result)


@router.post(
    "",
    response_model=FoodItemRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a food item",
    responses={**UNAUTHORIZED_RESPONSE},
)
async def create_food(
    payload: FoodItemCreate, current_user: CurrentUser, db: DbSession
) -> FoodItem:
    """Create a new food item for the current user."""
    food = FoodItem(user_id=current_user.id, **payload.model_dump())
    db.add(food)
    await db.commit()
    await db.refresh(food)
    return food


@router.post(
    "/estimate-nutrition",
    response_model=NutritionEstimateResponse,
    summary="Estimate per-serving nutrition for a draft food (AI)",
    responses={**UNAUTHORIZED_RESPONSE, **QUOTA_EXCEEDED_RESPONSE},
)
async def estimate_food_nutrition(
    payload: NutritionEstimateRequest, current_user: CurrentUser, db: DbSession
) -> NutritionEstimateResponse:
    """Estimate per-serving calories and macros for a food's draft text.

    Content-in-body (not a saved food id) so it works while a food is still being
    created and reflects the ingredients being typed right now. Nothing is
    written — the client fills the numbers into the editor and the user saves (or
    corrects) them. ``current_user`` gates the AI cost behind auth and the quota.
    """
    enforce_ai_quota(current_user)
    with ai_errors_as_http("Could not estimate nutrition right now. Please try again."):
        estimate, model = await estimate_nutrition(
            name=payload.name, ingredients=payload.ingredients
        )
    await record_ai_usage(current_user, db)

    return NutritionEstimateResponse(model=model, **estimate.model_dump())


@router.get(
    "/frequent",
    response_model=list[FrequentFood],
    summary="The user's most-logged foods with their usual slot (log-again rail)",
    responses={**UNAUTHORIZED_RESPONSE},
)
async def list_frequent_food(
    current_user: CurrentUser, db: DbSession
) -> list[FrequentFood]:
    """Return the user's most-logged foods for the log page's "one tap again" rail.

    Each entry carries the food's live name, how many times it's been logged, and
    the slot it's most often eaten in (used as the default when the user taps it).
    Joining ``FoodItem`` drops logs whose food was deleted (the FK is nulled), so
    every shortcut still points at a real, re-loggable food. Ordered by log count
    (busiest first) and capped at ``_FREQUENT_LIMIT``.
    """
    # Count per (food, slot) in one pass; fold into per-food totals + top slot.
    per_food_slot = (
        await db.execute(
            select(
                MealLog.food_id,
                FoodItem.name,
                MealLog.slot,
                func.count().label("n"),
            )
            .join(FoodItem, FoodItem.id == MealLog.food_id)
            .where(FoodItem.user_id == current_user.id)
            .group_by(MealLog.food_id, FoodItem.name, MealLog.slot)
        )
    ).all()

    # food_id -> {"name", "count", "top_slot", "_top_n"}; the busiest slot wins.
    by_food: dict[int, dict] = {}
    for row in per_food_slot:
        agg = by_food.setdefault(
            row.food_id,
            {"name": row.name, "count": 0, "top_slot": row.slot, "_top_n": 0},
        )
        agg["count"] += row.n
        if row.n > agg["_top_n"]:
            agg["_top_n"] = row.n
            agg["top_slot"] = row.slot

    ranked = sorted(
        by_food.items(),
        key=lambda item: (-item[1]["count"], item[1]["name"].lower()),
    )
    return [
        FrequentFood(
            food_id=food_id,
            name=agg["name"],
            count=agg["count"],
            top_slot=agg["top_slot"],
        )
        for food_id, agg in ranked[:_FREQUENT_LIMIT]
    ]


@router.get(
    "/{food_id}",
    response_model=FoodItemRead,
    summary="Get a single food item",
    responses={**UNAUTHORIZED_RESPONSE, **_NOT_FOUND},
)
async def get_food(food_id: int, current_user: CurrentUser, db: DbSession) -> FoodItem:
    """Return a single food item owned by the current user."""
    return await _get_owned_food(food_id, current_user, db)


@router.get(
    "/{food_id}/activity",
    response_model=FoodActivity,
    summary="How a food item has been logged (count, top slot, recent logs)",
    responses={**UNAUTHORIZED_RESPONSE, **_NOT_FOUND},
)
async def get_food_activity(
    food_id: int, current_user: CurrentUser, db: DbSession
) -> FoodActivity:
    """Summarize a food's meal-log history for the reader's activity panel.

    Returns the total number of times it's been logged, its most-used slot, and
    the most recent handful of logs (newest first).
    """
    await _get_owned_food(food_id, current_user, db)

    # Count per slot in one pass: total is the sum, top slot is the busiest.
    per_slot = (
        await db.execute(
            select(MealLog.slot, func.count().label("n"))
            .where(MealLog.user_id == current_user.id, MealLog.food_id == food_id)
            .group_by(MealLog.slot)
            .order_by(func.count().desc())
        )
    ).all()
    count = sum(row.n for row in per_slot)
    top_slot = per_slot[0].slot if per_slot else None

    recent = list(
        await db.scalars(
            select(MealLog)
            .where(MealLog.user_id == current_user.id, MealLog.food_id == food_id)
            .order_by(MealLog.log_date.desc(), MealLog.created_at.desc())
            .limit(_RECENT_LOG_LIMIT)
        )
    )

    return FoodActivity(count=count, top_slot=top_slot, recent=recent)


@router.patch(
    "/{food_id}",
    response_model=FoodItemRead,
    summary="Update a food item (partial)",
    responses={**UNAUTHORIZED_RESPONSE, **_NOT_FOUND},
)
async def update_food(
    food_id: int, payload: FoodItemUpdate, current_user: CurrentUser, db: DbSession
) -> FoodItem:
    """Apply a partial update to a food item.

    Only the fields present in the request change. The patch is merged onto the
    current values and re-validated through ``FoodItemBase`` so ingredient
    cleaning and the count cap always hold; the validated ingredients are written
    back as plain dicts (the JSONB column stores objects, not Pydantic models).
    """
    food = await _get_owned_food(food_id, current_user, db)

    if not apply_validated_patch(
        food, payload, schema=FoodItemBase, fields=_FOOD_FIELDS, fallback_msg="Invalid food update"
    ):
        return food

    await db.commit()
    await db.refresh(food)
    return food


@router.delete(
    "/{food_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a food item",
    responses={**UNAUTHORIZED_RESPONSE, **_NOT_FOUND},
)
async def delete_food(food_id: int, current_user: CurrentUser, db: DbSession) -> None:
    """Permanently delete a food item owned by the current user."""
    food = await _get_owned_food(food_id, current_user, db)
    await db.delete(food)
    await db.commit()
