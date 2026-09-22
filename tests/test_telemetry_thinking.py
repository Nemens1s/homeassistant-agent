from langchain_core.messages import AIMessage
from app.telemetry.thinking import split_thinking

def test_reasoning_content_field():
    m = AIMessage(content="the answer is 5", additional_kwargs={"reasoning_content": "let me think"})
    assert split_thinking(m) == ("let me think", "the answer is 5")

def test_inline_think_block():
    m = AIMessage(content="<think>hmm</think>the answer is 5")
    assert split_thinking(m) == ("hmm", "the answer is 5")

def test_no_thinking():
    m = AIMessage(content="plain")
    assert split_thinking(m) == ("", "plain")

def test_list_content_flattened():
    m = AIMessage(content=[{"type": "text", "text": "hello"}])
    assert split_thinking(m) == ("", "hello")
