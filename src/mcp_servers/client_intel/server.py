"""
Client Intelligence MCP Server
Exposes client database operations as MCP tools.
Backed by SQLite locally; swap the repo import for the Graph adapter in production.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from src.logging_config import configure_logging, get_logger, new_request_id
from src.mcp_servers.client_intel import sqlite_repo as repo
from src.models.models import (
    Client,
    Opportunity,
    OpportunityStatus,
    OpportunityType,
    PracticeArea,
)

configure_logging()
log = get_logger("client-intel")

app = Server("client-intel")

_DEFAULT_CALLER_ID = os.getenv("CALLER_ID", "unknown")

_CALLER_ID_SCHEMA = {
    "caller_id": {
        "type": "string",
        "description": "Attorney identifier (e.g. email). Falls back to CALLER_ID env var if omitted.",
    },
}


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search_clients",
            description=(
                "Search the client database by name, practice area, or jurisdiction. "
                "Returns a list of matching clients with their details."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Free-text search against client name, aliases, and notes.",
                    },
                    "practice_area": {
                        "type": "string",
                        "enum": [p.value for p in PracticeArea],
                        "description": "Filter by practice area.",
                    },
                    "jurisdiction": {
                        "type": "string",
                        "description": "Filter by jurisdiction, e.g. 'nysd' or 'cacd'.",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="get_client",
            description="Retrieve full details for a specific client by ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "client_id": {"type": "string"},
                },
                "required": ["client_id"],
            },
        ),
        Tool(
            name="find_entity_matches",
            description=(
                "Given a list of party names from a docket, find matching clients "
                "in the database using fuzzy name matching. Returns matches with "
                "confidence scores."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "party_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of party names from the docket filing.",
                    },
                    "threshold": {
                        "type": "number",
                        "description": "Minimum confidence threshold (0.0–1.0). Default 0.5.",
                        "default": 0.5,
                    },
                },
                "required": ["party_names"],
            },
        ),
        Tool(
            name="check_conflicts",
            description=(
                "Check whether any party names in a docket match existing clients, "
                "which would indicate a potential conflict of interest. "
                "Always run this before logging a new business opportunity. "
                "Returns a check_id to be used with sign_off_conflict_check."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "party_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "All party names appearing in the docket.",
                    },
                    **_CALLER_ID_SCHEMA,
                },
                "required": ["party_names"],
            },
        ),
        Tool(
            name="sign_off_conflict_check",
            description=(
                "Record attorney sign-off on a conflict check result. "
                "Stamps the audit record with who reviewed it and what action was taken."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "check_id": {
                        "type": "string",
                        "description": "The check_id returned by check_conflicts.",
                    },
                    "action_taken": {
                        "type": "string",
                        "description": "e.g. 'OPENED_MATTER', 'DECLINED', 'REFERRED_TO_ETHICS'",
                    },
                    **_CALLER_ID_SCHEMA,
                },
                "required": ["check_id", "action_taken"],
            },
        ),
        Tool(
            name="log_opportunity",
            description=(
                "Save a business development opportunity to the opportunity tracker. "
                "Call this after confirming there are no conflicts and the opportunity "
                "is worth flagging."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "docket_json": {
                        "type": "string",
                        "description": "JSON-serialized Docket object.",
                    },
                    "opportunity_type": {
                        "type": "string",
                        "enum": [t.value for t in OpportunityType],
                    },
                    "matched_client_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Client IDs of matched clients.",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Brief human-readable summary of the opportunity.",
                    },
                    "assigned_to": {
                        "type": "string",
                        "description": "Relationship partner to assign this to.",
                    },
                    **_CALLER_ID_SCHEMA,
                },
                "required": ["docket_json", "opportunity_type", "summary"],
            },
        ),
        Tool(
            name="update_opportunity_status",
            description="Update the status of an existing opportunity (e.g. after attorney review).",
            inputSchema={
                "type": "object",
                "properties": {
                    "opportunity_id": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": [s.value for s in OpportunityStatus],
                    },
                    "notes": {
                        "type": "string",
                        "description": "Optional notes to append.",
                    },
                    **_CALLER_ID_SCHEMA,
                },
                "required": ["opportunity_id", "status"],
            },
        ),
        Tool(
            name="list_opportunities",
            description="List logged opportunities, optionally filtered by status or type.",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": [s.value for s in OpportunityStatus],
                    },
                    "opportunity_type": {
                        "type": "string",
                        "enum": [t.value for t in OpportunityType],
                    },
                },
                "required": [],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    repo.init_db()

    rid = new_request_id()
    caller_id = arguments.get("caller_id") or _DEFAULT_CALLER_ID
    log.info("tool_call", tool=name, caller_id=caller_id, request_id=rid)

    if name == "search_clients":
        clients = repo.search_clients(
            query=arguments.get("query", ""),
            practice_area=arguments.get("practice_area"),
            jurisdiction=arguments.get("jurisdiction"),
        )
        return [TextContent(
            type="text",
            text=json.dumps([c.model_dump() for c in clients], default=str, indent=2),
        )]

    elif name == "get_client":
        client = repo.get_client(arguments["client_id"])
        if not client:
            return [TextContent(type="text", text=json.dumps({"error": "Client not found"}))]
        return [TextContent(type="text", text=client.model_dump_json(indent=2))]

    elif name == "find_entity_matches":
        matches = repo.find_entity_matches(
            party_names=arguments["party_names"],
            threshold=arguments.get("threshold", 0.5),
        )
        return [TextContent(
            type="text",
            text=json.dumps([m.model_dump() for m in matches], default=str, indent=2),
        )]

    elif name == "check_conflicts":
        conflicts, check_id = repo.check_conflicts(arguments["party_names"], caller_id=caller_id)
        return [TextContent(
            type="text",
            text=json.dumps({
                "check_id": check_id,
                "result_summary": "CONFLICT_FOUND" if conflicts else "CLEAR",
                "conflicts": [c.model_dump() for c in conflicts],
            }, default=str, indent=2),
        )]

    elif name == "sign_off_conflict_check":
        success = repo.sign_off_conflict_check(
            check_id=arguments["check_id"],
            action_taken=arguments["action_taken"],
            signed_off_by=caller_id,
        )
        return [TextContent(type="text", text=json.dumps({"success": success}))]

    elif name == "log_opportunity":
        from src.models.models import Docket, EntityMatch, Party
        docket = Docket.model_validate_json(arguments["docket_json"])

        matched_clients: list[EntityMatch] = []
        for cid in arguments.get("matched_client_ids", []):
            client = repo.get_client(cid)
            if client:
                matched_clients.append(EntityMatch(
                    client=client,
                    matched_party=Party(name=client.name, role="unknown"),
                    confidence=1.0,
                    match_reason="Explicitly provided by agent",
                ))

        opp = Opportunity(
            docket=docket,
            opportunity_type=OpportunityType(arguments["opportunity_type"]),
            matched_clients=matched_clients,
            summary=arguments.get("summary"),
            assigned_to=arguments.get("assigned_to"),
        )
        saved = repo.log_opportunity(opp, caller_id=caller_id)
        return [TextContent(type="text", text=saved.model_dump_json(indent=2))]

    elif name == "update_opportunity_status":
        success = repo.update_opportunity_status(
            opportunity_id=arguments["opportunity_id"],
            status=OpportunityStatus(arguments["status"]),
            notes=arguments.get("notes"),
            caller_id=caller_id,
        )
        return [TextContent(type="text", text=json.dumps({"success": success}))]

    elif name == "list_opportunities":
        opps = repo.list_opportunities(
            status=OpportunityStatus(arguments["status"]) if "status" in arguments else None,
            opportunity_type=OpportunityType(arguments["opportunity_type"]) if "opportunity_type" in arguments else None,
        )
        return [TextContent(
            type="text",
            text=json.dumps([o.model_dump() for o in opps], default=str, indent=2),
        )]

    return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]


async def main():
    repo.init_db()
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
