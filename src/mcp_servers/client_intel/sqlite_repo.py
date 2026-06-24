"""
SQLite-backed repository for local development.
This is a drop-in replacement for the SharePoint/Graph adapter
that will be used in production. The interface is identical.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.models.models import (
    Client,
    Conflict,
    ConflictCheck,
    EntityMatch,
    Opportunity,
    OpportunityStatus,
    OpportunityType,
    Party,
    PracticeArea,
)


DB_PATH = Path(__file__).parent.parent.parent / "data" / "local.db"


@contextmanager
def get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Create tables if they don't exist. Safe to call repeatedly."""
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS clients (
                client_id    TEXT PRIMARY KEY,
                name         TEXT NOT NULL,
                aliases      TEXT DEFAULT '[]',
                industry     TEXT,
                practice_areas TEXT DEFAULT '[]',
                jurisdictions  TEXT DEFAULT '[]',
                relationship_partner TEXT,
                notes        TEXT
            );

            CREATE TABLE IF NOT EXISTS opportunities (
                opportunity_id TEXT PRIMARY KEY,
                docket_json    TEXT NOT NULL,
                opportunity_type TEXT NOT NULL,
                matched_clients_json TEXT DEFAULT '[]',
                conflicts_json TEXT DEFAULT '[]',
                status         TEXT DEFAULT 'new',
                summary        TEXT,
                created_at     TEXT NOT NULL,
                assigned_to    TEXT,
                logged_by      TEXT,
                updated_by     TEXT,
                updated_at     TEXT
            );

            CREATE TABLE IF NOT EXISTS conflict_checks (
                check_id        TEXT PRIMARY KEY,
                run_at          TEXT NOT NULL,
                caller_id       TEXT NOT NULL,
                parties_queried TEXT NOT NULL,
                results         TEXT NOT NULL,
                result_summary  TEXT NOT NULL,
                action_taken    TEXT,
                signed_off_by   TEXT,
                signed_off_at   TEXT
            );
        """)
        _migrate_db(conn)


def _migrate_db(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the initial schema to existing databases."""
    migrations = [
        "ALTER TABLE opportunities ADD COLUMN logged_by TEXT",
        "ALTER TABLE opportunities ADD COLUMN updated_by TEXT",
        "ALTER TABLE opportunities ADD COLUMN updated_at TEXT",
    ]
    for sql in migrations:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass  # column already exists


# ---------------------------------------------------------------------------
# Client operations
# ---------------------------------------------------------------------------

def _row_to_client(row: sqlite3.Row) -> Client:
    return Client(
        client_id=row["client_id"],
        name=row["name"],
        aliases=json.loads(row["aliases"]),
        industry=row["industry"],
        practice_areas=[PracticeArea(p) for p in json.loads(row["practice_areas"])],
        jurisdictions=json.loads(row["jurisdictions"]),
        relationship_partner=row["relationship_partner"],
        notes=row["notes"],
    )


def search_clients(
    query: str = "",
    practice_area: Optional[str] = None,
    jurisdiction: Optional[str] = None,
) -> list[Client]:
    with get_conn() as conn:
        sql = "SELECT * FROM clients WHERE 1=1"
        params: list = []

        if query:
            sql += " AND (name LIKE ? OR aliases LIKE ? OR notes LIKE ?)"
            q = f"%{query}%"
            params.extend([q, q, q])

        if practice_area:
            sql += " AND practice_areas LIKE ?"
            params.append(f"%{practice_area}%")

        if jurisdiction:
            sql += " AND jurisdictions LIKE ?"
            params.append(f"%{jurisdiction}%")

        rows = conn.execute(sql, params).fetchall()
        return [_row_to_client(r) for r in rows]


def get_client(client_id: str) -> Optional[Client]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM clients WHERE client_id = ?", (client_id,)
        ).fetchone()
        return _row_to_client(row) if row else None


def upsert_client(client: Client) -> Client:
    if not client.client_id:
        client.client_id = str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO clients VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(client_id) DO UPDATE SET
                name=excluded.name,
                aliases=excluded.aliases,
                industry=excluded.industry,
                practice_areas=excluded.practice_areas,
                jurisdictions=excluded.jurisdictions,
                relationship_partner=excluded.relationship_partner,
                notes=excluded.notes
        """, (
            client.client_id,
            client.name,
            json.dumps(client.aliases),
            client.industry,
            json.dumps([p.value for p in client.practice_areas]),
            json.dumps(client.jurisdictions),
            client.relationship_partner,
            client.notes,
        ))
    return client


def find_entity_matches(
    party_names: list[str],
    threshold: float = 0.5,
) -> list[EntityMatch]:
    """
    Naive fuzzy matching for local dev.
    Production will use Azure Cognitive Search or embeddings.
    """
    matches: list[EntityMatch] = []
    all_clients = search_clients()

    for party_name in party_names:
        party_name_lower = party_name.lower()
        for client in all_clients:
            candidates = [client.name] + client.aliases
            for candidate in candidates:
                if _name_similarity(party_name_lower, candidate.lower()) >= threshold:
                    matches.append(EntityMatch(
                        client=client,
                        matched_party=Party(name=party_name, role="unknown"),
                        confidence=_name_similarity(party_name_lower, candidate.lower()),
                        match_reason=f"Name similarity match: '{party_name}' ~ '{candidate}'",
                    ))
                    break  # one match per client is enough

    return matches


def check_conflicts(
    party_names: list[str],
    caller_id: str = "unknown",
) -> tuple[list[Conflict], str]:
    """
    Returns (conflicts, check_id). Always writes an audit record regardless of
    whether conflicts are found — the CLEAR result is as legally significant as
    CONFLICT_FOUND. The check_id should be included in the response so attorneys
    can use sign_off_conflict_check() to record their review action.
    """
    conflicts: list[Conflict] = []
    for party_name in party_names:
        matches = find_entity_matches([party_name], threshold=0.7)
        for match in matches:
            conflicts.append(Conflict(
                client=match.client,
                adverse_party=match.matched_party,
                conflict_description=(
                    f"Party '{party_name}' may match existing client "
                    f"'{match.client.name}' (confidence: {match.confidence:.0%}). "
                    "Manual conflict check required."
                ),
            ))

    check_id = _write_conflict_audit(caller_id, party_names, conflicts)
    return conflicts, check_id


def _write_conflict_audit(
    caller_id: str,
    parties_queried: list[str],
    conflicts: list[Conflict],
) -> str:
    check_id = str(uuid.uuid4())
    result_summary = "CONFLICT_FOUND" if conflicts else "CLEAR"
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO conflict_checks
              (check_id, run_at, caller_id, parties_queried, results, result_summary)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                check_id,
                datetime.utcnow().isoformat(),
                caller_id,
                json.dumps(parties_queried),
                json.dumps([c.model_dump() for c in conflicts], default=str),
                result_summary,
            ),
        )
    return check_id


def sign_off_conflict_check(
    check_id: str,
    action_taken: str,
    signed_off_by: str,
) -> bool:
    """Record attorney sign-off on a conflict check. Returns False if check_id not found."""
    with get_conn() as conn:
        result = conn.execute(
            """
            UPDATE conflict_checks
            SET action_taken = ?, signed_off_by = ?, signed_off_at = ?
            WHERE check_id = ?
            """,
            (action_taken, signed_off_by, datetime.utcnow().isoformat(), check_id),
        )
        return result.rowcount > 0


def _name_similarity(a: str, b: str) -> float:
    """
    Simple token overlap similarity. Good enough for local dev.
    Replace with embeddings in production.
    """
    tokens_a = set(a.split())
    tokens_b = set(b.split())
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)


# ---------------------------------------------------------------------------
# Opportunity operations
# ---------------------------------------------------------------------------

def log_opportunity(opportunity: Opportunity, caller_id: str = "unknown") -> Opportunity:
    if not opportunity.opportunity_id:
        opportunity.opportunity_id = str(uuid.uuid4())

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO opportunities
              (opportunity_id, docket_json, opportunity_type, matched_clients_json,
               conflicts_json, status, summary, created_at, assigned_to, logged_by)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(opportunity_id) DO NOTHING
            """,
            (
                opportunity.opportunity_id,
                opportunity.docket.model_dump_json(),
                opportunity.opportunity_type.value,
                json.dumps([m.model_dump() for m in opportunity.matched_clients]),
                json.dumps([c.model_dump() for c in opportunity.conflicts]),
                opportunity.status.value,
                opportunity.summary,
                opportunity.created_at.isoformat(),
                opportunity.assigned_to,
                caller_id,
            ),
        )
    return opportunity


def update_opportunity_status(
    opportunity_id: str,
    status: OpportunityStatus,
    notes: Optional[str] = None,
    caller_id: str = "unknown",
) -> bool:
    with get_conn() as conn:
        result = conn.execute(
            """
            UPDATE opportunities
            SET status = ?, summary = COALESCE(?, summary),
                updated_by = ?, updated_at = ?
            WHERE opportunity_id = ?
            """,
            (status.value, notes, caller_id, datetime.utcnow().isoformat(), opportunity_id),
        )
        return result.rowcount > 0


def list_opportunities(
    status: Optional[OpportunityStatus] = None,
    opportunity_type: Optional[OpportunityType] = None,
) -> list[Opportunity]:
    with get_conn() as conn:
        sql = "SELECT * FROM opportunities WHERE 1=1"
        params: list = []
        if status:
            sql += " AND status = ?"
            params.append(status.value)
        if opportunity_type:
            sql += " AND opportunity_type = ?"
            params.append(opportunity_type.value)

        rows = conn.execute(sql, params).fetchall()
        results = []
        for row in rows:
            from src.models.models import Docket
            opp = Opportunity(
                opportunity_id=row["opportunity_id"],
                docket=Docket.model_validate_json(row["docket_json"]),
                opportunity_type=OpportunityType(row["opportunity_type"]),
                status=OpportunityStatus(row["status"]),
                summary=row["summary"],
                created_at=datetime.fromisoformat(row["created_at"]),
                assigned_to=row["assigned_to"],
            )
            results.append(opp)
        return results
