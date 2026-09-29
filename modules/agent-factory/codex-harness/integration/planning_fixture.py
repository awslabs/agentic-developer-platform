"""Bounded model output fixtures for the shared planning contract."""


def planning_output(persona, source="instructions"):
    artifact = {
        "summary": "Fixture planning report.",
        "requirements": [{"id": "evidence", "text": "Inspect supplied evidence", "source_refs": [source]}],
        "assumptions": [], "open_questions": [], "superseded_requirements": [],
    }
    artifact.update({
        "architect": {"design": "Reuse the existing report host", "stories": [], "publish_stories": False},
        "product": {"acceptance_criteria": ["Cite supplied evidence"]},
        "pm": {"schedule": []},
        "intent-refinement": {"draft": {"intent": "Inspect supplied evidence"}},
    }[persona])
    return {"artifact": artifact, "clarification": None}
