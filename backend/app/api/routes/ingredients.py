from fastapi import APIRouter, status
from sqlalchemy import select

from app.api.crud import get_owned_or_404
from app.api.deps import UNAUTHORIZED_RESPONSE, CurrentUser, DbSession
from app.api.responses import not_found_response
from app.models.ingredient import Ingredient
from app.schemas.ingredient import IngredientCreate, IngredientRead, IngredientUpdate

router = APIRouter(prefix="/ingredients", tags=["ingredients"])

INGREDIENT_NOT_FOUND = "Ingredient not found."

_NOT_FOUND = not_found_response("No such ingredient for this user", INGREDIENT_NOT_FOUND)


async def _get_owned_ingredient(
    ingredient_id: int, user: CurrentUser, db: DbSession
) -> Ingredient:
    """Fetch an ingredient owned by the current user, or raise 404."""
    return await get_owned_or_404(db, Ingredient, ingredient_id, user.id, INGREDIENT_NOT_FOUND)


@router.get(
    "",
    response_model=list[IngredientRead],
    summary="List the current user's pantry ingredients",
    responses={**UNAUTHORIZED_RESPONSE},
)
async def list_ingredients(current_user: CurrentUser, db: DbSession) -> list[Ingredient]:
    """Return all of the user's ingredients, alphabetically."""
    result = await db.scalars(
        select(Ingredient)
        .where(Ingredient.user_id == current_user.id)
        .order_by(Ingredient.name)
    )
    return list(result)


@router.post(
    "",
    response_model=IngredientRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a pantry ingredient",
    responses={**UNAUTHORIZED_RESPONSE},
)
async def create_ingredient(
    payload: IngredientCreate, current_user: CurrentUser, db: DbSession
) -> Ingredient:
    """File a new ingredient in the current user's pantry."""
    ingredient = Ingredient(user_id=current_user.id, **payload.model_dump())
    db.add(ingredient)
    await db.commit()
    await db.refresh(ingredient)
    return ingredient


@router.patch(
    "/{ingredient_id}",
    response_model=IngredientRead,
    summary="Update an ingredient (partial)",
    responses={**UNAUTHORIZED_RESPONSE, **_NOT_FOUND},
)
async def update_ingredient(
    ingredient_id: int,
    payload: IngredientUpdate,
    current_user: CurrentUser,
    db: DbSession,
) -> Ingredient:
    """Apply a partial update to an ingredient."""
    ingredient = await _get_owned_ingredient(ingredient_id, current_user, db)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(ingredient, field, value)
    await db.commit()
    await db.refresh(ingredient)
    return ingredient


@router.delete(
    "/{ingredient_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an ingredient",
    responses={**UNAUTHORIZED_RESPONSE, **_NOT_FOUND},
)
async def delete_ingredient(
    ingredient_id: int, current_user: CurrentUser, db: DbSession
) -> None:
    """Remove an ingredient from the current user's pantry."""
    ingredient = await _get_owned_ingredient(ingredient_id, current_user, db)
    await db.delete(ingredient)
    await db.commit()
