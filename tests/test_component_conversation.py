"""Conversation-entity tests that need the Home Assistant test harness.

The harness (``homeassistant`` + ``pytest-homeassistant-custom-component``)
does not install into the project's Python 3.14 working venv without pulling
in ~90 packages and downgrading pytest/aiohttp, so it lives in a separate
venv. This module is skipped (not failed) when ``homeassistant`` is not
importable, which keeps the working-venv suite green while the harness venv
exercises it.

Scope: these tests drive ``LocalAgentConversationEntity.async_process``
directly against the real HA ``conversation`` / ``intent`` / ``ulid`` APIs
with a stub App client. Because the entity overrides ``async_process`` in
full (it does not touch ``self.hass`` or the chat-session/chat-log
machinery), no running ``hass`` is required. This verifies the HA API surface
the entity depends on -- ``ConversationEntity`` subclassing, the abstract
``supported_languages`` property, ``ConversationInput`` fields,
``IntentResponse.async_set_speech`` / ``async_set_error``,
``IntentResponseErrorCode.UNKNOWN``, ``ConversationResult`` construction and
``ulid.ulid_now`` -- and the entity's own logic (id assignment, thread_id
reuse, error mapping).

NOT covered here (needs a live HA instance to verify): config-flow
registration, ``async_setup_entry`` platform forwarding, entity/agent_id
registration, and that ``conversation.async_converse`` dispatches to
``async_process`` end to end. The full end-to-end path could not be exercised
under the harness on this machine because ``hass.block_till_done()`` hangs
(it blocks even for the bare ``homeassistant`` component setup under Python
3.14 here, and is not cancellable via ``asyncio.wait_for``).
"""

import pytest

pytest.importorskip("homeassistant")

from homeassistant.components import conversation  # noqa: E402
from homeassistant.core import Context  # noqa: E402
from homeassistant.helpers import intent  # noqa: E402

from custom_components.local_ha_agent.client import AgentApiError  # noqa: E402
from custom_components.local_ha_agent.conversation import (  # noqa: E402
    LocalAgentConversationEntity,
)


class _FakeEntry:
    entry_id = "entry-123"


class _StubClient:
    """Records chat() calls and echoes text + thread_id back as the reply."""

    def __init__(self):
        self.calls = []

    async def chat(self, text, conversation_id):
        self.calls.append((text, conversation_id))
        return f"echo:{text}:{conversation_id}"


class _BoomClient:
    async def chat(self, text, conversation_id):
        raise AgentApiError("connection refused")


def _make_input(text, conversation_id):
    return conversation.ConversationInput(
        text=text,
        context=Context(),
        conversation_id=conversation_id,
        device_id=None,
        satellite_id=None,
        language="en",
        agent_id="conversation.local_ha_agent",
    )


def test_entity_is_concrete_and_supports_all_languages():
    # Instantiating proves the abstract supported_languages property is
    # satisfied on this HA version.
    entity = LocalAgentConversationEntity(_FakeEntry(), _StubClient())
    assert entity.supported_languages == "*"
    assert entity._attr_unique_id == "entry-123"


async def test_async_process_returns_agent_speech():
    client = _StubClient()
    entity = LocalAgentConversationEntity(_FakeEntry(), client)

    result = await entity.async_process(_make_input("turn on the light", None))

    assert isinstance(result, conversation.ConversationResult)
    conversation_id = result.conversation_id
    assert conversation_id  # a fresh ulid was assigned
    assert result.response.speech["plain"]["speech"] == (
        f"echo:turn on the light:{conversation_id}"
    )
    # The App was sent the user's text and the assigned conversation id.
    assert client.calls == [("turn on the light", conversation_id)]


async def test_async_process_reuses_conversation_id_as_thread_id():
    client = _StubClient()
    entity = LocalAgentConversationEntity(_FakeEntry(), client)

    first = await entity.async_process(_make_input("hello", None))
    conversation_id = first.conversation_id

    second = await entity.async_process(_make_input("again", conversation_id))

    assert second.conversation_id == conversation_id
    # Both turns went to the App under the same thread_id.
    assert client.calls[0][1] == conversation_id
    assert client.calls[1][1] == conversation_id


async def test_async_process_maps_client_error_to_intent_error():
    entity = LocalAgentConversationEntity(_FakeEntry(), _BoomClient())

    result = await entity.async_process(_make_input("hello", None))

    assert result.response.error_code == intent.IntentResponseErrorCode.UNKNOWN
    assert "not reachable" in result.response.speech["plain"]["speech"]
    # A conversation_id is still returned so the caller can retry the thread.
    assert result.conversation_id
