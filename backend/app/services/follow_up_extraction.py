"""Extract actionable follow-ups from a note using the Anthropic API.

This is the AI feature that bridges journaling and reminders: it reads a note
and *proposes* reminders the writer implicitly committed to. Nothing is created
here — the caller shows the proposals and the user accepts the ones they want.

It is written to demonstrate the CCAF "Prompt Engineering & Structured Output"
patterns end-to-end:

- **Structured output via a strict JSON schema** (Domain 4.3): the response is
  constrained to ``_SCHEMA`` server-side, so there are no JSON syntax errors to
  parse around.
- **A nullable field to prevent hallucination** (Domain 4.3): ``remind_at`` is
  ``["string", "null"]`` so the model returns ``null`` instead of inventing a
  time when the entry gives none.
- **Explicit criteria over vague instructions** (Domain 4.1) and **few-shot
  examples for ambiguous cases** (Domain 4.2): see ``_SYSTEM_PROMPT``.
- **A validation-retry loop** (Domain 4.4): the schema kills syntax errors, but
  if the result still fails our Pydantic model we retry once with the error fed
  back — and stop retrying rather than loop forever.
- **Confidence for human-review routing** (Domain 5.5): each item self-reports
  confidence, used by the UI as a hint — never as an auto-accept gate.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from app.schemas.note_ai import FollowUp, FollowUpExtraction
from app.services.ai_client import AIResult, active_model, count_words, generate_structured

# Below this many words (title + body) a note is too thin to plausibly contain a
# follow-up. We skip the model entirely — no cost, and it avoids feeding nonsense
# to the extractor. Mirrors the frontend's MIN_FOLLOW_UP_WORDS gate.
_MIN_CONTENT_WORDS = 6


@dataclass
class FollowUpResult(AIResult):
    """Proposed follow-ups; ``model``/``used_model`` come from :class:`AIResult`."""

    follow_ups: list[FollowUp]


# The strict output contract. `additionalProperties: false` + `required` on
# every field is what makes the output guaranteed-shaped; `remind_at` is
# nullable so "no time stated" is representable without fabrication.
_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["follow_ups"],
    "properties": {
        "follow_ups": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "remind_at", "kind", "confidence", "reason"],
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Short imperative phrase, <=120 chars, e.g. 'Send Sarah the proposal'.",
                    },
                    "remind_at": {
                        "type": ["string", "null"],
                        "format": "date-time",
                        "description": (
                            "ISO-8601 with a UTC offset (ending in Z). null when the entry "
                            "states or implies no time — never invent one."
                        ),
                    },
                    "kind": {"type": "string", "enum": ["task", "event", "unclear"]},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "reason": {
                        "type": "string",
                        "description": "One short sentence justifying the item, <=280 chars.",
                    },
                },
            },
        }
    },
}

_SYSTEM_PROMPT = """\
You extract actionable follow-ups from a single personal note or journal entry \
and return them as reminders the writer can accept.

A follow-up is something the writer committed to do or attend in the FUTURE — a \
task, an errand, a message to send, or an appointment. Signals include: "need \
to", "should", "have to", "must", "remember to", "promised to", "book", "call", \
"email", "by <day>", "next week".

Do NOT extract:
- Things already done or in the past ("sent the report", "met Sarah today").
- Feelings, reflections, opinions, or general musings ("felt tired", "great weather").
- Hypotheticals or vague wishes with no intent to act ("would be nice to travel someday").

When in doubt whether something is actionable, leave it out. Precision matters \
more than recall here — the writer can always add a reminder manually, but a \
wrong or noisy suggestion erodes their trust in the feature.

For each follow-up:
- title: a short imperative phrase (<=120 chars).
- remind_at: resolve relative dates ("Friday", "tomorrow", "next week") against \
the Current time given in the request, and return ISO-8601 with a UTC offset \
(ending in Z). If the entry gives no time and implies none, return null — do \
NOT invent a time.
- kind: "event" if it has a specific time/appointment, "task" for a to-do, \
"unclear" if it is actionable but you cannot tell.
- confidence: "high" for an explicit commitment with a clear action; "medium" \
if the intent is implied; "low" if you are inferring.
- reason: one short sentence explaining why you extracted it.

Return an empty list if nothing is actionable. Respond only via the provided JSON schema.

Examples (assume Current time is 2026-07-29T10:00:00Z, a Wednesday):

Entry: "Met Sarah today, promised to send her the proposal by Friday. Should \
book flights for the Pune trip sometime. Lovely weather."
-> follow_ups:
  - {"title": "Send Sarah the proposal", "remind_at": "2026-07-31T17:00:00Z", "kind": "task", "confidence": "high", "reason": "Explicit promise with a Friday deadline."}
  - {"title": "Book flights for the Pune trip", "remind_at": null, "kind": "task", "confidence": "medium", "reason": "Stated intention but no date given."}
  ("Met Sarah" is in the past and "Lovely weather" is not actionable — both skipped.)

Entry: "Dentist appointment tomorrow at 3pm. Felt anxious about it."
-> follow_ups:
  - {"title": "Dentist appointment", "remind_at": "2026-07-30T15:00:00Z", "kind": "event", "confidence": "high", "reason": "Explicit appointment with a stated time."}
  ("Felt anxious" is a feeling — skipped.)
"""


def _build_user_message(
    *, title: str, body: str, kind: str, entry_date, now: datetime
) -> str:
    """Assemble the request body — the note plus the anchor time for relative dates."""
    lines = [
        f"Current time (UTC): {now.isoformat()}",
        f"Note kind: {kind}",
    ]
    if entry_date is not None:
        lines.append(f"Entry date: {entry_date.isoformat()}")
    lines.append(f"Title: {title}")
    lines.append("Body:")
    lines.append(body)
    return "\n".join(lines)


def _ensure_aware(follow_ups: list[FollowUp]) -> list[FollowUp]:
    """Force any naive ``remind_at`` to UTC so downstream reminders are unambiguous.

    The prompt asks for a UTC offset, but if the model omits one we treat the
    time as UTC rather than reject the whole suggestion.
    """
    for item in follow_ups:
        if item.remind_at is not None and item.remind_at.tzinfo is None:
            item.remind_at = item.remind_at.replace(tzinfo=UTC)
    return follow_ups


async def suggest_follow_ups(
    *,
    title: str,
    body: str,
    kind: str,
    entry_date=None,
    now: datetime | None = None,
) -> FollowUpResult:
    """Propose reminders implied by a note, with the model that produced them.

    Returns ``used_model=False`` (no API call, so the route charges nothing) for a
    note too thin to bother the model with. Raises
    :class:`~app.services.ai_client.AINotConfiguredError` when the provider's key
    is missing/rejected, or :class:`~app.services.ai_client.AIError` when the
    model never returns a valid result.
    """
    # Too little to work with -> nothing to extract; skip the model (and its
    # cost). No provider or key needed for this path.
    if count_words(title, body) < _MIN_CONTENT_WORDS:
        return FollowUpResult(model=active_model(), used_model=False, follow_ups=[])

    now = now or datetime.now(UTC)
    extraction, model = await generate_structured(
        system=_SYSTEM_PROMPT,
        user_message=_build_user_message(
            title=title, body=body, kind=kind, entry_date=entry_date, now=now
        ),
        anthropic_schema=_SCHEMA,
        response_model=FollowUpExtraction,
    )
    return FollowUpResult(
        model=model, used_model=True, follow_ups=_ensure_aware(extraction.follow_ups)
    )
