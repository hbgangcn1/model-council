"""P2-2：汇率陈旧状态 CLI（供 host-bridge plugin 插件 run_council 前置检查）。

用法：python orchestrator/fx_status.py
退出码：0=正常/黄灯 / 2=停机（落后 ≥ staleHaltDays 个交易日，应暂停 CNY 记账）。
输出：selector.fx_status() 的 JSON。
"""
import json
import sys

try:
    from orchestrator import selector
except ImportError:
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
    from orchestrator import selector  # noqa: E402

st = selector.fx_status()
print(json.dumps(st, ensure_ascii=False, indent=2))
sys.exit(2 if st.get("level", 0) >= 2 else 0)
