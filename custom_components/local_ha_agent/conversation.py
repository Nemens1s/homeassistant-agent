"""Conversation entity: forwards Assist input to the App agent."""

from __future__ import annotations

from typing import Literal

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import ulid

from .client import AgentApiClient, AgentApiError
from .const import CONF_BASE_URL


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    client = AgentApiClient(entry.data[CONF_BASE_URL], async_get_clientsession(hass))
    async_add_entities([LocalAgentConversationEntity(entry, client)])


class LocalAgentConversationEntity(conversation.ConversationEntity):
    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, entry: ConfigEntry, client: AgentApiClient) -> None:
        self._client = client
        self._attr_unique_id = entry.entry_id

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        # ``ConversationEntity.supported_languages`` is an abstract property on
        # this HA version; the ``_attr_supported_languages`` shortcut does not
        # satisfy it, so it must be implemented directly. MATCH_ALL == "*".
        return MATCH_ALL

    async def async_process(
        self, user_input: conversation.ConversationInput
    ) -> conversation.ConversationResult:
        conversation_id = user_input.conversation_id or ulid.ulid_now()
        response = intent.IntentResponse(language=user_input.language)
        try:
            reply = await self._client.chat(user_input.text, conversation_id)
            response.async_set_speech(reply)
        except AgentApiError as err:
            response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                f"The local agent is not reachable: {err}",
            )
        return conversation.ConversationResult(
            response=response, conversation_id=conversation_id
        )
