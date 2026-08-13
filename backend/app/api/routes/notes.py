from fastapi import APIRouter, Query, status
from sqlalchemy import select

from app.api.ai_errors import ai_errors_as_http
from app.api.ai_quota import (
    QUOTA_EXCEEDED_RESPONSE,
    enforce_ai_quota,
    record_ai_usage,
)
from app.api.crud import apply_validated_patch, get_owned_or_404
from app.api.deps import UNAUTHORIZED_RESPONSE, CurrentUser, DbSession
from app.api.responses import not_found_response
from app.models.note import Note
from app.schemas.note import NoteBase, NoteCreate, NoteRead, NoteSearchHit, NoteUpdate
from app.schemas.note_ai import (
    FollowUpSuggestionsResponse,
    TagSuggestionRequest,
    TagSuggestionsResponse,
)
from app.services.follow_up_extraction import suggest_follow_ups
from app.services.journal_index import reindex_note_safe
from app.services.note_search import search_notes
from app.services.tag_suggestion import suggest_tags

# Fields that make up a note's editable body (used to merge partial updates).
_NOTE_FIELDS = (
    "kind", "title", "body_md", "entry_date", "tags", "folder", "items", "mood", "pinned",
)

router = APIRouter(prefix="/notes", tags=["notes"])

NOTE_NOT_FOUND = "Note not found."

_NOT_FOUND = not_found_response("No such note for this user", NOTE_NOT_FOUND)


async def _get_owned_note(note_id: int, user: CurrentUser, db: DbSession) -> Note:
    """Fetch a note owned by the current user, or raise 404."""
    return await get_owned_or_404(db, Note, note_id, user.id, NOTE_NOT_FOUND)


@router.get(
    "",
    response_model=list[NoteRead],
    summary="List the current user's notes",
    responses={**UNAUTHORIZED_RESPONSE},
)
async def list_notes(current_user: CurrentUser, db: DbSession) -> list[Note]:
    """Return all of the user's notes, pinned first then most-recently updated."""
    result = await db.scalars(
        select(Note)
        .where(Note.user_id == current_user.id)
        .order_by(Note.pinned.desc(), Note.updated_at.desc())
    )
    return list(result)


@router.post(
    "",
    response_model=NoteRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a note",
    responses={**UNAUTHORIZED_RESPONSE},
)
async def create_note(payload: NoteCreate, current_user: CurrentUser, db: DbSession) -> Note:
    """Create a new note or journal entry for the current user."""
    note = Note(user_id=current_user.id, **payload.model_dump())
    db.add(note)
    await db.commit()
    await db.refresh(note)
    # Keep the journal RAG index fresh (best-effort; a journal-only no-op otherwise).
    await reindex_note_safe(db, note)
    return note


@router.get(
    "/search",
    response_model=list[NoteSearchHit],
    summary="Full-text search the current user's notes",
    responses={**UNAUTHORIZED_RESPONSE},
)
async def search_notes_route(
    current_user: CurrentUser,
    db: DbSession,
    q: str = Query(..., min_length=1, description="Search query (supports quotes, or, -exclude)"),
    limit: int = Query(20, ge=1, le=100),
) -> list[NoteSearchHit]:
    """Ranked full-text search over the user's notes.

    Declared *before* ``/{note_id}`` so "search" isn't parsed as an id.
    """
    rows = await search_notes(db, current_user.id, q, limit)
    return [
        NoteSearchHit(
            **NoteRead.model_validate(note).model_dump(),
            rank=float(rank),
            snippet=snippet,
        )
        for note, rank, snippet in rows
    ]


@router.get(
    "/{note_id}",
    response_model=NoteRead,
    summary="Get a single note",
    responses={**UNAUTHORIZED_RESPONSE, **_NOT_FOUND},
)
async def get_note(note_id: int, current_user: CurrentUser, db: DbSession) -> Note:
    """Return a single note owned by the current user."""
    return await _get_owned_note(note_id, current_user, db)


@router.patch(
    "/{note_id}",
    response_model=NoteRead,
    summary="Update a note (partial)",
    responses={**UNAUTHORIZED_RESPONSE, **_NOT_FOUND},
)
async def update_note(
    note_id: int, payload: NoteUpdate, current_user: CurrentUser, db: DbSession
) -> Note:
    """Apply a partial update to a note.

    Only the fields present in the request change; this also covers pinning —
    send just ``{"pinned": true}`` / ``{"pinned": false}``. The patch is merged
    onto the current values and re-validated so invariants (journal-needs-a-date,
    notes-carry-no-date/mood) and tag normalization always hold; SQLAlchemy then
    writes only the columns that actually changed.
    """
    note = await _get_owned_note(note_id, current_user, db)

    if not apply_validated_patch(
        note, payload, schema=NoteBase, fields=_NOTE_FIELDS, fallback_msg="Invalid note update"
    ):
        return note

    await db.commit()
    await db.refresh(note)
    # Re-embed the entry so edits are reflected in journal search (best-effort).
    await reindex_note_safe(db, note)
    return note


@router.post(
    "/{note_id}/follow-up-suggestions",
    response_model=FollowUpSuggestionsResponse,
    summary="Suggest reminders (follow-ups) implied by a note",
    responses={**UNAUTHORIZED_RESPONSE, **QUOTA_EXCEEDED_RESPONSE, **_NOT_FOUND},
)
async def suggest_note_follow_ups(
    note_id: int, current_user: CurrentUser, db: DbSession
) -> FollowUpSuggestionsResponse:
    """Read a note and propose reminders the writer implied.

    Nothing is created here — this only *proposes*. The client shows the
    suggestions and the user accepts the ones they want; each accepted one
    becomes a reminder via ``POST /reminders`` with ``target_type="note"`` so it
    links back to this note (human-in-the-loop, CCAF Domain 5.5).
    """
    note = await _get_owned_note(note_id, current_user, db)
    enforce_ai_quota(current_user)
    with ai_errors_as_http("Could not extract follow-ups right now. Please try again."):
        result = await suggest_follow_ups(
            title=note.title,
            body=note.body_md,
            kind=note.kind,
            entry_date=note.entry_date,
        )
    # Charge only when the model actually ran; a note too thin to extract from
    # short-circuits without an API call (see suggest_follow_ups).
    if result.used_model:
        await record_ai_usage(current_user, db)

    return FollowUpSuggestionsResponse(
        note_id=note.id, model=result.model, suggestions=result.follow_ups
    )


@router.post(
    "/tag-suggestions",
    response_model=TagSuggestionsResponse,
    summary="Suggest topic tags for draft note text",
    responses={**UNAUTHORIZED_RESPONSE, **QUOTA_EXCEEDED_RESPONSE},
)
async def suggest_note_tags(
    payload: TagSuggestionRequest, current_user: CurrentUser, db: DbSession
) -> TagSuggestionsResponse:
    """Read the *draft* text of an entry and propose topic tags.

    Content-in-body (not a saved note id) so suggestions reflect what's being
    written right now and work on unsaved entries. Nothing is created — the
    client applies the tags the user taps, then saves the note normally.
    ``current_user`` gates the AI cost behind authentication and the free quota.
    """
    enforce_ai_quota(current_user)
    with ai_errors_as_http("Could not suggest tags right now. Please try again."):
        result = await suggest_tags(title=payload.title, body=payload.body_md)
    # Charge only when the model actually ran; text too thin to tag short-circuits
    # without an API call (see suggest_tags).
    if result.used_model:
        await record_ai_usage(current_user, db)

    return TagSuggestionsResponse(model=result.model, suggestions=result.tags)


@router.delete(
    "/{note_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a note",
    responses={**UNAUTHORIZED_RESPONSE, **_NOT_FOUND},
)
async def delete_note(note_id: int, current_user: CurrentUser, db: DbSession) -> None:
    """Permanently delete a note owned by the current user."""
    note = await _get_owned_note(note_id, current_user, db)
    await db.delete(note)
    await db.commit()
