"""Shared CRUD building blocks for the owned-resource routers.

Every per-user resource router (notes, reminders, food, meals, …) needs the same
two things: fetch a row by id but 404 unless it belongs to the caller, and apply a
partial ``PATCH`` by re-validating the merged result through the resource's Base
schema. These lived copy-pasted in each router; centralizing them keeps the
behaviour identical and the routers thin.
"""

from collections.abc import Sequence
from typing import Protocol, TypeVar

from fastapi import HTTPException, status
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession


class _Owned(Protocol):
    """A model row that carries the owning user's id."""

    user_id: int


_Owned_co = TypeVar("_Owned_co", bound=_Owned)


async def get_owned_or_404(
    db: AsyncSession,
    model: type[_Owned_co],
    obj_id: int,
    user_id: int,
    detail: str,
) -> _Owned_co:
    """Fetch ``model`` row ``obj_id`` owned by ``user_id``, or raise 404 ``detail``.

    The single source of truth for "load by id, but a row that doesn't exist and a
    row owned by someone else are indistinguishable to the caller" — both 404, so a
    user can't probe which ids exist.
    """
    obj = await db.get(model, obj_id)
    if obj is None or obj.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)
    return obj


def apply_validated_patch(
    instance: object,
    payload: BaseModel,
    *,
    schema: type[BaseModel],
    fields: Sequence[str],
    fallback_msg: str,
) -> bool:
    """Merge a partial update onto ``instance`` and write back validated values.

    Applies the ``exclude_unset`` fields of ``payload`` on top of ``instance``'s
    current values, re-validates the *merged* whole through ``schema`` (so model
    invariants — e.g. journal-needs-a-date — hold across partial updates), then
    writes the validated values back. Values are written via ``model_dump()`` so
    nested schemas land as the plain dicts a JSONB column stores, not Pydantic
    objects.

    Returns ``False`` when the payload carries no changes (caller can early-return
    untouched); raises 422 with the first validation message (or ``fallback_msg``)
    when the merged result is invalid.
    """
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return False

    merged = {field: getattr(instance, field) for field in fields} | updates
    try:
        validated = schema.model_validate(merged)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors()[0].get("msg", fallback_msg),
        ) from exc

    data = validated.model_dump()
    for field in fields:
        setattr(instance, field, data[field])
    return True
