from app.telemetry import conventions as c


def test_keys_are_pinned_strings():
    assert c.SPAN_INVOKE_AGENT == "invoke_agent gosling"
    assert c.GEN_AI_OPERATION_NAME == "gen_ai.operation.name"
    assert c.GOSLING_FAST_PATH == "gosling.fast_path"
    assert c.GOSLING_TOOLS_OFFERED == "gosling.tools.offered"
    assert c.GOSLING_PATH == "gosling.path"


def test_span_names():
    assert c.SPAN_INVOKE_AGENT == "invoke_agent gosling"
    assert c.SPAN_CHAT == "chat"
    assert c.SPAN_CLASSIFY == "fast_path.classify"
    assert c.SPAN_EXECUTE_TOOL == "execute_tool"


def test_gen_ai_keys():
    assert c.GEN_AI_OPERATION_NAME == "gen_ai.operation.name"
    assert c.GEN_AI_CONVERSATION_ID == "gen_ai.conversation.id"
    assert c.GEN_AI_REQUEST_MODEL == "gen_ai.request.model"
    assert c.GEN_AI_RESPONSE_MODEL == "gen_ai.response.model"
    assert c.GEN_AI_USAGE_INPUT_TOKENS == "gen_ai.usage.input_tokens"
    assert c.GEN_AI_USAGE_OUTPUT_TOKENS == "gen_ai.usage.output_tokens"
    assert c.GEN_AI_RESPONSE_FINISH_REASONS == "gen_ai.response.finish_reasons"
    assert c.GEN_AI_TOOL_NAME == "gen_ai.tool.name"
    assert c.GEN_AI_TOOL_CALL_ID == "gen_ai.tool.call.id"


def test_gosling_invoke_agent_keys():
    assert c.GOSLING_CHANNEL == "gosling.channel"
    assert c.GOSLING_ENDPOINT == "gosling.endpoint"
    assert c.GOSLING_DEVICE_ID == "gosling.device_id"
    assert c.GOSLING_LLM_PROVIDER == "gosling.llm.provider"
    assert c.GOSLING_INPUT_TEXT == "gosling.input.text"
    assert c.GOSLING_OUTPUT_TEXT == "gosling.output.text"
    assert c.GOSLING_PATH == "gosling.path"
    assert c.GOSLING_OUTCOME == "gosling.outcome"
    assert c.GOSLING_PROMPT_HASH == "gosling.prompt_hash"
    assert c.GOSLING_TOOLSET_HASH == "gosling.toolset_hash"
    assert c.GOSLING_MAX_TIER == "gosling.max_tier"
    assert c.GOSLING_APP_VERSION == "gosling.app_version"
    assert c.GOSLING_TTFT_MS == "gosling.ttft_ms"


def test_gosling_chat_keys():
    assert c.GOSLING_FAST_PATH == "gosling.fast_path"
    assert c.GOSLING_STEP == "gosling.step"
    assert c.GOSLING_TOOLS_OFFERED == "gosling.tools.offered"
    assert c.GOSLING_MESSAGES_COUNT == "gosling.messages.count"
    assert c.GOSLING_THINKING_TEXT == "gosling.thinking.text"
    assert c.GOSLING_CONTENT_TEXT == "gosling.content.text"
    assert c.GOSLING_TOOL_CALLS == "gosling.tool_calls"


def test_gosling_fast_path_classify_keys():
    assert c.GOSLING_FP_BACKEND == "gosling.fast_path.backend"
    assert c.GOSLING_FP_MENU_HASH == "gosling.fast_path.menu_hash"
    assert c.GOSLING_FP_ENTITY_ID == "gosling.fast_path.entity_id"
    assert c.GOSLING_FP_CONFIDENCE == "gosling.fast_path.confidence"
    assert c.GOSLING_FP_THRESHOLD == "gosling.fast_path.threshold"
    assert c.GOSLING_FP_ACCEPTED == "gosling.fast_path.accepted"
    assert c.GOSLING_FP_SKIP_REASON == "gosling.fast_path.skip_reason"


def test_gosling_execute_tool_keys():
    assert c.GOSLING_TOOL_ARGS == "gosling.tool.args"
    assert c.GOSLING_TOOL_STATUS == "gosling.tool.status"
    assert c.GOSLING_TOOL_ERROR_CODE == "gosling.tool.error_code"
    assert c.GOSLING_TOOL_RESULT == "gosling.tool.result"
