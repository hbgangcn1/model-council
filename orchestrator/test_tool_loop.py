"""v15.10 council 工具循环测试（执行员自搜 + 验证员复核）。
v15.11 分池记账（exec/verifier 各一池 + run 级 backstop）。

全 stub，不碰网络：伪造 DSHBridgeClient（按轮返回 tool_calls/文本）与
tool_exec，验证多轮循环、预算封顶、allowlist、不可信包装、轨迹摘要。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator import tool_loop  # noqa: E402
from orchestrator.llm_transports import dsh_bridge as _bridge  # noqa: E402


class _FakeClient:
    """按预设剧本返回的假 bridge：script = list of (text, tool_calls)。"""

    def __init__(self, script, *a, **k):
        self._script = list(script)
        self.calls = []

    def call_messages(self, model, level, messages, tools, max_tokens):
        self.calls.append({"messages": [dict(m) for m in messages],
                           "tools": tools})
        if not self._script:
            return "收尾文本", {"usage": {}, "elapsed_s": 0.1}
        text, tcs = self._script.pop(0)
        meta = {"usage": {"promptTokens": 10, "completionTokens": 5},
                "elapsed_s": 0.1}
        if tcs:
            meta["tool_calls"] = tcs
        return text, meta


def _tc(name, args, cid="c1"):
    import json
    return {"id": cid, "name": name,
            "arguments": args if isinstance(args, str) else json.dumps(args)}


def _patch(monkeypatch, script, exec_results=None):
    monkeypatch.setattr(_bridge, "DSHBridgeClient", _FakeClient)

    def _init(self, *a, **k):
        self._script = list(script)
        self.calls = []
    monkeypatch.setattr(_FakeClient, "__init__", _init)
    seen = {}

    def _fake_exec(name, args, timeout_s=None, bridge_url=None):
        seen[name] = args
        if exec_results and name in exec_results:
            v = exec_results[name]
            if isinstance(v, Exception):
                raise v
            return v
        return f"[{name} 结果]"
    monkeypatch.setattr(_bridge, "tool_exec", _fake_exec)
    return seen


def test_multi_round_loop(monkeypatch):
    script = [
        ("", [_tc("web_search", {"queries": ["q1"]}, "c1")]),
        ("最终答案附链接", []),
    ]
    _patch(monkeypatch, script)
    budget = tool_loop.ToolBudget(10)
    text, meta, traj = tool_loop.call_with_tools(
        "m", "low", "prompt", 100, tool_loop.council_tools(), 3, budget)
    assert text == "最终答案附链接"
    assert len(traj) == 1 and traj[0]["tool"] == "web_search" and traj[0]["ok"]
    assert meta["transport"] == "dsh_bridge+tools"
    assert meta["usage"]["promptTokens"] == 20  # 两轮累加


def test_disallowed_tool_rejected_without_exec(monkeypatch):
    script = [
        ("", [_tc("bash", {"cmd": "rm"}, "c9")]),
        ("收尾", []),
    ]
    seen = _patch(monkeypatch, script)
    budget = tool_loop.ToolBudget(10)
    text, meta, traj = tool_loop.call_with_tools(
        "m", "low", "prompt", 100, tool_loop.council_tools(), 3, budget)
    assert "bash" not in seen  # 根本没执行
    assert traj[0]["error"] == "tool_not_allowed"
    assert text == "收尾"


def test_budget_exhaustion_ends_gracefully(monkeypatch):
    script = [
        ("", [_tc("web_search", {"queries": ["a"]}, "c1"),
              _tc("web_search", {"queries": ["b"]}, "c2")]),
        ("预算内收尾", []),
    ]
    _patch(monkeypatch, script)
    budget = tool_loop.ToolBudget(1)  # 只够 1 次，整批 2 次原子申请失败
    text, meta, traj = tool_loop.call_with_tools(
        "m", "low", "prompt", 100, tool_loop.council_tools(), 3, budget)
    assert any(str(t.get("error", "")).startswith("tool_budget_exhausted") for t in traj)
    assert text == "预算内收尾"


def test_wrap_truncates_and_marks_untrusted():
    long_text = "x" * (tool_loop.TOOL_RESULT_MAX_CHARS + 100)
    out = tool_loop.wrap_tool_result("web_search", {"queries": ["q"]}, long_text)
    assert "不可信" in out
    assert "已截断" in out
    assert len(out) < len(long_text) + 500


def test_trajectory_digest():
    traj = [{"round": 0, "tool": "web_search", "args": {"queries": ["q"]},
             "ok": True, "resultChars": 123},
            {"round": 1, "tool": "web_fetch", "args": {}, "ok": False,
             "resultChars": 0, "error": "boom"}]
    d = tool_loop.trajectory_digest(traj)
    assert "web_search" in d and "123字" in d and "boom" in d
    assert tool_loop.trajectory_digest([]) == "（本路未调用工具）"


def test_council_tools_shape():
    tools = tool_loop.council_tools()
    names = [t["function"]["name"] for t in tools]
    assert names == ["web_search", "web_fetch", "read_file", "list_dir", "search_content"]
    assert set(names) <= set(tool_loop.ALLOWED_TOOLS)


def test_council_tools_json_types():
    # 2026-09-12 实战教训：description 后的多余逗号会把它变成单元素 tuple，
    # JSON 里变成 array → 上游 union 校验报 tools[0] did not match any
    # supported type，两家 provider 全挂。所有文本字段必须是 str。
    import json
    for t in tool_loop.council_tools():
        assert t["type"] == "function"
        fn = t["function"]
        assert isinstance(fn["name"], str)
        assert isinstance(fn["description"], str), fn["name"]
        params = fn["parameters"]
        assert params["type"] == "object"
        assert isinstance(params["properties"], dict)
        assert isinstance(params.get("required", []), list)
        for pname, pspec in params["properties"].items():
            assert isinstance(pspec.get("type"), str), (fn["name"], pname)
            for k, v in pspec.items():
                assert isinstance(v, (str, dict, list, int, float, bool)), (fn["name"], pname, k)
    # 整体可 JSON 往返（tuple 会变成 array，顺带抓住）
    rt = json.loads(json.dumps(tool_loop.council_tools()))
    assert isinstance(rt[0]["function"]["description"], str)


# ---------- v15.11 分池 + backstop ----------

def test_acquire_split_phase_fail():
    phase = tool_loop.ToolBudget(1)
    backstop = tool_loop.ToolBudget(100)
    assert tool_loop.acquire_split(phase, backstop, 1) is None
    assert tool_loop.acquire_split(phase, backstop, 1) == "phase"
    assert backstop.used == 1  # 第二次分池就没过，backstop 没动


def test_acquire_split_backstop_rollback():
    phase = tool_loop.ToolBudget(100)
    backstop = tool_loop.ToolBudget(1)
    assert tool_loop.acquire_split(phase, backstop, 1) is None
    assert (phase.used, backstop.used) == (1, 1)
    assert tool_loop.acquire_split(phase, backstop, 1) == "backstop"
    assert phase.used == 1  # backstop 没拿到，分池扣款已回滚
    assert backstop.used == 1


def test_release_floor_zero():
    b = tool_loop.ToolBudget(2)
    b.release(5)
    assert b.used == 0 and b.remaining() == 2


def test_backstop_trips_in_loop(monkeypatch):
    script = [
        ("", [_tc("web_search", {"queries": ["a"]}, "c1")]),
        ("", [_tc("web_search", {"queries": ["b"]}, "c2")]),
        ("收尾", []),
    ]
    _patch(monkeypatch, script)
    phase = tool_loop.ToolBudget(100)
    backstop = tool_loop.ToolBudget(1)
    text, meta, traj = tool_loop.call_with_tools(
        "m", "low", "prompt", 100, tool_loop.council_tools(), 3, phase,
        backstop=backstop)
    assert any(t.get("error") == "tool_budget_exhausted(backstop)" for t in traj)
    assert phase.used == 1  # 第二轮回滚，只花了第一轮的 1 次
    assert text == "收尾"


def test_phase_pools_isolated(monkeypatch):
    script = [
        ("", [_tc("web_search", {"queries": ["a"]}, "c1")]),
        ("exec done", []),
    ]
    _patch(monkeypatch, script)
    exec_pool = tool_loop.ToolBudget(60)
    ver_pool = tool_loop.ToolBudget(60)
    backstop = tool_loop.ToolBudget(500)
    tool_loop.call_with_tools("m", "low", "p", 100, tool_loop.council_tools(),
                              3, exec_pool, backstop=backstop)
    assert exec_pool.used == 1
    assert ver_pool.used == 0 and ver_pool.remaining() == 60  # 验证员池分毫未动
    assert backstop.used == 1


# ---------- v15.14b 收尾失败留痕 ----------

def test_wrap_up_failure_is_recorded(monkeypatch):
    """v15.14b：工具轮次耗尽后的「收尾调用」失败，必须留痕。

    此前那里是 `except Exception: pass`，完全静默 → 上层只看到笼统的 transport-error，
    得手工翻 verdict-raw 的 meta 才能还原真相（实测 run 2026-09-14_21-17-03 那条
    1/40 的失败就是这么查出来的）。收尾请求的特征是 tools is None。
    """
    script = [
        ("", [_tc("web_search", {"queries": ["a"]}, "c1")]),
        ("", [_tc("web_search", {"queries": ["b"]}, "c2")]),
    ]

    class _RaiseOnWrap(_FakeClient):
        def __init__(self, *a, **k):      # 真实调用是 DSHBridgeClient(url=...)
            self._script = list(script)
            self.calls = []

        def call_messages(self, model, level, messages, tools, max_tokens):
            if tools is None:             # 收尾请求不带 tools
                raise RuntimeError("wrap up boom")
            return super().call_messages(model, level, messages, tools, max_tokens)

    monkeypatch.setattr(_bridge, "DSHBridgeClient", _RaiseOnWrap)
    monkeypatch.setattr(_bridge, "tool_exec",
                        lambda name, args, timeout_s=None, bridge_url=None: "[ok]")

    _text, meta, traj = tool_loop.call_with_tools(
        "m", "low", "prompt", 100, tool_loop.council_tools(), 2,
        tool_loop.ToolBudget(10))
    assert meta["tool_stop_note"] == "max_tool_rounds_exceeded+wrap_up_failed"
    assert "boom" in (meta.get("wrap_up_error") or "")
    assert len(traj) == 2        # 工具本身执行成功，失败的是收尾那一次


def test_wrap_up_empty_is_recorded(monkeypatch):
    """收尾调用成功但没吐文本，也要与「正常收尾」区分开。"""
    script = [
        ("", [_tc("web_search", {"queries": ["a"]}, "c1")]),
        ("", [_tc("web_search", {"queries": ["b"]}, "c2")]),
    ]

    class _EmptyWrap(_FakeClient):
        def __init__(self, *a, **k):
            self._script = list(script)
            self.calls = []

        def call_messages(self, model, level, messages, tools, max_tokens):
            if tools is None:
                return "", {"usage": {}, "elapsed_s": 0.1}
            return super().call_messages(model, level, messages, tools, max_tokens)

    monkeypatch.setattr(_bridge, "DSHBridgeClient", _EmptyWrap)
    monkeypatch.setattr(_bridge, "tool_exec",
                        lambda name, args, timeout_s=None, bridge_url=None: "[ok]")
    _text, meta, _traj = tool_loop.call_with_tools(
        "m", "low", "prompt", 100, tool_loop.council_tools(), 2,
        tool_loop.ToolBudget(10))
    assert meta["tool_stop_note"] == "max_tool_rounds_exceeded+wrap_up_empty"
