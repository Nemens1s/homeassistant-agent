import re

from pydantic import BaseModel, Field

from app.constants import WRITE_DOMAINS
from app.tools.base import Tier, ToolDefinition, ToolResult, bound_rows, entity_domain
from app.tools.helpers.lookups import area_names, entity_area_ids
from app.tools.registry import register
from app.tools.context import ws_ready


class Params(BaseModel):
    query: str = Field(description="Search term matched case-insensitively against entity_id and friendly name.")


# Filler words that would otherwise substring-match half the home ("the" is in
# "weather" and "thermostat").
_STOP_WORDS = frozenset({"the", "a", "an", "of", "in", "on", "at", "is", "my", "our", "to", "for"})

# Default row cap for this tool; settings.max_rows stays the upper bound.
_DEFAULT_CAP = 20

_ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")


def _query_words(query: str) -> list[str]:
    """Lowercase, punctuation stripped (except '.' and '_', which entity_ids
    use), stop words dropped."""
    cleaned = re.sub(r"[^\w\s.]", " ", query.lower())
    words = []
    for w in cleaned.split():
        w = w.strip(".")
        if w and w not in _STOP_WORDS:
            words.append(w)
    return words


def _rank(states: list[dict], words: list[str]) -> list[dict]:
    """Entities matching every word, or — if none does — those matching any
    word; most words matched first, then by friendly name."""
    scored = []
    for s in states:
        eid_lower = s["entity_id"].lower()
        fname_lower = s.get("attributes", {}).get("friendly_name", "").lower()
        score = 0
        for w in words:
            if w in eid_lower or w in fname_lower:
                score += 1
        if score:
            scored.append((score, fname_lower, s))

    full = []
    for item in scored:
        if item[0] == len(words):
            full.append(item)
    if full:
        scored = full

    scored.sort(key=lambda item: (-item[0], item[1]))
    ranked = []
    for item in scored:
        ranked.append(item[2])
    return ranked


async def _label_map(ctx) -> dict[str, list[str]]:
    """{entity_id: [label_names]} merging entity labels with its device's labels."""
    devices = await ctx.ws.request_cached("config/device_registry/list")
    entities = await ctx.ws.request_cached("config/entity_registry/list")
    labels = await ctx.ws.request_cached("config/label_registry/list")

    label_name = {}
    for lb in labels:
        label_name[lb["label_id"]] = lb["name"]

    device_labels: dict[str, list[str]] = {}
    for d in devices:
        resolved = []
        for lid in d.get("labels", []):
            resolved.append(label_name.get(lid, lid))
        device_labels[d["id"]] = resolved

    label_map: dict[str, list[str]] = {}
    for e in entities:
        entity_lbls = []
        for lid in e.get("labels", []):
            entity_lbls.append(label_name.get(lid, lid))
        dev_lbls = device_labels.get(e.get("device_id", ""), [])
        all_lbls = sorted(set(entity_lbls) | set(dev_lbls))
        if all_lbls:
            label_map[e["entity_id"]] = all_lbls
    return label_map


async def handler(params: Params, ctx) -> ToolResult:
    query = params.query.strip().lower()
    words = _query_words(query)
    if not words:
        # Only stop words / punctuation: there is nothing to search on.
        return ToolResult.error("invalid_params", "query has no search words; name the thing to find")
    states = await ctx.rest.list_states()

    matches = []
    if _ENTITY_ID.match(query):
        for s in states:
            if s["entity_id"] == query:
                matches.append(s)
                break
    if not matches:
        matches = _rank(states, words)

    area_map: dict[str, str] = {}
    label_map: dict[str, list[str]] = {}
    if ws_ready(ctx):
        names = await area_names(ctx)
        # include all categories: a matched diagnostic entity should still show its area
        by_area = await entity_area_ids(ctx, exclude_categories=())
        for eid, aid in by_area.items():
            area_map[eid] = names.get(aid, "")
        label_map = await _label_map(ctx)

    controllable = set()
    for d in WRITE_DOMAINS:
        if d != "automation":
            controllable.add(d)

    rows = []
    for s in matches:
        domain = entity_domain(s["entity_id"])
        row: dict = {
            "entity_id": s["entity_id"],
            "state": s["state"],
            "friendly_name": s.get("attributes", {}).get("friendly_name", ""),
            "controllable": domain in controllable,
        }
        if ws_ready(ctx):
            row["area"] = area_map.get(s["entity_id"], "")
            lbls = label_map.get(s["entity_id"])
            if lbls:
                row["labels"] = lbls
        rows.append(row)
    cap = min(_DEFAULT_CAP, ctx.settings.max_rows)
    return ToolResult.ok(bound_rows(rows, max_rows=cap))


register(
    ToolDefinition(
        name="search_entities",
        description=(
            "Search entities by a keyword in their name or entity_id when the exact entity_id is unknown. Returns state and area for each match. "
            "Use when the user names a specific thing but you don't know its entity_id (e.g. 'zigbee bridge', 'sleeping helper', 'tapo'). "
            "Not for whole-room overviews, automation lists, or battery questions — those have dedicated tools."
        ),
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
