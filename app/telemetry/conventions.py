# Single source of truth for OTel span names and attribute keys.
# Span names follow the design doc's "Telemetry span model" section.
# Attribute names follow OTel GenAI semantic conventions where one exists
# (experimental — pinned here); project-specific attributes use the "gosling." prefix.
# No OTel import needed: these are plain string constants.

# ---------------------------------------------------------------------------
# Span names
# ---------------------------------------------------------------------------

# Root span for a request (created in main.py and the CLI)
SPAN_INVOKE_AGENT = "invoke_agent gosling"

# One per model step (LLM or fast-path synthetic response)
SPAN_CHAT = "chat"

# Child of the first-step chat span; created by FastPathMiddleware
SPAN_CLASSIFY = "fast_path.classify"

# One per tool execution (created by TelemetryMiddleware)
SPAN_EXECUTE_TOOL = "execute_tool"

# ---------------------------------------------------------------------------
# OTel GenAI semantic convention attribute keys
# (experimental spec, pinned to the names current at design time)
# ---------------------------------------------------------------------------

# invoke_agent gosling + chat spans
GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_CONVERSATION_ID = "gen_ai.conversation.id"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GEN_AI_RESPONSE_FINISH_REASONS = "gen_ai.response.finish_reasons"

# execute_tool spans
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_TOOL_CALL_ID = "gen_ai.tool.call.id"

# ---------------------------------------------------------------------------
# gosling.* project-specific attribute keys
# ---------------------------------------------------------------------------

# --- invoke_agent gosling span ---

# Channel through which the request arrived: ui | assist | cli | event
GOSLING_CHANNEL = "gosling.channel"

# HTTP endpoint or "cli"
GOSLING_ENDPOINT = "gosling.endpoint"

# Optional device_id forwarded by the Assist component
GOSLING_DEVICE_ID = "gosling.device_id"

# Configured LLM provider (e.g. "ollama", "openai"); paired with gen_ai.request.model
GOSLING_LLM_PROVIDER = "gosling.llm.provider"

# User message as received (post-Whisper for voice)
GOSLING_INPUT_TEXT = "gosling.input.text"

# Final reply text
GOSLING_OUTPUT_TEXT = "gosling.output.text"

# fast_path | agent — fast_path if the first step was a hit
GOSLING_PATH = "gosling.path"

# ok | recursion_limit | error | cancelled
GOSLING_OUTCOME = "gosling.outcome"

# SHA-256 (16 hex chars) of the base system prompt (see Snapshots)
GOSLING_PROMPT_HASH = "gosling.prompt_hash"

# SHA-256 (16 hex chars) of the full tool registry schema at build time
GOSLING_TOOLSET_HASH = "gosling.toolset_hash"

# Maximum tool tier allowed for this request
GOSLING_MAX_TIER = "gosling.max_tier"

# App version string
GOSLING_APP_VERSION = "gosling.app_version"

# Time to first token in ms (streaming endpoint only)
GOSLING_TTFT_MS = "gosling.ttft_ms"

# --- chat span ---

# True for synthetic fast-path responses
GOSLING_FAST_PATH = "gosling.fast_path"

# 1-based step index within the request
GOSLING_STEP = "gosling.step"

# JSON list of tool names in request.tools (after subsetting)
GOSLING_TOOLS_OFFERED = "gosling.tools.offered"

# Number of messages sent to the model after trimming
GOSLING_MESSAGES_COUNT = "gosling.messages.count"

# Full thinking text (reasoning field or <think> block)
GOSLING_THINKING_TEXT = "gosling.thinking.text"

# Visible content text
GOSLING_CONTENT_TEXT = "gosling.content.text"

# JSON list of {id, name, args}
GOSLING_TOOL_CALLS = "gosling.tool_calls"

# --- fast_path.classify span ---

# Backend name (e.g. "needle", "needle-remote")
GOSLING_FP_BACKEND = "gosling.fast_path.backend"

# SHA-256 (16 hex chars) of the menu items as sent to the backend
GOSLING_FP_MENU_HASH = "gosling.fast_path.menu_hash"

# Predicted entity_id, or empty string for a miss
GOSLING_FP_ENTITY_ID = "gosling.fast_path.entity_id"

# Model confidence score
GOSLING_FP_CONFIDENCE = "gosling.fast_path.confidence"

# Acceptance threshold configured for the backend
GOSLING_FP_THRESHOLD = "gosling.fast_path.threshold"

# True when confidence >= threshold and entity_id is set
GOSLING_FP_ACCEPTED = "gosling.fast_path.accepted"

# Reason not classified: disabled | empty_menu | error
GOSLING_FP_SKIP_REASON = "gosling.fast_path.skip_reason"

# --- execute_tool span ---

# Tool input arguments as JSON with sorted keys
GOSLING_TOOL_ARGS = "gosling.tool.args"

# Status parsed from the result envelope (ok, error, repeated_call, etc.)
GOSLING_TOOL_STATUS = "gosling.tool.status"

# Error code parsed from the result envelope (AI-gate codes, repeated_call, etc.)
GOSLING_TOOL_ERROR_CODE = "gosling.tool.error_code"

# Full result envelope JSON, capped at 64 KB
GOSLING_TOOL_RESULT = "gosling.tool.result"
