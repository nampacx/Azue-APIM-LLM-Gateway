"""S.H.I.E.L.D. MCP server – an Azure Function (Python v2) using the Functions MCP extension.

Exposed through the HEIMDAL gateway (APIM) as an MCP server. Tools are deliberately simple,
static MCU-flavoured data so the demo needs no extra backends.
"""
import json
import logging

import azure.functions as func

app = func.FunctionApp()

AVENGERS = {
    "thor": {"name": "Thor Odinson", "role": "God of Thunder", "status": "available", "weapon": "Stormbreaker"},
    "tony stark": {"name": "Tony Stark", "role": "Genius, billionaire", "status": "on mission", "weapon": "Iron Man Mark LXXXV"},
    "hulk": {"name": "Bruce Banner", "role": "Scientist / Hulk", "status": "available", "weapon": "Gamma radiation"},
    "black widow": {"name": "Natasha Romanoff", "role": "Spy", "status": "undercover", "weapon": "Widow's Bite"},
    "captain america": {"name": "Steve Rogers", "role": "Super soldier", "status": "available", "weapon": "Vibranium shield"},
}

MISSIONS = {
    "bifrost-repair": {"status": "in-progress", "lead": "Thor", "threat": "medium"},
    "tesseract-recovery": {"status": "planned", "lead": "Black Widow", "threat": "high"},
    "ultron-cleanup": {"status": "completed", "lead": "Tony Stark", "threat": "critical"},
}

STONES = {
    "space": {"colour": "blue", "location": "Tesseract (Asgard vault)"},
    "mind": {"colour": "yellow", "location": "Vision's forehead"},
    "reality": {"colour": "red", "location": "The Aether, Collector's gallery"},
    "power": {"colour": "purple", "location": "Orb, Xandar"},
    "time": {"colour": "green", "location": "Eye of Agamotto"},
    "soul": {"colour": "orange", "location": "Vormir"},
}

_NAME_PROP = json.dumps([{"propertyName": "name", "propertyType": "string", "description": "Name, e.g. 'thor'."}])


def _lookup(table: dict, context: str) -> str:
    args = json.loads(context).get("arguments", {})
    key = str(args.get("name", "")).strip().lower()
    logging.info("MCP lookup name=%s", key)
    item = table.get(key)
    return json.dumps(item) if item else json.dumps({"error": f"'{key}' not found", "known": sorted(table)})


@app.mcp_tool_trigger(
    arg_name="context",
    tool_name="list_avengers",
    description="List the Avengers roster with their current status.",
    tool_properties="[]",
)
def list_avengers(context: str) -> str:
    return json.dumps([{"id": k, **v} for k, v in AVENGERS.items()])


@app.mcp_tool_trigger(
    arg_name="context",
    tool_name="get_avenger",
    description="Get details (role, status, weapon) of one Avenger by name.",
    tool_properties=_NAME_PROP,
)
def get_avenger(context: str) -> str:
    return _lookup(AVENGERS, context)


@app.mcp_tool_trigger(
    arg_name="context",
    tool_name="get_mission_status",
    description="Get status, lead and threat level of a S.H.I.E.L.D. mission by name.",
    tool_properties=_NAME_PROP,
)
def get_mission_status(context: str) -> str:
    return _lookup(MISSIONS, context)


@app.mcp_tool_trigger(
    arg_name="context",
    tool_name="locate_infinity_stone",
    description="Locate an Infinity Stone (space, mind, reality, power, time, soul).",
    tool_properties=_NAME_PROP,
)
def locate_infinity_stone(context: str) -> str:
    return _lookup(STONES, context)
