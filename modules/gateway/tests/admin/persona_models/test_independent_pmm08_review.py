import json
from dataclasses import replace

from src.admin.persona_models import catalogue, service
from tests.admin.persona_models.test_retirement_alerts import _preference


async def test_retired_saved_mapping_is_visible_as_warning_in_list_and_explain(db_session, monkeypatch):
    retired = replace(catalogue.PLATFORM_MODEL_CATALOGUE[0], lifecycle="retired")
    monkeypatch.setattr(catalogue, "PLATFORM_MODEL_CATALOGUE", (retired,))
    pref = _preference(model_id=retired.canonical_model_id)
    db_session.add(pref)
    await db_session.commit()
    entries = await service.build_preference_list(db_session, org_id=pref.org_id, principal_kind=pref.principal_kind, principal_id=pref.principal_id)
    entry = next(row for row in entries if row["persona_key"] == pref.persona_key)
    explain = await service.build_explain(
        db_session, org_id=pref.org_id, principal_kind=pref.principal_kind, principal_id=pref.principal_id, persona_key=pref.persona_key
    )
    assert "retired" in json.dumps(entry, default=str).lower(), entry
    assert "retired" in json.dumps(explain, default=str).lower(), explain
