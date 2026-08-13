from fastapi import APIRouter, status
from sqlalchemy import select

from app.api.ai_errors import ai_errors_as_http
from app.api.ai_quota import QUOTA_EXCEEDED_RESPONSE, enforce_ai_quota, record_ai_usage
from app.api.crud import get_owned_or_404
from app.api.deps import UNAUTHORIZED_RESPONSE, CurrentUser, DbSession
from app.api.responses import not_found_response
from app.models.journal_insight import JournalInsight
from app.schemas.journal_insight import (
    JournalInsightCreate,
    JournalInsightRead,
    JournalInsightVote,
)
from app.schemas.journal_qa import AskJournalRequest, AskJournalResponse
from app.services.journal_qa import answer_question

router = APIRouter(prefix="/journal", tags=["journal"])

INSIGHT_NOT_FOUND = "Saved insight not found."

_INSIGHT_NOT_FOUND = not_found_response("No such insight for this user", INSIGHT_NOT_FOUND)


@router.post(
    "/ask",
    response_model=AskJournalResponse,
    summary="Ask a question about your journal (RAG)",
    responses={**UNAUTHORIZED_RESPONSE, **QUOTA_EXCEEDED_RESPONSE},
)
async def ask_journal(
    payload: AskJournalRequest, current_user: CurrentUser, db: DbSession
) -> AskJournalResponse:
    """Answer a natural-language question grounded in the user's journal entries.

    Hybrid retrieval (semantic + full-text, fused via RRF) finds the relevant
    entries; the model answers only from them and cites the ones it used. Quota is
    enforced up front, but a credit is charged **only when the model actually
    runs** — a no-data answer (empty journal, or nothing relevant) is free.
    """
    enforce_ai_quota(current_user)
    with ai_errors_as_http("Could not answer that right now. Please try again."):
        result = await answer_question(db, current_user, payload.question)
    if result.used_model:
        await record_ai_usage(current_user, db)
    return AskJournalResponse(
        answer=result.answer, citations=result.citations, model=result.model
    )


# ---- Saved insights (Patterns) ---------------------------------------------
# Plain CRUD over answers the user chose to keep. Saving persists an
# already-generated answer, so there's no model call — no quota, no credit.


async def _get_owned_insight(
    insight_id: int, user: CurrentUser, db: DbSession
) -> JournalInsight:
    """Fetch a saved insight owned by the current user, or raise 404."""
    return await get_owned_or_404(db, JournalInsight, insight_id, user.id, INSIGHT_NOT_FOUND)


@router.post(
    "/insights",
    response_model=JournalInsightRead,
    status_code=status.HTTP_201_CREATED,
    summary="Save an answer as a Patterns finding",
    responses={**UNAUTHORIZED_RESPONSE},
)
async def save_insight(
    payload: JournalInsightCreate, current_user: CurrentUser, db: DbSession
) -> JournalInsight:
    """Persist a generated answer + its citations snapshot for the Patterns page."""
    insight = JournalInsight(
        user_id=current_user.id,
        question=payload.question,
        answer=payload.answer,
        citations=[c.model_dump(mode="json") for c in payload.citations],
        model=payload.model,
    )
    db.add(insight)
    await db.commit()
    await db.refresh(insight)
    return insight


@router.get(
    "/insights",
    response_model=list[JournalInsightRead],
    summary="List saved Patterns findings (newest first)",
    responses={**UNAUTHORIZED_RESPONSE},
)
async def list_insights(
    current_user: CurrentUser, db: DbSession
) -> list[JournalInsight]:
    """Every saved insight for the user, newest first."""
    result = await db.scalars(
        select(JournalInsight)
        .where(JournalInsight.user_id == current_user.id)
        .order_by(JournalInsight.created_at.desc(), JournalInsight.id.desc())
    )
    return list(result)


@router.patch(
    "/insights/{insight_id}",
    response_model=JournalInsightRead,
    summary="Record the 'was this true for you?' vote",
    responses={**UNAUTHORIZED_RESPONSE, **_INSIGHT_NOT_FOUND},
)
async def vote_insight(
    insight_id: int,
    payload: JournalInsightVote,
    current_user: CurrentUser,
    db: DbSession,
) -> JournalInsight:
    """Set (or clear, with null) the helpful vote on a saved insight."""
    insight = await _get_owned_insight(insight_id, current_user, db)
    insight.helpful = payload.helpful
    await db.commit()
    await db.refresh(insight)
    return insight


@router.delete(
    "/insights/{insight_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a saved Patterns finding",
    responses={**UNAUTHORIZED_RESPONSE, **_INSIGHT_NOT_FOUND},
)
async def delete_insight(
    insight_id: int, current_user: CurrentUser, db: DbSession
) -> None:
    """Remove a saved insight the user owns."""
    insight = await _get_owned_insight(insight_id, current_user, db)
    await db.delete(insight)
    await db.commit()
