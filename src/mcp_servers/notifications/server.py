"""
Notifications MCP Server
Local dev mode: writes to stdout and a local log file.
Production: swapped for Microsoft Graph calls (Teams, Outlook).
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

app = Server("notifications")

LOG_PATH = Path(__file__).parent.parent.parent.parent / "data" / "notifications.log"


def _write_log(entry: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    # Also print so you can see it in terminal during local dev
    print(f"\n[NOTIFICATION] {entry['type'].upper()}")
    print(f"  Channel : {entry.get('channel', entry.get('recipient', 'N/A'))}")
    print(f"  Title   : {entry.get('title', entry.get('subject', 'N/A'))}")
    print(f"  Body    : {str(entry.get('body', ''))[:120]}...")
    print()


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="send_teams_card",
            description=(
                "Post an opportunity or conflict alert as an adaptive card to a Teams channel. "
                "In production this calls Microsoft Graph. Locally it logs to file."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "string",
                        "description": "Teams channel name, e.g. 'business-development' or 'conflicts'.",
                    },
                    "title": {"type": "string"},
                    "body": {
                        "type": "string",
                        "description": "Plain-text summary. In production this becomes a rich adaptive card.",
                    },
                    "opportunity_json": {
                        "type": "string",
                        "description": "Optional JSON-serialized Opportunity for structured card rendering.",
                    },
                    "urgency": {
                        "type": "string",
                        "enum": ["low", "normal", "high"],
                        "default": "normal",
                    },
                },
                "required": ["channel", "title", "body"],
            },
        ),
        Tool(
            name="draft_email",
            description=(
                "Draft and send an email to a relationship partner about an opportunity. "
                "In production this calls Microsoft Graph Mail.Send. Locally it logs to file."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "recipient": {
                        "type": "string",
                        "description": "Recipient name or email address.",
                    },
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["recipient", "subject", "body"],
            },
        ),
        Tool(
            name="create_outlook_task",
            description=(
                "Create a follow-up task in the assigned attorney's Outlook task list. "
                "In production this calls Microsoft Graph Tasks. Locally it logs to file."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "assignee": {
                        "type": "string",
                        "description": "Attorney name or email.",
                    },
                    "title": {"type": "string"},
                    "details": {"type": "string"},
                    "due_date": {
                        "type": "string",
                        "description": "Due date in YYYY-MM-DD format.",
                    },
                },
                "required": ["assignee", "title", "details"],
            },
        ),
        Tool(
            name="post_digest",
            description="Post a weekly digest of opportunities to a Teams channel.",
            inputSchema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string"},
                    "opportunities_json": {
                        "type": "string",
                        "description": "JSON array of opportunity summaries.",
                    },
                    "period": {
                        "type": "string",
                        "description": "Human-readable period label, e.g. 'Week of June 23, 2026'.",
                    },
                },
                "required": ["channel", "opportunities_json", "period"],
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:

    now = datetime.utcnow().isoformat()

    if name == "send_teams_card":
        entry = {
            "type": "teams_card",
            "timestamp": now,
            "channel": arguments["channel"],
            "title": arguments["title"],
            "body": arguments["body"],
            "urgency": arguments.get("urgency", "normal"),
        }
        _write_log(entry)
        return [TextContent(type="text", text=json.dumps({"status": "logged", "entry": entry}))]

    elif name == "draft_email":
        entry = {
            "type": "email",
            "timestamp": now,
            "recipient": arguments["recipient"],
            "subject": arguments["subject"],
            "body": arguments["body"],
        }
        _write_log(entry)
        return [TextContent(type="text", text=json.dumps({"status": "logged", "entry": entry}))]

    elif name == "create_outlook_task":
        entry = {
            "type": "outlook_task",
            "timestamp": now,
            "assignee": arguments["assignee"],
            "title": arguments["title"],
            "details": arguments["details"],
            "due_date": arguments.get("due_date"),
        }
        _write_log(entry)
        return [TextContent(type="text", text=json.dumps({"status": "logged", "entry": entry}))]

    elif name == "post_digest":
        opportunities = json.loads(arguments["opportunities_json"])
        entry = {
            "type": "weekly_digest",
            "timestamp": now,
            "channel": arguments["channel"],
            "period": arguments["period"],
            "opportunity_count": len(opportunities),
            "opportunities": opportunities,
        }
        _write_log(entry)
        return [TextContent(type="text", text=json.dumps({"status": "logged", "entry": entry}))]

    return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
