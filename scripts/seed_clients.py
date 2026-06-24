"""
Seed the local SQLite database with test client data.
Run once before developing: python scripts/seed_clients.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.mcp_servers.client_intel import sqlite_repo as repo
from src.models.models import Client, PracticeArea

TEST_CLIENTS = [
    Client(
        client_id="client-001",
        name="Acme Corporation",
        aliases=["Acme Corp", "Acme Inc", "ACME"],
        industry="manufacturing",
        practice_areas=[PracticeArea.LITIGATION, PracticeArea.EMPLOYMENT],
        jurisdictions=["nysd", "nyed", "njd"],
        relationship_partner="Sarah Chen",
        notes="Long-standing client. Manufacturer of consumer goods.",
    ),
    Client(
        client_id="client-002",
        name="GlobalTech Solutions LLC",
        aliases=["GlobalTech", "Global Tech Solutions"],
        industry="technology",
        practice_areas=[PracticeArea.INTELLECTUAL_PROPERTY, PracticeArea.CORPORATE],
        jurisdictions=["cacd", "cand", "wawd"],
        relationship_partner="James Okafor",
        notes="SaaS company. Active patent portfolio.",
    ),
    Client(
        client_id="client-003",
        name="Riverside Healthcare Partners",
        aliases=["Riverside Healthcare", "RHP"],
        industry="healthcare",
        practice_areas=[PracticeArea.LITIGATION, PracticeArea.CORPORATE],
        jurisdictions=["ilnd", "ilcd"],
        relationship_partner="Maria Santos",
        notes="Hospital group operating in Illinois.",
    ),
    Client(
        client_id="client-004",
        name="Summit Financial Group",
        aliases=["Summit Financial", "SFG Inc"],
        industry="financial services",
        practice_areas=[PracticeArea.CORPORATE, PracticeArea.LITIGATION],
        jurisdictions=["nysd", "ctd"],
        relationship_partner="Sarah Chen",
        notes="Mid-size asset manager.",
    ),
    Client(
        client_id="client-005",
        name="Pacific Realty Holdings",
        aliases=["Pacific Realty", "PRH"],
        industry="real estate",
        practice_areas=[PracticeArea.REAL_ESTATE, PracticeArea.LITIGATION],
        jurisdictions=["cacd", "caed"],
        relationship_partner="James Okafor",
        notes="Commercial real estate developer.",
    ),
]


def main():
    repo.init_db()
    for client in TEST_CLIENTS:
        repo.upsert_client(client)
        print(f"  ✓ Seeded: {client.name}")
    print(f"\nSeeded {len(TEST_CLIENTS)} clients into local database.")
    print(f"Database location: {repo.DB_PATH}")


if __name__ == "__main__":
    main()
