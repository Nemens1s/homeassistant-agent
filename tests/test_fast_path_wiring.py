from app.agent.middleware import build_middleware
from app.fast_path.middleware import FastPathMiddleware
from app.fast_path.backend import FakeBackend
from app.tools.adapter import LoopGuard
from app.config import Settings


class _Menu:
    async def get(self): ...


def _types(mws):
    return [type(m).__name__ for m in mws]


def test_fastpath_added_when_provided_and_tier2():
    s = Settings(max_tier=2, enable_tool_subsetting=False)
    mws = build_middleware(s, LoopGuard(), "p", 1024, fast_path=(FakeBackend(), _Menu()))
    assert "FastPathMiddleware" in _types(mws)
    # outermost-first: FastPath is the last (innermost) entry
    assert isinstance(mws[-1], FastPathMiddleware)


def test_fastpath_absent_without_tier2():
    s = Settings(max_tier=1, enable_tool_subsetting=False)
    mws = build_middleware(s, LoopGuard(), "p", 1024, fast_path=(FakeBackend(), _Menu()))
    assert "FastPathMiddleware" not in _types(mws)


def test_fastpath_absent_when_none():
    s = Settings(max_tier=2, enable_tool_subsetting=False)
    mws = build_middleware(s, LoopGuard(), "p", 1024, fast_path=None)
    assert "FastPathMiddleware" not in _types(mws)
