"""Ask my journal — hybrid retrieval + grounded answer (RAG).

The retrieval core is **hybrid**: a dense arm (pgvector cosine over
``note_chunks``) and a lexical arm (the notes' Postgres full-text index, scoped to
journal entries), fused with **Reciprocal Rank Fusion**, then filtered by a
relevance floor and diversified with **MMR** so a cluster of near-duplicate
entries doesn't crowd out the answer. The final excerpts are handed to the model,
which answers *only* from them and reports which it used, giving precise citations.

``retrieve()`` is deliberately endpoint-agnostic (it takes ``db, user, question``
and knows nothing about HTTP) so a future ``search_journal`` chat tool can reuse
it verbatim. Answer generation goes through the shared, provider-agnostic
``generate_structured`` (Anthropic by default); embeddings are local and free.
"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.note import Note
from app.models.note_chunk import NoteChunk
from app.models.user import User
from app.schemas.journal_qa import AskJournalAnswer, Citation
from app.services.ai_client import AIResult, active_model, generate_structured
from app.services.embeddings import embed_query

# Retrieval knobs.
_DENSE_K = 20        # candidates from the dense (vector) arm
_LEXICAL_K = 20      # candidates from the lexical (FTS) arm
_RRF_K = 60          # RRF damping constant (standard default)
_FUSE_CANDIDATES = 10  # top fused notes to pull full context for
_TOP_K = 5           # excerpts finally shown to the model
_MIN_SIMILARITY = 0.15  # cosine-similarity floor; below this a hit is "not relevant"
_MMR_LAMBDA = 0.7    # relevance vs diversity trade-off in MMR


@dataclass
class Retrieved:
    """One retrieved journal excerpt, with the data needed for MMR + citation."""

    note_id: int
    title: str
    entry_date: date | None
    chunk_text: str
    embedding: list[float]
    similarity: float  # cosine similarity of this chunk to the query (1 - distance)


def rrf_fuse(*ranked_lists: list[int], k: int = _RRF_K) -> dict[int, float]:
    """Reciprocal Rank Fusion: combine ranked id lists into fused scores.

    ``score(id) = Σ 1 / (k + rank)`` across every list the id appears in (rank
    1-based). An id found by only one arm still scores; one found by both ranks
    higher. Pure and order-independent — the unit of the retrieval tests.
    """
    scores: dict[int, float] = {}
    for ranked in ranked_lists:
        for rank, note_id in enumerate(ranked):
            scores[note_id] = scores.get(note_id, 0.0) + 1.0 / (k + rank + 1)
    return scores


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def _mmr_select(candidates: list[Retrieved], k: int) -> list[Retrieved]:
    """Maximal Marginal Relevance: greedily pick k relevant-but-diverse excerpts.

    Balances similarity-to-query against similarity-to-already-picked (embeddings
    are unit vectors, so cosine similarity is a dot product), so five near-identical
    "sleep" entries don't all get selected.
    """
    pool = list(candidates)
    selected: list[Retrieved] = []
    while pool and len(selected) < k:
        best_i = 0
        best_score = None
        for i, cand in enumerate(pool):
            redundancy = max((_dot(cand.embedding, s.embedding) for s in selected), default=0.0)
            score = _MMR_LAMBDA * cand.similarity - (1 - _MMR_LAMBDA) * redundancy
            if best_score is None or score > best_score:
                best_score, best_i = score, i
        selected.append(pool.pop(best_i))
    return selected


async def _dense_ranked(db: AsyncSession, user_id: int, qvec: list[float]) -> list[int]:
    """Note ids ranked by best chunk cosine distance (nearest first, deduped)."""
    dist = NoteChunk.embedding.cosine_distance(qvec)
    rows = (
        await db.execute(
            select(NoteChunk.note_id)
            .where(NoteChunk.user_id == user_id)
            .order_by(dist)
            .limit(_DENSE_K)
        )
    ).scalars()
    seen: list[int] = []
    for note_id in rows:
        if note_id not in seen:
            seen.append(note_id)
    return seen


async def _lexical_ranked(db: AsyncSession, user_id: int, question: str) -> list[int]:
    """Journal note ids ranked by full-text relevance (reuses the Ch.9 FTS index)."""
    tsquery = func.websearch_to_tsquery("english", question)
    rank = func.ts_rank_cd(Note.search_vector, tsquery)
    rows = (
        await db.execute(
            select(Note.id)
            .where(
                Note.user_id == user_id,
                Note.kind == "journal",
                Note.search_vector.op("@@")(tsquery),
            )
            .order_by(rank.desc(), Note.updated_at.desc())
            .limit(_LEXICAL_K)
        )
    ).scalars()
    return list(rows)


async def _hydrate(
    db: AsyncSession, note_ids: list[int], qvec: list[float]
) -> dict[int, Retrieved]:
    """For each note id, fetch its most query-relevant chunk + metadata + embedding."""
    if not note_ids:
        return {}
    dist = NoteChunk.embedding.cosine_distance(qvec)
    rows = (
        await db.execute(
            select(
                NoteChunk.note_id,
                NoteChunk.chunk_text,
                NoteChunk.embedding,
                dist.label("dist"),
                Note.title,
                Note.entry_date,
            )
            .join(Note, Note.id == NoteChunk.note_id)
            .where(NoteChunk.note_id.in_(note_ids))
            .order_by(NoteChunk.note_id, dist)
        )
    ).all()

    best: dict[int, Retrieved] = {}
    for note_id, chunk_text, embedding, d, title, entry_date in rows:
        if note_id in best:  # rows are ordered by distance, so first is best
            continue
        best[note_id] = Retrieved(
            note_id=note_id,
            title=title,
            entry_date=entry_date,
            chunk_text=chunk_text,
            embedding=list(embedding),
            similarity=1.0 - float(d),
        )
    return best


async def retrieve(db: AsyncSession, user: User, question: str) -> list[Retrieved]:
    """Hybrid retrieve the most relevant journal excerpts for a question.

    Dense (pgvector) + lexical (FTS) → RRF fusion → relevance floor → MMR diversity.
    Returns up to ``_TOP_K`` excerpts, or ``[]`` when nothing clears the floor.
    """
    qvec = await embed_query(question)
    dense = await _dense_ranked(db, user.id, qvec)
    lexical = await _lexical_ranked(db, user.id, question)
    if not dense and not lexical:
        return []

    fused = rrf_fuse(dense, lexical)
    top_ids = sorted(fused, key=lambda nid: fused[nid], reverse=True)[:_FUSE_CANDIDATES]

    hydrated = await _hydrate(db, top_ids, qvec)
    # Keep fused order. A candidate stays if it's semantically close enough (dense
    # floor) OR it matched the full-text arm — a keyword hit is its own relevance
    # signal, so lexical matches bypass the semantic floor. When neither holds for
    # anything (query unrelated to the whole corpus), we return nothing → no-data.
    lexical_set = set(lexical)
    candidates = [
        hydrated[nid]
        for nid in top_ids
        if nid in hydrated
        and (hydrated[nid].similarity >= _MIN_SIMILARITY or nid in lexical_set)
    ]
    if not candidates:
        return []
    return _mmr_select(candidates, _TOP_K)


# ---- Answer generation -----------------------------------------------------

_ANSWER_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer", "used_note_ids"],
    "properties": {
        "answer": {
            "type": "string",
            "description": (
                "A warm, concise answer to the question, grounded ONLY in the "
                "provided journal excerpts. Refer to entries by their date "
                "naturally (e.g. 'back on June 3rd...'). If the excerpts don't "
                "cover the question, say so plainly instead of guessing."
            ),
        },
        "used_note_ids": {
            "type": "array",
            "items": {"type": "integer"},
            "description": (
                "The id values of the excerpts you actually used to answer. "
                "Empty if the excerpts didn't cover the question."
            ),
        },
    },
}

_SYSTEM_PROMPT = """\
You are the Thyme journal assistant. Answer the user's question about \
their own journal using ONLY the excerpts provided — never invent events, dates, \
or feelings that aren't there. Ground every claim in the excerpts, refer to \
entries by their date so the user can place them, and keep the tone warm and \
concise (a short paragraph). If the excerpts don't actually address the question, \
say so honestly rather than guessing. Report the ids of the excerpts you used in \
used_note_ids. Respond only via the provided JSON schema.\
"""

_NO_ENTRIES = (
    "You don't have any journal entries yet — write a few and I'll be able to "
    "answer questions about your days."
)
_NO_MATCH = (
    "I couldn't find anything in your journal about that. Try rephrasing, or it "
    "may be something you haven't written about yet."
)


def _snippet(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _build_user_message(question: str, excerpts: list[Retrieved]) -> str:
    lines = [f"Question: {question}", "", "Journal excerpts:"]
    for e in excerpts:
        when = e.entry_date.isoformat() if e.entry_date else "undated"
        lines.append(f"\n[id={e.note_id} | {when} | {e.title}]")
        lines.append(e.chunk_text)
    return "\n".join(lines)


@dataclass
class QaResult(AIResult):
    """The grounded answer + citations; ``model``/``used_model`` come from :class:`AIResult`."""

    answer: str
    citations: list[Citation]


async def answer_question(db: AsyncSession, user: User, question: str) -> QaResult:
    """Retrieve, then generate a grounded, cited answer.

    Short-circuits (no model call, so the caller doesn't charge a credit) when the
    user has no journal entries or nothing relevant is found.
    """
    has_any = await db.scalar(
        select(func.count()).select_from(NoteChunk).where(NoteChunk.user_id == user.id)
    )
    if not has_any:
        return QaResult(model=active_model(), used_model=False, answer=_NO_ENTRIES, citations=[])

    excerpts = await retrieve(db, user, question)
    if not excerpts:
        return QaResult(model=active_model(), used_model=False, answer=_NO_MATCH, citations=[])

    result, model = await generate_structured(
        system=_SYSTEM_PROMPT,
        user_message=_build_user_message(question, excerpts),
        anthropic_schema=_ANSWER_SCHEMA,
        response_model=AskJournalAnswer,
    )

    by_id = {e.note_id: e for e in excerpts}
    used = [by_id[nid] for nid in result.used_note_ids if nid in by_id]
    citations = [
        Citation(
            note_id=e.note_id,
            entry_date=e.entry_date,
            title=e.title,
            snippet=_snippet(e.chunk_text),
        )
        for e in used
    ]
    return QaResult(model=model, used_model=True, answer=result.answer, citations=citations)
