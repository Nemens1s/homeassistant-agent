from pydantic import BaseModel

from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.registry import register


class Params(BaseModel):
    pass


async def handler(params: Params, ctx) -> ToolResult:
    if ctx.ws is None:
        return ToolResult.error(
            "ws_unavailable",
            "Weather lookup needs the websocket connection, which is not available.",
        )
    entities = await ctx.ws.request_cached("config/entity_registry/list")
    weather_ids = [e["entity_id"] for e in entities if e["entity_id"].startswith("weather.")]

    if not weather_ids:
        return ToolResult.error("no_weather_entity", "No weather entities found in Home Assistant.")

    results = []
    for entity_id in weather_ids:
        state = await ctx.rest.get_state(entity_id)
        attrs = state.get("attributes", {})
        entry = {
            "entity_id": entity_id,
            "condition": state["state"],
            "temperature": attrs.get("temperature"),
            "temperature_unit": attrs.get("temperature_unit"),
            "humidity": attrs.get("humidity"),
            "wind_speed": attrs.get("wind_speed"),
            "wind_speed_unit": attrs.get("wind_speed_unit"),
            "wind_bearing": attrs.get("wind_bearing"),
            "pressure": attrs.get("pressure"),
            "pressure_unit": attrs.get("pressure_unit"),
        }
        forecast = attrs.get("forecast") or []
        if forecast:
            entry["forecast"] = forecast[:5]
        results.append(entry)

    return ToolResult.ok(results[0] if len(results) == 1 else results)


register(
    ToolDefinition(
        name="get_weather",
        description="Get current weather conditions (temperature, humidity, wind, condition) and upcoming forecast if available. Use for any weather-related question.",
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
