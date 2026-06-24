"""
CourtListener Wrapper MCP Server

Proxies the official CourtListener MCP server (mcp.courtlistener.com) and
layers on this project's conflict-checking and opportunity-management tools.
The orchestrator connects to this one server instead of managing three separate
ones for court data, conflict checking, and client intel.

Startup behaviour:
  - If COURTLISTENER_API_TOKEN is set, connects to the official CL MCP server
    via SSE and proxies all its tools transparently.
  - Falls back to the project's direct CourtListener REST API calls if the
    upstream MCP server is unavailable.
  - Conflict-checking and opportunity tools are always available regardless of
    the upstream connection state.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from contextlib import AsyncExitStack
from datetime import datetime, timedelta
from typing import Any, Optional

import httpx
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from src.logging_config import configure_logging, get_logger, new_request_id, request_id
from src.mcp_servers.client_intel import sqlite_repo as repo
from src.models.models import (
    Docket,
    Opportunity,
    OpportunityStatus,
    OpportunityType,
    Party,
    PracticeArea,
)

configure_logging()
log = get_logger("courtlistener-wrapper")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

app = Server("courtlistener-wrapper")

CL_MCP_URL = os.getenv("COURTLISTENER_MCP_URL", "https://mcp.courtlistener.com/sse")
CL_TOKEN = os.getenv("COURTLISTENER_API_TOKEN", "")
CL_REST_BASE = "https://www.courtlistener.com/api/rest/v4"
_DEFAULT_CALLER_ID = os.getenv("CALLER_ID", "unknown")

# CourtListener nature-of-suit → practice area mapping
NOS_TO_PRACTICE_AREA: dict[str, PracticeArea] = {
    "110": PracticeArea.CORPORATE, "120": PracticeArea.CORPORATE,
    "130": PracticeArea.CORPORATE, "140": PracticeArea.CORPORATE,
    "150": PracticeArea.LITIGATION, "160": PracticeArea.EMPLOYMENT,
    "190": PracticeArea.CORPORATE, "310": PracticeArea.LITIGATION,
    "315": PracticeArea.LITIGATION, "320": PracticeArea.LITIGATION,
    "330": PracticeArea.EMPLOYMENT, "340": PracticeArea.LITIGATION,
    "410": PracticeArea.CORPORATE, "430": PracticeArea.CORPORATE,
    "440": PracticeArea.EMPLOYMENT, "442": PracticeArea.EMPLOYMENT,
    "710": PracticeArea.EMPLOYMENT, "720": PracticeArea.EMPLOYMENT,
    "790": PracticeArea.EMPLOYMENT,
    "820": PracticeArea.INTELLECTUAL_PROPERTY,
    "830": PracticeArea.INTELLECTUAL_PROPERTY,
    "840": PracticeArea.INTELLECTUAL_PROPERTY,
    "850": PracticeArea.CORPORATE, "870": PracticeArea.TAX,
    "890": PracticeArea.ENVIRONMENTAL, "893": PracticeArea.ENVIRONMENTAL,
    "895": PracticeArea.CORPORATE,
}

# ---------------------------------------------------------------------------
# Module-level state — populated at startup if CL MCP is reachable
# ---------------------------------------------------------------------------

_cl_session: Optional[ClientSession] = None
_proxied_cl_tools: list[Tool] = []
_proxied_cl_names: set[str] = set()


# ---------------------------------------------------------------------------
# CourtListener REST API helpers
# ---------------------------------------------------------------------------

def _cl_headers() -> dict:
    h = {"Content-Type": "application/json"}
    if CL_TOKEN:
        h["Authorization"] = f"Token {CL_TOKEN}"
    return h


def _parse_docket(data: dict) -> Docket:
    parties: list[Party] = []
    for p in data.get("parties", []):
        parties.append(Party(
            name=p.get("name", "Unknown"),
            role=(
                p.get("party_types", [{}])[0].get("name", "party")
                if p.get("party_types") else "party"
            ),
        ))
    nos_code = str(data.get("nature_of_suit", ""))
    practice_area = NOS_TO_PRACTICE_AREA.get(nos_code, PracticeArea.LITIGATION)
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


def _parse_search_result(data: dict) -> Docket:
    case_name = data.get("caseName") or data.get("case_name") or "Unknown v. Unknown"
    date_filed_raw = data.get("dateFiled") or data.get("date_filed")
    try:
        date_filed = datetime.fromisoformat(date_filed_raw.replace("Z", "+00:00")) if date_filed_raw else datetime.utcnow()
    except (ValueError, AttributeError):
        date_filed = datetime.utcnow()

    parties: list[Party] = []
    for name in data.get("party_names", []):
        parties.append(Party(name=name, role="party"))

    return Docket(
        case_id=str(data.get("docket_id") or data.get("id", "")),
        case_name=case_name,
        case_number=data.get("docketNumber") or data.get("docket_number", ""),
        court=data.get("court_id", ""),
        jurisdiction=data.get("court_id", ""),
        date_filed=date_filed,
        parties=parties,
        claims=[data.get("suitNature", "")] if data.get("suitNature") else [],
        source="courtlistener",
        source_url=f"https://www.courtlistener.com{data.get('absolute_url', '')}",
        raw_text=json.dumps(data),
    )


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return False


_CL_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=5.0)


@retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    reraise=True,
)
async def _rest_search_dockets(
    court: Optional[str] = None,
    date_from: Optional[str] = None,
    keywords: Optional[str] = None,
    page_size: int = 20,
) -> list[Docket]:
    params: dict = {"type": "r", "format": "json", "page_size": page_size}
    if court:
        params["court"] = court
    if date_from:
        params["filed_after"] = date_from
    if keywords:
        params["q"] = keywords

    rid = request_id.get()
    async with httpx.AsyncClient(timeout=_CL_TIMEOUT) as client:
        t0 = time.perf_counter()
        r = await client.get(f"{CL_REST_BASE}/search/", headers=_cl_headers(), params=params)
        duration_ms = int((time.perf_counter() - t0) * 1000)
        log.info(
            "cl_api_call",
            endpoint="search",
            status=r.status_code,
            duration_ms=duration_ms,
            request_id=rid,
        )
        r.raise_for_status()
        return [_parse_search_result(d) for d in r.json().get("results", [])]


@retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    reraise=True,
)
async def _rest_search_by_party(
    name: str,
    jurisdiction: Optional[str] = None,
    date_from: Optional[str] = None,
    page_size: int = 20,
) -> list[Docket]:
    params: dict = {"type": "r", "format": "json", "page_size": page_size, "q": f'"{name}"'}
    if jurisdiction:
        params["court"] = jurisdiction
    if date_from:
        params["filed_after"] = date_from

    rid = request_id.get()
    async with httpx.AsyncClient(timeout=_CL_TIMEOUT) as client:
        t0 = time.perf_counter()
        r = await client.get(f"{CL_REST_BASE}/search/", headers=_cl_headers(), params=params)
        duration_ms = int((time.perf_counter() - t0) * 1000)
        log.info(
            "cl_api_call",
            endpoint="search_by_party",
            status=r.status_code,
            duration_ms=duration_ms,
            request_id=rid,
        )
        r.raise_for_status()
        return [_parse_search_result(d) for d in r.json().get("results", [])]


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

_CALLER_ID_SCHEMA = {
    "caller_id": {
        "type": "string",
        "description": (
            "Attorney identifier (e.g. email). Falls back to the CALLER_ID env var "
            "if omitted. Stamped on all audit records."
        ),
    },
}

_OWN_TOOLS: list[Tool] = [
    Tool(
        name="check_conflicts",
        description=(
            "Check a list of party names from a docket against the firm's client database "
            "to identify potential conflicts of interest. Always run before logging an "
            "opportunity. Returns a check_id that must be used with sign_off_conflict_check "
            "before opening a new matter."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "party_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Party names from the docket (plaintiffs, defendants, etc.).",
                },
                **_CALLER_ID_SCHEMA,
            },
            "required": ["party_names"],
        },
    ),
    Tool(
        name="sign_off_conflict_check",
        description=(
            "Record attorney sign-off on a conflict check result. Call after reviewing the "
            "check_conflicts output. Stamps the audit record with who reviewed it and what "
            "action was taken. Required before opening any new matter."
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
        name="find_entity_matches",
        description=(
            "Fuzzy-match a list of party names against the firm's client database. "
            "Returns matches with confidence scores. Use this after conflict checking to "
            "identify new business opportunities with existing clients."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "party_names": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "threshold": {
                    "type": "number",
                    "description": "Minimum match confidence (0.0–1.0). Default 0.5.",
                    "default": 0.5,
                },
            },
            "required": ["party_names"],
        },
    ),
    Tool(
        name="log_opportunity",
        description=(
            "Save a business development opportunity to the tracker. "
            "Call after confirming there are no conflicts and the opportunity is worth pursuing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "docket_json": {"type": "string", "description": "JSON-serialized Docket object."},
                "opportunity_type": {
                    "type": "string",
                    "enum": [t.value for t in OpportunityType],
                },
                "matched_client_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "summary": {"type": "string"},
                "assigned_to": {"type": "string"},
                **_CALLER_ID_SCHEMA,
            },
            "required": ["docket_json", "opportunity_type", "summary"],
        },
    ),
    Tool(
        name="list_opportunities",
        description="List tracked opportunities, optionally filtered by status or type.",
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
    Tool(
        name="update_opportunity_status",
        description="Update the status of a tracked opportunity after attorney review.",
        inputSchema={
            "type": "object",
            "properties": {
                "opportunity_id": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": [s.value for s in OpportunityStatus],
                },
                "notes": {"type": "string"},
                **_CALLER_ID_SCHEMA,
            },
            "required": ["opportunity_id", "status"],
        },
    ),
    Tool(
        name="search_filings_with_conflicts",
        description=(
            "Search CourtListener for recent filings and immediately check all discovered "
            "parties against the firm's client database. Returns each docket enriched with "
            "conflict flags and client matches — a single call replaces separate search + "
            "conflict-check steps."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "court": {
                    "type": "string",
                    "description": (
                        "CourtListener court ID, e.g. 'nysd' (S.D.N.Y.), 'cacd' (C.D. Cal.). "
                        "Omit to search all federal courts."
                    ),
                },
                "date_from": {
                    "type": "string",
                    "description": "Earliest filing date (YYYY-MM-DD). Defaults to yesterday.",
                },
                "keywords": {
                    "type": "string",
                    "description": "Full-text keyword search across docket text.",
                },
                "page_size": {
                    "type": "integer",
                    "description": "Number of filings to retrieve (max 100). Default 20.",
                    "default": 20,
                },
                **_CALLER_ID_SCHEMA,
            },
            "required": [],
        },
    ),
    Tool(
        name="check_party_in_courts",
        description=(
            "Search CourtListener for all recent cases involving a named entity, then check "
            "whether that entity (or any co-parties) matches an existing firm client. "
            "Useful for monitoring whether a known company has been named in new litigation."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "party_name": {
                    "type": "string",
                    "description": "Company or person name to search for.",
                },
                "jurisdiction": {
                    "type": "string",
                    "description": "Optional court ID to limit results.",
                },
                "date_from": {
                    "type": "string",
                    "description": "Earliest filing date (YYYY-MM-DD).",
                },
                **_CALLER_ID_SCHEMA,
            },
            "required": ["party_name"],
        },
    ),
]

_OWN_TOOL_NAMES: set[str] = {t.name for t in _OWN_TOOLS}


@app.list_tools()
async def list_tools() -> list[Tool]:
    return _proxied_cl_tools + _OWN_TOOLS


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    repo.init_db()

    rid = new_request_id()
    caller_id = arguments.get("caller_id") or _DEFAULT_CALLER_ID

    log.info("tool_call", tool=name, caller_id=caller_id, request_id=rid)

    try:
        result = await _dispatch(name, arguments, caller_id, rid)
        log.info("tool_success", tool=name, caller_id=caller_id, request_id=rid)
        return result
    except Exception as exc:
        log.error(
            "tool_error",
            tool=name,
            caller_id=caller_id,
            request_id=rid,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        raise


async def _dispatch(
    name: str, arguments: dict, caller_id: str, rid: str
) -> list[TextContent]:

    # ---- Forward to official CL MCP server ----
    if name in _proxied_cl_names and _cl_session is not None:
        result = await _cl_session.call_tool(name, arguments)
        return result.content  # type: ignore[return-value]

    # ---- Conflict & client intel tools ----

    if name == "check_conflicts":
        conflicts, check_id = repo.check_conflicts(arguments["party_names"], caller_id=caller_id)
        return [TextContent(
            type="text",
            text=json.dumps({
                "check_id": check_id,
                "result_summary": "CONFLICT_FOUND" if conflicts else "CLEAR",
                "conflicts": [c.model_dump() for c in conflicts],
            }, default=str, indent=2),
        )]

    if name == "sign_off_conflict_check":
        success = repo.sign_off_conflict_check(
            check_id=arguments["check_id"],
            action_taken=arguments["action_taken"],
            signed_off_by=caller_id,
        )
        return [TextContent(type="text", text=json.dumps({"success": success}))]

    if name == "find_entity_matches":
        matches = repo.find_entity_matches(
            party_names=arguments["party_names"],
            threshold=arguments.get("threshold", 0.5),
        )
        return [TextContent(
            type="text",
            text=json.dumps([m.model_dump() for m in matches], default=str, indent=2),
        )]

    if name == "log_opportunity":
        docket = Docket.model_validate_json(arguments["docket_json"])
        matched_clients = []
        for cid in arguments.get("matched_client_ids", []):
            client = repo.get_client(cid)
            if client:
                from src.models.models import EntityMatch
                matched_clients.append(EntityMatch(
                    client=client,
                    matched_party=Party(name=client.name, role="unknown"),
                    confidence=1.0,
                    match_reason="Provided by agent",
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

    if name == "list_opportunities":
        opps = repo.list_opportunities(
            status=OpportunityStatus(arguments["status"]) if "status" in arguments else None,
            opportunity_type=(
                OpportunityType(arguments["opportunity_type"])
                if "opportunity_type" in arguments else None
            ),
        )
        return [TextContent(
            type="text",
            text=json.dumps([o.model_dump() for o in opps], default=str, indent=2),
        )]

    if name == "update_opportunity_status":
        success = repo.update_opportunity_status(
            opportunity_id=arguments["opportunity_id"],
            status=OpportunityStatus(arguments["status"]),
            notes=arguments.get("notes"),
            caller_id=caller_id,
        )
        return [TextContent(type="text", text=json.dumps({"success": success}))]

    # ---- Combined tools ----

    if name == "search_filings_with_conflicts":
        try:
            date_from = arguments.get(
                "date_from",
                (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d"),
            )
            dockets = await _rest_search_dockets(
                court=arguments.get("court"),
                date_from=date_from,
                keywords=arguments.get("keywords"),
                page_size=arguments.get("page_size", 20),
            )
        except Exception as exc:
            log.error(
                "cl_search_failed",
                tool=name,
                error=str(exc),
                error_type=type(exc).__name__,
                request_id=rid,
            )
            return [TextContent(
                type="text",
                text=json.dumps({"error": f"CourtListener API error: {type(exc).__name__}: {exc}"}),
            )]

        enriched = []
        for docket in dockets:
            party_names = [p.name for p in docket.parties]
            conflicts, check_id = repo.check_conflicts(party_names, caller_id=caller_id) if party_names else ([], "")
            matches = repo.find_entity_matches(party_names, threshold=0.5) if party_names else []
            enriched.append({
                "docket": docket.model_dump(),
                "check_id": check_id,
                "conflicts": [c.model_dump() for c in conflicts],
                "client_matches": [m.model_dump() for m in matches],
                "has_conflict": len(conflicts) > 0,
                "has_client_match": len(matches) > 0,
            })

        return [TextContent(
            type="text",
            text=json.dumps(enriched, default=str, indent=2),
        )]

    if name == "check_party_in_courts":
        try:
            dockets = await _rest_search_by_party(
                name=arguments["party_name"],
                jurisdiction=arguments.get("jurisdiction"),
                date_from=arguments.get("date_from"),
            )
        except Exception as exc:
            log.error(
                "cl_search_failed",
                tool=name,
                error=str(exc),
                error_type=type(exc).__name__,
                request_id=rid,
            )
            return [TextContent(
                type="text",
                text=json.dumps({"error": f"CourtListener API error: {type(exc).__name__}: {exc}"}),
            )]

        all_party_names: list[str] = []
        seen: set[str] = set()
        for docket in dockets:
            for p in docket.parties:
                if p.name not in seen:
                    all_party_names.append(p.name)
                    seen.add(p.name)

        conflicts, check_id = repo.check_conflicts(all_party_names, caller_id=caller_id) if all_party_names else ([], "")
        matches = repo.find_entity_matches(all_party_names, threshold=0.5) if all_party_names else []

        return [TextContent(
            type="text",
            text=json.dumps(
                {
                    "search_term": arguments["party_name"],
                    "check_id": check_id,
                    "dockets_found": len(dockets),
                    "dockets": [d.model_dump() for d in dockets],
                    "conflicts": [c.model_dump() for c in conflicts],
                    "client_matches": [m.model_dump() for m in matches],
                    "has_conflict": len(conflicts) > 0,
                    "has_client_match": len(matches) > 0,
                },
                default=str,
                indent=2,
            ),
        )]

    return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    global _cl_session, _proxied_cl_tools, _proxied_cl_names

    repo.init_db()

    async with AsyncExitStack() as stack:
        if CL_TOKEN:
            try:
                cl_read, cl_write = await stack.enter_async_context(
                    sse_client(CL_MCP_URL, headers={"Authorization": f"Token {CL_TOKEN}"})
                )
                cl_session = await stack.enter_async_context(
                    ClientSession(cl_read, cl_write)
                )
                await cl_session.initialize()

                tools_result = await cl_session.list_tools()
                _cl_session = cl_session
                _proxied_cl_tools = tools_result.tools
                _proxied_cl_names = {t.name for t in tools_result.tools}

                log.info(
                    "cl_mcp_connected",
                    proxied_tools=len(_proxied_cl_tools),
                )
            except Exception as exc:
                log.warning(
                    "cl_mcp_unavailable",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    mode="local_only",
                )
        else:
            log.info("cl_mcp_skipped", reason="no_token", mode="local_only")

        read_stream, write_stream = await stack.enter_async_context(stdio_server())
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
