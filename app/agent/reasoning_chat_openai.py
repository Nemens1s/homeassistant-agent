"""ChatOpenAI subclass that surfaces reasoning_content from the streaming delta.

ChatOpenAI explicitly does not extract this field. ChoiceDelta has
extra="allow" so the value survives model_dump(); we lift it into
additional_kwargs where the rest of the pipeline expects it.
Works with llama.cpp, DeepSeek API, and any OpenAI-compatible provider.
"""

from __future__ import annotations

from langchain_openai import ChatOpenAI


class ReasoningChatOpenAI(ChatOpenAI):
    def _convert_chunk_to_generation_chunk(self, chunk, default_chunk_class, base_generation_info):
        gen_chunk = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        if gen_chunk is None:
            return gen_chunk
        choices = chunk.get("choices") or chunk.get("chunk", {}).get("choices", [])
        if choices:
            delta = choices[0].get("delta") or {}
            reasoning = delta.get("reasoning_content", "")
            if reasoning:
                gen_chunk.message.additional_kwargs["reasoning_content"] = reasoning
        return gen_chunk
