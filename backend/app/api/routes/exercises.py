from datetime import date

from fastapi import APIRouter, status
from sqlalchemy import select

from app.api.crud import get_owned_or_404
from app.api.deps import UNAUTHORIZED_RESPONSE, CurrentUser, DbSession
from app.api.responses import not_found_response
from app.models.exercise_log import ExerciseLog
from app.schemas.exercise_log import (
    ExerciseLogCreate,
    ExerciseLogRead,
    ExerciseLogUpdate,
)

router = APIRouter(prefix="/exercises", tags=["exercises"])

EXERCISE_NOT_FOUND = "Exercise not found."

_NOT_FOUND = not_found_response("No such exercise for this user", EXERCISE_NOT_FOUND)


async def _get_owned_exercise(
    exercise_id: int, user: CurrentUser, db: DbSession
) -> ExerciseLog:
    """Fetch an exercise owned by the current user, or raise 404."""
    return await get_owned_or_404(db, ExerciseLog, exercise_id, user.id, EXERCISE_NOT_FOUND)


@router.get(
    "",
    response_model=list[ExerciseLogRead],
    summary="List exercises logged in a date range",
    responses={**UNAUTHORIZED_RESPONSE},
)
async def list_exercises(
    start: date, end: date, current_user: CurrentUser, db: DbSession
) -> list[ExerciseLog]:
    """Return the user's exercises with ``start <= log_date <= end`` (inclusive)."""
    result = await db.scalars(
        select(ExerciseLog)
        .where(
            ExerciseLog.user_id == current_user.id,
            ExerciseLog.log_date >= start,
            ExerciseLog.log_date <= end,
        )
        .order_by(ExerciseLog.log_date, ExerciseLog.created_at)
    )
    return list(result)


@router.post(
    "",
    response_model=ExerciseLogRead,
    status_code=status.HTTP_201_CREATED,
    summary="Log an exercise for a day",
    responses={**UNAUTHORIZED_RESPONSE},
)
async def create_exercise(
    payload: ExerciseLogCreate, current_user: CurrentUser, db: DbSession
) -> ExerciseLog:
    """Add an exercise to a day."""
    exercise = ExerciseLog(user_id=current_user.id, **payload.model_dump())
    db.add(exercise)
    await db.commit()
    await db.refresh(exercise)
    return exercise


@router.patch(
    "/{exercise_id}",
    response_model=ExerciseLogRead,
    summary="Update a logged exercise",
    responses={**UNAUTHORIZED_RESPONSE, **_NOT_FOUND},
)
async def update_exercise(
    exercise_id: int,
    payload: ExerciseLogUpdate,
    current_user: CurrentUser,
    db: DbSession,
) -> ExerciseLog:
    """Edit an exercise's name or note."""
    exercise = await _get_owned_exercise(exercise_id, current_user, db)

    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(exercise, field, value)

    await db.commit()
    await db.refresh(exercise)
    return exercise


@router.delete(
    "/{exercise_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a logged exercise",
    responses={**UNAUTHORIZED_RESPONSE, **_NOT_FOUND},
)
async def delete_exercise(
    exercise_id: int, current_user: CurrentUser, db: DbSession
) -> None:
    """Remove an exercise from a day."""
    exercise = await _get_owned_exercise(exercise_id, current_user, db)
    await db.delete(exercise)
    await db.commit()
