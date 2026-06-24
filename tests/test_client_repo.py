"""
Tests for the SQLite client repository and entity matching logic.
Run with: pytest tests/
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import os
import pytest
import tempfile

# Point the DB at a temp file so tests don't touch dev data
os.environ["TESTING"] = "1"
import src.mcp_servers.client_intel.sqlite_repo as repo


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Each test gets its own isolated SQLite database."""
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(repo, "DB_PATH", db_path)
    repo.init_db()
    yield


def _make_client(**kwargs):
    from src.models.models import Client, PracticeArea
    defaults = dict(
        client_id="c1",
        name="Acme Corporation",
        aliases=["Acme Corp"],
        industry="manufacturing",
        practice_areas=[PracticeArea.LITIGATION],
        jurisdictions=["nysd"],
        relationship_partner="Jane Smith",
    )
    defaults.update(kwargs)
    return Client(**defaults)


# ---------------------------------------------------------------------------
# Client CRUD
# ---------------------------------------------------------------------------

def test_upsert_and_retrieve_client():
    client = _make_client()
    repo.upsert_client(client)
    retrieved = repo.get_client("c1")
    assert retrieved is not None
    assert retrieved.name == "Acme Corporation"
    assert "Acme Corp" in retrieved.aliases


def test_search_clients_by_name():
    repo.upsert_client(_make_client(client_id="c1", name="Acme Corporation"))
    repo.upsert_client(_make_client(client_id="c2", name="Beta Industries", aliases=["Beta Inc"]))
    results = repo.search_clients(query="Acme")
    assert len(results) == 1
    assert results[0].name == "Acme Corporation"


def test_search_clients_by_practice_area():
    from src.models.models import PracticeArea
    repo.upsert_client(_make_client(client_id="c1", practice_areas=[PracticeArea.LITIGATION]))
    repo.upsert_client(_make_client(client_id="c2", name="TechCo", practice_areas=[PracticeArea.INTELLECTUAL_PROPERTY]))
    results = repo.search_clients(practice_area="intellectual_property")
    assert len(results) == 1
    assert results[0].name == "TechCo"


def test_get_nonexistent_client_returns_none():
    assert repo.get_client("does-not-exist") is None


# ---------------------------------------------------------------------------
# Entity matching
# ---------------------------------------------------------------------------

def test_exact_name_match():
    repo.upsert_client(_make_client())
    matches = repo.find_entity_matches(["Acme Corporation"], threshold=0.5)
    assert len(matches) == 1
    assert matches[0].confidence == 1.0


def test_alias_match():
    repo.upsert_client(_make_client())
    matches = repo.find_entity_matches(["Acme Corp"], threshold=0.5)
    assert len(matches) == 1


def test_no_match_below_threshold():
    repo.upsert_client(_make_client())
    matches = repo.find_entity_matches(["Totally Unrelated Company"], threshold=0.5)
    assert len(matches) == 0


def test_multiple_parties():
    repo.upsert_client(_make_client(client_id="c1", name="Acme Corporation"))
    repo.upsert_client(_make_client(client_id="c2", name="Beta Industries", aliases=["Beta Inc"]))
    matches = repo.find_entity_matches(["Acme Corporation", "Beta Inc"], threshold=0.5)
    assert len(matches) == 2


# ---------------------------------------------------------------------------
# Conflict checking
# ---------------------------------------------------------------------------

def test_conflict_detected_for_existing_client():
    repo.upsert_client(_make_client())
    conflicts, check_id = repo.check_conflicts(["Acme Corporation"])
    assert len(conflicts) == 1
    assert "Acme Corporation" in conflicts[0].conflict_description
    assert check_id  # audit record was written


def test_no_conflict_for_unknown_party():
    repo.upsert_client(_make_client())
    conflicts, check_id = repo.check_conflicts(["Some Random LLC"])
    assert len(conflicts) == 0
    assert check_id  # CLEAR result still produces an audit record


# ---------------------------------------------------------------------------
# Opportunity logging
# ---------------------------------------------------------------------------

def test_log_and_retrieve_opportunity():
    from datetime import datetime
    from src.models.models import Docket, Opportunity, OpportunityType, OpportunityStatus

    docket = Docket(
        case_id="d1",
        case_name="Plaintiff v. Defendant",
        case_number="1:24-cv-00001",
        court="nysd",
        jurisdiction="nysd",
        date_filed=datetime(2024, 1, 15),
        source="courtlistener",
    )
    opp = Opportunity(
        docket=docket,
        opportunity_type=OpportunityType.NEW_BUSINESS,
        summary="Potential new client in manufacturing sector.",
    )
    saved = repo.log_opportunity(opp)
    assert saved.opportunity_id is not None

    all_opps = repo.list_opportunities()
    assert len(all_opps) == 1
    assert all_opps[0].opportunity_type == OpportunityType.NEW_BUSINESS


def test_update_opportunity_status():
    from datetime import datetime
    from src.models.models import Docket, Opportunity, OpportunityType, OpportunityStatus

    docket = Docket(
        case_id="d2", case_name="X v. Y", case_number="1:24-cv-00002",
        court="nysd", jurisdiction="nysd", date_filed=datetime(2024, 1, 15),
        source="courtlistener",
    )
    opp = repo.log_opportunity(Opportunity(
        docket=docket,
        opportunity_type=OpportunityType.NEW_BUSINESS,
    ))

    success = repo.update_opportunity_status(
        opp.opportunity_id,
        OpportunityStatus.PURSUING,
        notes="Attorney reached out.",
    )
    assert success is True

    opps = repo.list_opportunities(status=OpportunityStatus.PURSUING)
    assert len(opps) == 1
