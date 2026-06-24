"""
Docket Monitor MCP Server
Fetches new court filings from CourtListener (free, open source).
Swappable for Docket Alarm or PACER via config in production.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta
from typing import Optional

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from src.models.models import Docket, Party, PracticeArea

app = Server("docket-monitor")

COURTLISTENER_BASE = "https://www.courtlistener.com/api/rest/v4"
COURTLISTENER_TOKEN = os.getenv("COURTLISTENER_API_TOKEN", "")

# Maps CourtListener nature-of-suit codes to our practice areas
NOS_TO_PRACTICE_AREA: dict[str, PracticeArea] = {
    "110": PracticeArea.CORPORATE,          # Insurance
    "120": PracticeArea.CORPORATE,          # Marine
    "130": PracticeArea.CORPORATE,          # Miller Act
    "140": PracticeArea.CORPORATE,          # Negotiable Instrument
    "150": PracticeArea.LITIGATION,         # Recovery of Overpayment
    "160": PracticeArea.EMPLOYMENT,         # Stockholders' Suits
    "190": PracticeArea.CORPORATE,          # Other Contracts
    "310": PracticeArea.LITIGATION,         # Airplane
    "315": PracticeArea.LITIGATION,         # Airplane Product Liability
    "320": PracticeArea.LITIGATION,         # Assault, Libel & Slander
    "330": PracticeArea.EMPLOYMENT,         # Federal Employers' Liability
    "340": PracticeArea.LITIGATION,         # Marine
    "410": PracticeArea.CORPORATE,          # Antitrust
    "430": PracticeArea.CORPORATE,          # Banks & Banking
    "440": PracticeArea.EMPLOYMENT,         # Other Civil Rights
    "442": PracticeArea.EMPLOYMENT,         # Employment
    "710": PracticeArea.EMPLOYMENT,         # Fair Labor Standards
    "720": PracticeArea.EMPLOYMENT,         # Labor/Management Relations
    "790": PracticeArea.EMPLOYMENT,         # Other Labor Litigation
    "820": PracticeArea.INTELLECTUAL_PROPERTY,  # Copyrights
    "830": PracticeArea.INTELLECTUAL_PROPERTY,  # Patent
    "840": PracticeArea.INTELLECTUAL_PROPERTY,  # Trademark
    "850": PracticeArea.CORPORATE,          # Securities / Commodities
    "870": PracticeArea.TAX,                # Tax Suits
    "890": PracticeArea.ENVIRONMENTAL,      # Other Statutory Actions
    "893": PracticeArea.ENVIRONMENTAL,      # Environmental Matters
    "895": PracticeArea.CORPORATE,          # Freedom of Information
}


def _headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if COURTLISTENER_TOKEN:
        headers["Authorization"] = f"Token {COURTLISTENER_TOKEN}"
    return headers


def _parse_docket(data: dict) -> Docket:
    """Convert a CourtListener docket API response into our Docket model."""

    # Extract parties from nested structure
    parties: list[Party] = []
    for p in data.get("parties", []):
        parties.append(Party(
            name=p.get("name", "Unknown"),
            role=p.get("party_types", [{}])[0].get("name", "party")
            if p.get("party_types") else "party",
        ))

    # Determine practice area from nature of suit code
    nos_code = str(data.get("nature_of_suit", ""))
    practice_area = NOS_TO_PRACTICE_AREA.get(nos_code, PracticeArea.LITIGATION)

    # Build the case name
    case_name = data.get("case_name") or data.get("case_name_short") or "Unknown v. Unknown"

    return Docket(
        case_id=str(data.get("id", "")),
        case_name=case_name,
        case_number=data.get("docket_number", ""),
        court=data.get("court_id", ""),
        jurisdiction=data.get("court_id", ""),
        date_filed=datetime.fromisoformat(
            data["date_filed"].replace("Z", "+00:00")
        ) if data.get("date_filed") else datetime.utcnow(),
        practice_area=practice_area,
        parties=parties,
        claims=[data.get("cause", "")] if data.get("cause") else [],
        source="courtlistener",
        source_url=f"https://www.courtlistener.com{data.get('absolute_url', '')}",
        raw_text=json.dumps(data),
    )


async def _fetch_dockets(
    court: Optional[str] = None,
    date_from: Optional[str] = None,
    keywords: Optional[str] = None,
    page_size: int = 20,
) -> list[Docket]:
    params: dict = {"format": "json", "page_size": page_size, "order_by": "-date_filed"}

    if court:
        params["court"] = court
    if date_from:
        params["date_filed__gte"] = date_from
    if keywords:
        params["q"] = keywords

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{COURTLISTENER_BASE}/dockets/",
            headers=_headers(),
            params=params,
        )
        response.raise_for_status()
        data = response.json()

    return [_parse_docket(d) for d in data.get("results", [])]


async def _fetch_case_details(case_id: str) -> Optional[Docket]:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{COURTLISTENER_BASE}/dockets/{case_id}/",
            headers=_headers(),
            params={"format": "json"},
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return _parse_docket(response.json())


async def _search_by_party(
    name: str,
    jurisdiction: Optional[str] = None,
    date_from: Optional[str] = None,
) -> list[Docket]:
    params: dict = {
        "format": "json",
        "page_size": 20,
        "party_name": name,
        "order_by": "-date_filed",
    }
    if jurisdiction:
        params["court"] = jurisdiction
    if date_from:
        params["date_filed__gte"] = date_from

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{COURTLISTENER_BASE}/dockets/",
            headers=_headers(),
            params=params,
        )
        response.raise_for_status()
        data = response.json()

    return [_parse_docket(d) for d in data.get("results", [])]


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search_new_filings",
            description=(
                "Search for new court docket filings. Can filter by court, "
                "date range, and keyword. Returns a list of dockets."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "court": {
                        "type": "string",
                        "description": (
                            "CourtListener court ID, e.g. 'nysd' (S.D.N.Y.), "
                            "'cacd' (C.D. Cal.), 'txnd' (N.D. Tex.). "
                            "Omit to search all federal courts."
                        ),
                    },
                    "date_from": {
                        "type": "string",
                        "description": "Earliest filing date in YYYY-MM-DD format. Defaults to yesterday.",
                    },
                    "keywords": {
                        "type": "string",
                        "description": "Full-text keyword search across docket text.",
                    },
                    "page_size": {
                        "type": "integer",
                        "description": "Number of results to return (max 100). Default 20.",
                        "default": 20,
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="get_case_details",
            description="Retrieve full details for a specific case by its CourtListener ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "case_id": {
                        "type": "string",
                        "description": "The CourtListener docket ID.",
                    },
                },
                "required": ["case_id"],
            },
        ),
        Tool(
            name="search_by_party_name",
            description=(
                "Search for dockets where a specific company or person is named as a party. "
                "Useful for monitoring whether a known entity has been sued or has filed suit."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The party name to search for.",
                    },
                    "jurisdiction": {
                        "type": "string",
                        "description": "Optional court ID to limit results.",
                    },
                    "date_from": {
                        "type": "string",
                        "description": "Earliest filing date in YYYY-MM-DD format.",
                    },
                },
                "required": ["name"],
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:

    if name == "search_new_filings":
        date_from = arguments.get(
            "date_from",
            (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d"),
        )
        dockets = await _fetch_dockets(
            court=arguments.get("court"),
            date_from=date_from,
            keywords=arguments.get("keywords"),
            page_size=arguments.get("page_size", 20),
        )
        return [TextContent(
            type="text",
            text=json.dumps(
                [d.model_dump() for d in dockets],
                default=str,
                indent=2,
            ),
        )]

    elif name == "get_case_details":
        docket = await _fetch_case_details(arguments["case_id"])
        if not docket:
            return [TextContent(type="text", text=json.dumps({"error": "Case not found"}))]
        return [TextContent(type="text", text=docket.model_dump_json(indent=2))]

    elif name == "search_by_party_name":
        dockets = await _search_by_party(
            name=arguments["name"],
            jurisdiction=arguments.get("jurisdiction"),
            date_from=arguments.get("date_from"),
        )
        return [TextContent(
            type="text",
            text=json.dumps(
                [d.model_dump() for d in dockets],
                default=str,
                indent=2,
            ),
        )]

    return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
