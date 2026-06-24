"""
Agent Orchestrator
Connects to all three MCP servers and runs the docket analysis loop.
Uses the MCP client library to talk to each server over stdio.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta
from typing import Any, Optional

import anthropic
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from src.models.models import Docket, OpportunityType


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MODEL = "claude-sonnet-4-6"

# MCP server entry points
DOCKET_MONITOR_SERVER = StdioServerParameters(
    command="python",
    args=["-m", "src.mcp_servers.docket_monitor.server"],
)
CLIENT_INTEL_SERVER = StdioServerParameters(
    command="python",
    args=["-m", "src.mcp_servers.client_intel.server"],
)
NOTIFICATIONS_SERVER = StdioServerParameters(
    command="python",
    args=["-m", "src.mcp_servers.notifications.server"],
)

EXTRACTION_PROMPT = """
You are a legal intelligence analyst. You will be given raw docket data from a court filing.

Extract and return a JSON object with these fields:
- party_names: list of all party names (plaintiffs, defendants, etc.)
- industry: the likely industry of the defendant(s) (e.g. "financial services", "technology", "healthcare")
- claim_types: list of claim types or causes of action
- jurisdiction: the court and jurisdiction
- estimated_exposure: "low", "medium", or "high" based on the nature of claims
- practice_area: one of: litigation, corporate, employment, intellectual_property, environmental, tax, real_estate, bankruptcy, other
- summary: a 2-3 sentence plain English summary of what this case is about

Return ONLY valid JSON. No preamble, no markdown fences.

Docket data:
{docket_json}
"""

OPPORTUNITY_PROMPT = """
You are a business development analyst at a law firm.

You have a new court docket and a list of potential client matches from the firm's database.
Conflicts have also been checked.

Docket summary:
{docket_summary}

Client matches (fuzzy-matched party names against client database):
{matches_json}

Conflicts found:
{conflicts_json}

Based on this information:
1. Determine the opportunity type:
   - "new_business": a non-client company is involved in litigation where the firm has expertise
   - "existing_client_alert": an existing client is named in a new case the firm doesn't know about
   - "conflict_flag": a potential conflict of interest exists
   - "not_relevant": no actionable opportunity

2. Write a concise opportunity summary (2-3 sentences) for the attorney.
   If it's a new business opportunity, include a suggested outreach angle.
   If it's a conflict flag, clearly state the nature of the conflict.

3. Identify the best relationship partner to assign this to, based on the matched client's partner
   or "BD Team" if no client match.

Return a JSON object with:
- opportunity_type: one of the four types above
- summary: the opportunity summary
- assigned_to: attorney or team name
- confidence: "low", "medium", or "high"

Return ONLY valid JSON.
"""


# ---------------------------------------------------------------------------
# MCP session helpers
# ---------------------------------------------------------------------------

async def call_mcp_tool(
    session: ClientSession,
    tool_name: str,
    arguments: dict,
) -> Any:
    """Call a tool on an MCP server session and return parsed result."""
    result = await session.call_tool(tool_name, arguments)
    if result.content and result.content[0].type == "text":
        try:
            return json.loads(result.content[0].text)
        except json.JSONDecodeError:
            return result.content[0].text
    return None


# ---------------------------------------------------------------------------
# Claude API helpers
# ---------------------------------------------------------------------------

def extract_entities(docket: Docket) -> dict:
    """Use Claude to extract structured entities from raw docket data."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=MODEL,
        max_tokens=1000,
        messages=[{
            "role": "user",
            "content": EXTRACTION_PROMPT.format(
                docket_json=docket.model_dump_json(indent=2)
            ),
        }],
    )
    raw = response.content[0].text.strip()
    return json.loads(raw)


def classify_opportunity(
    docket_summary: str,
    matches: list[dict],
    conflicts: list[dict],
) -> dict:
    """Use Claude to classify the opportunity type and draft a summary."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=MODEL,
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": OPPORTUNITY_PROMPT.format(
                docket_summary=docket_summary,
                matches_json=json.dumps(matches, indent=2),
                conflicts_json=json.dumps(conflicts, indent=2),
            ),
        }],
    )
    raw = response.content[0].text.strip()
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Core processing loop
# ---------------------------------------------------------------------------

async def process_dockets(
    court: Optional[str] = None,
    date_from: Optional[str] = None,
    dry_run: bool = False,
) -> list[dict]:
    """
    Main entry point. Opens connections to all three MCP servers,
    fetches new dockets, analyzes each one, and routes output.
    Returns a list of processed opportunity records.
    """

    if not date_from:
        date_from = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")

    results = []
    print(f"\n{'='*60}")
    print(f"Docket Intelligence Run — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Court filter : {court or 'all'}")
    print(f"Date from    : {date_from}")
    print(f"Dry run      : {dry_run}")
    print(f"{'='*60}\n")

    async with stdio_client(DOCKET_MONITOR_SERVER) as (dm_read, dm_write):
        async with ClientSession(dm_read, dm_write) as docket_session:
            await docket_session.initialize()

            async with stdio_client(CLIENT_INTEL_SERVER) as (ci_read, ci_write):
                async with ClientSession(ci_read, ci_write) as client_session:
                    await client_session.initialize()

                    async with stdio_client(NOTIFICATIONS_SERVER) as (n_read, n_write):
                        async with ClientSession(n_read, n_write) as notif_session:
                            await notif_session.initialize()

                            # Step 1: Fetch new filings
                            print("Fetching new docket filings...")
                            raw_dockets = await call_mcp_tool(
                                docket_session,
                                "search_new_filings",
                                {"court": court, "date_from": date_from} if court
                                else {"date_from": date_from},
                            )

                            if not raw_dockets:
                                print("No new filings found.")
                                return []

                            print(f"Found {len(raw_dockets)} new filings. Processing...\n")

                            for raw in raw_dockets:
                                docket = Docket.model_validate(raw)
                                result = await _process_single_docket(
                                    docket=docket,
                                    docket_session=docket_session,
                                    client_session=client_session,
                                    notif_session=notif_session,
                                    dry_run=dry_run,
                                )
                                results.append(result)

    print(f"\nProcessing complete. {len(results)} dockets analyzed.")
    _print_summary(results)
    return results


async def _process_single_docket(
    docket: Docket,
    docket_session: ClientSession,
    client_session: ClientSession,
    notif_session: ClientSession,
    dry_run: bool,
) -> dict:
    print(f"  Processing: {docket.case_name} ({docket.case_number})")

    try:
        # Step 2: Extract structured entities with Claude
        print(f"    → Extracting entities...")
        entities = extract_entities(docket)
        party_names = entities.get("party_names", [p.name for p in docket.parties])

        # Step 3: Check conflicts first
        print(f"    → Checking conflicts for {len(party_names)} parties...")
        conflicts = await call_mcp_tool(
            client_session,
            "check_conflicts",
            {"party_names": party_names},
        )

        # Step 4: Find entity matches
        print(f"    → Matching against client database...")
        matches = await call_mcp_tool(
            client_session,
            "find_entity_matches",
            {"party_names": party_names, "threshold": 0.5},
        )

        # Step 5: Classify opportunity
        print(f"    → Classifying opportunity...")
        classification = classify_opportunity(
            docket_summary=entities.get("summary", docket.case_name),
            matches=matches or [],
            conflicts=conflicts or [],
        )

        opp_type = classification.get("opportunity_type", "not_relevant")
        print(f"    → Result: {opp_type} (confidence: {classification.get('confidence')})")

        if opp_type == "not_relevant":
            return {"docket": docket.case_name, "result": "not_relevant"}

        # Step 6: Log the opportunity
        if not dry_run:
            matched_ids = [
                m["client"]["client_id"]
                for m in (matches or [])
                if m.get("client", {}).get("client_id")
            ]
            await call_mcp_tool(
                client_session,
                "log_opportunity",
                {
                    "docket_json": docket.model_dump_json(),
                    "opportunity_type": opp_type,
                    "matched_client_ids": matched_ids,
                    "summary": classification.get("summary", ""),
                    "assigned_to": classification.get("assigned_to", "BD Team"),
                },
            )

        # Step 7: Route notifications
        if not dry_run:
            if opp_type == "conflict_flag":
                await call_mcp_tool(
                    notif_session,
                    "send_teams_card",
                    {
                        "channel": "conflicts",
                        "title": f"⚠️ Conflict Flag: {docket.case_name}",
                        "body": classification.get("summary", ""),
                        "urgency": "high",
                    },
                )
            else:
                channel = "business-development"
                await call_mcp_tool(
                    notif_session,
                    "send_teams_card",
                    {
                        "channel": channel,
                        "title": f"New Opportunity: {docket.case_name}",
                        "body": classification.get("summary", ""),
                        "urgency": "normal",
                    },
                )
                if classification.get("assigned_to") and classification["assigned_to"] != "BD Team":
                    await call_mcp_tool(
                        notif_session,
                        "create_outlook_task",
                        {
                            "assignee": classification["assigned_to"],
                            "title": f"Review opportunity: {docket.case_name}",
                            "details": classification.get("summary", ""),
                            "due_date": (
                                datetime.utcnow() + timedelta(days=3)
                            ).strftime("%Y-%m-%d"),
                        },
                    )

        return {
            "docket": docket.case_name,
            "result": opp_type,
            "summary": classification.get("summary"),
            "assigned_to": classification.get("assigned_to"),
        }

    except Exception as e:
        print(f"    ✗ Error processing {docket.case_name}: {e}")
        return {"docket": docket.case_name, "result": "error", "error": str(e)}


def _print_summary(results: list[dict]) -> None:
    from collections import Counter
    counts = Counter(r.get("result") for r in results)
    print("\nSummary:")
    for result_type, count in sorted(counts.items()):
        print(f"  {result_type:<30} {count}")


# ---------------------------------------------------------------------------
# Entry point for direct invocation
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the docket intelligence agent")
    parser.add_argument("--court", help="CourtListener court ID to filter by")
    parser.add_argument("--date-from", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to DB or send notifications")
    args = parser.parse_args()

    asyncio.run(process_dockets(
        court=args.court,
        date_from=args.date_from,
        dry_run=args.dry_run,
    ))
