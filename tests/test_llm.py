from langchain_ollama import ChatOllama

from app.agent.llm import build_llm
from app.config import Settings


def test_ollama_provider_builds_chat_ollama_with_options():
    s = Settings(
        _env_file=None,
        llm_provider="ollama", ollama_url="http://laptop:11434",
        llm_model="qwen3:8b", temperature=0.0, seed=7, reasoning=False,
        num_predict=1024, num_ctx=4096, keep_alive=-1,
    )
    llm = build_llm(s)
    assert isinstance(llm, ChatOllama)
    assert llm.base_url == "http://laptop:11434"
    assert llm.model == "qwen3:8b"
    assert llm.seed == 7
    assert llm.reasoning is False
    assert llm.num_predict == 1024
    assert llm.num_ctx == 4096
    assert llm.keep_alive == -1


def test_litellm_provider_builds_chat_litellm():
    from langchain_litellm import ChatLiteLLM

    s = Settings(
        _env_file=None,
        llm_provider="anthropic", llm_model="claude-haiku-4-5-20251001",
        api_key="sk-test", seed=7,
    )
    llm = build_llm(s)
    assert isinstance(llm, ChatLiteLLM)
    assert llm.model == "anthropic/claude-haiku-4-5-20251001"
    assert llm.model_kwargs == {"seed": 7}
