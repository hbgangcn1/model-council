"""参数外置测试（评审报告 M 项）：params.py 加载/合并/兜底 + 消费方取值。

验收：缺文件/坏文件回退默认；深合并不丢兄弟键；环境变量隔离测试文件。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from orchestrator import params  # noqa: E402

FAILED = []

def check(name, cond):
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}")
        FAILED.append(name)

def test_load_defaults():
    print("[params 默认加载]")
    with tempfile.TemporaryDirectory() as td:
        os.environ["COUNCIL_PARAMS_FILE"] = os.path.join(td, "nope.json")
        p = params.load()
        check("缺文件回退 defaults", p["_source"] == "defaults")
        check("tiers 三档齐全", set(p["tiers"]) == {"fast", "standard", "deep"})
        check("circuit 参数存在", p["circuit"]["failureThreshold"] == 3)
        os.environ.pop("COUNCIL_PARAMS_FILE", None)

def test_merge_override():
    print("[params 覆盖与深合并]")
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "params.json"
        f.write_text(json.dumps({
            "tiers": {"fast": {"theta": 6.5}},   # v15.4：budgetCny 已删，覆盖 theta
            "circuit": {"failureThreshold": 5},
        }), encoding="utf-8")
        os.environ["COUNCIL_PARAMS_FILE"] = str(f)
        p = params.load()
        check("fast.theta 覆盖为 6.5", p["tiers"]["fast"]["theta"] == 6.5)
        check("fast.lambda_ 保留默认 0.15（v15.4 温和化）", p["tiers"]["fast"]["lambda_"] == 0.15)
        check("standard.theta 保留默认 8.0", p["tiers"]["standard"]["theta"] == 8.0)
        check("failureThreshold 覆盖为 5", p["circuit"]["failureThreshold"] == 5)
        check("failureWindowS 保留 600", p["circuit"]["failureWindowS"] == 600)
        os.environ.pop("COUNCIL_PARAMS_FILE", None)

def test_broken_file_fallback():
    print("[params 坏文件兜底]")
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "params.json"
        f.write_text("{not valid json", encoding="utf-8")
        os.environ["COUNCIL_PARAMS_FILE"] = str(f)
        p = params.load()
        check("坏文件回退 defaults（带 fallback 标记）", p["_source"].startswith("defaults(fallback:"))
        check("defaults 仍完整", p["tiers"]["deep"]["theta"] == 8.5)
        os.environ.pop("COUNCIL_PARAMS_FILE", None)

def test_get_dotted():
    print("[params.get 点路径]")
    p = params.load()
    check("get circuit.failureThreshold=3", params.get(p, "circuit.failureThreshold") == 3)
    check("get 不存在路径→默认值", params.get(p, "no.such.key", 42) == 42)

def main():
    test_load_defaults()
    test_merge_override()
    test_broken_file_fallback()
    test_get_dotted()
    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败: {FAILED}")
        sys.exit(1)
    print("✅ 全部通过")

if __name__ == "__main__":
    main()
