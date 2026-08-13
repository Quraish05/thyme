"""Suggest topic tags for a note or journal entry using the Anthropic/Gemini API.

The lighter sibling of :mod:`follow_up_extraction`: it reads an entry's text and
*proposes* a handful of topic tags. A tag just fills the note's own ``tags``
field (nothing is created), so the client applies suggestions by tapping — the
model proposes, the user disposes.

It reuses the shared provider plumbing in :mod:`app.services.ai_client` and the
same structured-output + validation-retry discipline as the follow-up extractor
(CCAF Domain 4.3 / 4.4).
"""

from dataclasses import dataclass

from app.schemas.note_ai import TagSuggestion, TagSuggestionExtraction
from app.services.ai_client import AIResult, active_model, count_words, generate_structured

# Below this many words there isn't enough substance to tag meaningfully; skip
# the model (and its cost) and return nothing. Mirrors the frontend gate.
_MIN_CONTENT_WORDS = 4


@dataclass
class TagResult(AIResult):
    """Proposed tags; ``model``/``used_model`` come from :class:`AIResult`."""

    tags: list[TagSuggestion]

# How many tags we ask for at most — enough to capture themes, few enough to
# stay curated rather than noisy.
_MAX_TAGS = 6


# Strict output contract: an array of {tag, reason}, every field required and no
# extras, so the result is guaranteed-shaped with no free-form parsing.
_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["tags"],
    "properties": {
        "tags": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["tag", "reason"],
                "properties": {
                    "tag": {
                        "type": "string",
                        "description": (
                            "A lowercase, hyphenated topic slug of 1-3 words, no '#', "
                            "e.g. 'work', 'mental-health', 'travel-planning'."
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": "One short phrase on why this tag fits, <=140 chars.",
                    },
                },
            },
        }
    },
}

_SYSTEM_PROMPT = f"""\
You read a single personal note or journal entry and propose a small set of \
topic tags that capture what it is ABOUT — the recurring themes, subjects, \
people, projects, or activities. Good tags let the writer group related entries \
together later.

Return at most {_MAX_TAGS} tags, fewer when the entry is short or single-topic. \
Order them most to least central.

Each tag must be:
- a lowercase, hyphenated slug of 1-3 words, with no leading '#' \
(e.g. "work", "mental-health", "gym", "travel-planning").
- a meaningful subject someone would actually filter by — a theme, domain, \
person, place, project, or activity.

Do NOT produce:
- generic filler that describes the medium rather than the content \
("journal", "note", "entry", "today", "thoughts", "misc", "update").
- sentiment-only tags ("happy", "sad") unless the entry is genuinely about \
that emotional state.
- near-duplicates of each other ("work" and "working"); pick one.

Prefer precision over quantity — a wrong or vague tag is worse than a missing \
one. Return an empty list if the entry is too thin to tag. Respond only via the \
provided JSON schema.

Examples:

Entry title "Rough day at the office", body "Sprint planning ran long, argued \
with Priya about scope. Went for a run after to clear my head."
-> tags:
  - {{"tag": "work", "reason": "Centered on a work sprint-planning day."}}
  - {{"tag": "conflict", "reason": "A disagreement with a colleague over scope."}}
  - {{"tag": "running", "reason": "Went for a run to decompress."}}

Entry title "Weekend", body "Nice weather."
-> tags: []  (too thin to tag meaningfully)
"""


def _build_user_message(*, title: str, body: str) -> str:
    """Assemble the request body from the draft the editor sent."""
    return "\n".join([f"Title: {title}", "Body:", body])


async def suggest_tags(*, title: str, body: str) -> TagResult:
    """Propose topic tags for an entry's text, with the model that produced them.

    Returns ``used_model=False`` (no API call, so the route charges nothing) for
    text too thin to tag meaningfully. Raises
    :class:`~app.services.ai_client.AINotConfiguredError` when the provider's key
    is missing/rejected, or :class:`~app.services.ai_client.AIError` when the
    model never returns a valid result.
    """
    # Too little to work with -> nothing to tag; skip the model (and its cost).
    if count_words(title, body) < _MIN_CONTENT_WORDS:
        return TagResult(model=active_model(), used_model=False, tags=[])

    extraction, model = await generate_structured(
        system=_SYSTEM_PROMPT,
        user_message=_build_user_message(title=title, body=body),
        anthropic_schema=_SCHEMA,
        response_model=TagSuggestionExtraction,
    )
    return TagResult(model=model, used_model=True, tags=extraction.tags[:_MAX_TAGS])
