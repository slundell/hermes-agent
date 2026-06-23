"""Guardrail: the background-review prompts must fence investigation/case
material OUT of skill reference files.

Root cause of the cross-session contamination (proven from request dumps
2026-06-13): the review prompt invited "domain notes you found while
working" into `references/<topic>.md`. For an investigation agent, that
deposited case subjects (people, places, per-subject search results) into
the always-preloaded `research-methodology` skill, which then primed
unrelated sessions when the model loaded the references via skill_view.

Skill references must be GENERALIZABLE methodology/technique only;
case-specific findings route to the vault / fact-store. This test ensures
that constraint survives future prompt edits — it is the WRITE-side half of
the fix (the READ-side was the one-time reference cleanup).
"""

from agent.background_review import (
    _SKILL_REVIEW_PROMPT,
    _COMBINED_REVIEW_PROMPT,
)

# A stable marker the guard clause must contain. Kept generic so reworded
# guidance still satisfies it as long as the constraint is present.
MARKER = "case material"


def test_skill_review_prompt_fences_case_material():
    low = _SKILL_REVIEW_PROMPT.lower()
    assert MARKER in low, (
        "skill-review prompt lost the case-material guard — references/ will "
        "re-accumulate investigation subjects that contaminate unrelated sessions"
    )
    # The guard must name the alternative destination so the model has
    # somewhere to put case findings instead of references/.
    assert "vault" in low or "fact" in low


def test_combined_review_prompt_fences_case_material():
    low = _COMBINED_REVIEW_PROMPT.lower()
    assert MARKER in low, (
        "combined-review prompt lost the case-material guard"
    )
    assert "vault" in low or "fact" in low
