"""terminator / calibration / budget 单元测试（v15.5 判停标准）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from orchestrator import terminator, calibration, budget

FAILED = []

def check(name, cond):
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}")
        FAILED.append(name)

def test_terminator():
    print("[terminator v15.5 判停标准：θ=9.5 / 3 轮窗口极差<0.2 / 无轮数上限]")
    # 场景1：硬门禁失败 → rework
    st = terminator.RoundState(round_no=1)
    a, r = terminator.decide(st, True, "h1", 9.5)
    check("硬门禁失败→rework", a == "rework")
    # 场景2：连续两轮同一问题 → stalled
    st = terminator.RoundState(round_no=2, s_history=[6.0], rework_topics=["h1"])
    a, r = terminator.decide(st, True, "h1", 9.5)
    check("同问题两轮无改善→stalled", a == "stalled")
    # 场景3：硬门禁 + 清单不同 + 轮数很大 → 仍返工（v15.5 无轮数上限，不再 forced）
    st = terminator.RoundState(round_no=20, s_history=[6.0, 6.6], rework_topics=["h1", "h2"])
    a, r = terminator.decide(st, True, "h3", 9.5)
    check("硬门禁 r=20 清单不同→仍 rework（无轮数上限）", a == "rework")
    # 场景3b（v15.5b）：硬门禁连续 3 轮未消除（清单每轮变化）→ stalled（防无限返工）
    st = terminator.RoundState(round_no=3, s_history=[6.0, 6.1],
                               hard_gate_failures=[True, True],
                               rework_topics=["h1", "h2"])
    a, r = terminator.decide(st, True, "h3", 9.5)
    check("硬门禁连续3轮未消除→stalled（防无限返工）", a == "stalled")
    # 场景3c：硬门禁连续 2 轮（第 2 次失败）→ 仍 rework
    st = terminator.RoundState(round_no=2, s_history=[6.0],
                               hard_gate_failures=[True],
                               rework_topics=["h1"])
    a, r = terminator.decide(st, True, "h2", 9.5)
    check("硬门禁第2次失败→仍 rework", a == "rework")
    # 场景4：达标（S_r ≥ θ=9.5）→ converged
    st = terminator.RoundState(round_no=1, s_history=[9.5])
    a, r = terminator.decide(st, False, "", 9.5)
    check("S_r=9.5≥θ→converged", a == "converged")
    # 场景5：S_r=9.4（差 0.1 不到 9.5）→ 不收敛
    st = terminator.RoundState(round_no=2, s_history=[9.0, 9.4])
    a, r = terminator.decide(st, False, "", 9.5)
    check("S_r=9.4<θ→不收敛", a == "rework")
    # 场景6：3 轮窗口极差 < 0.2 → early_stop（增长枯竭）
    st = terminator.RoundState(round_no=3, s_history=[9.0, 9.1, 9.15])
    a, r = terminator.decide(st, False, "", 9.5)
    check("近3轮极差0.15<0.2→early_stop", a == "early_stop")
    # 场景7：3 轮窗口极差 ≥ 0.2 → 继续返工
    st = terminator.RoundState(round_no=3, s_history=[9.0, 9.1, 9.35])
    a, r = terminator.decide(st, False, "", 9.5)
    check("近3轮极差0.35≥0.2→继续 rework", a == "rework")
    # 场景8：仅 2 轮（窗口不满 3）→ 不触发枯竭，rework
    st = terminator.RoundState(round_no=2, s_history=[9.0, 9.1])
    a, r = terminator.decide(st, False, "", 9.5)
    check("2 轮不触发枯竭→rework", a == "rework")
    # 场景9：轮数很大仍不强制终止（v15.5 无轮数上限）——有增长就继续
    st = terminator.RoundState(round_no=30, s_history=[9.0, 9.1, 9.35])
    a, r = terminator.decide(st, False, "", 9.5)
    check("r=30 有增长→继续 rework（无轮数上限）", a == "rework")
    # 场景10：墙钟超预算 → forced（最高优先级，即使已达标）
    st = terminator.RoundState(round_no=1, s_history=[9.6])
    a, r = terminator.decide(st, False, "", 9.5, wall_elapsed_s=1700.0, wall_budget_s=1680.0)
    check("墙钟≥预算→forced（优先级最高）", a == "forced")
    # 场景11：震荡防抖——+0.19/-0.19 交替，3 轮窗口极差 0.38 仍继续；5 轮窗口若极差<0.2 会停
    st = terminator.RoundState(round_no=3, s_history=[9.0, 9.19, 9.0])
    a, r = terminator.decide(st, False, "", 9.5)
    check("震荡 ±0.19 极差 0.19<0.2→early_stop（3轮窗口防抖生效）", a == "early_stop")
    # 场景12：成本参数保留为审计兼容——不触发 forced（v15.4 已删成本 forced）
    st = terminator.RoundState(round_no=2, s_history=[8.0, 8.8])
    a, r = terminator.decide(st, False, "", 9.5, cost_so_far=0.2, budget_cap=0.15)
    check("成本超 cap 不再 forced（v15.4 删除）", a == "rework")

def test_calibration():
    print("[双验证者尺度校准]")
    rows = [
        {"subtask_id": "s1", "verifier": "A", "score": 9.0},
        {"subtask_id": "s2", "verifier": "A", "score": 7.0},
        {"subtask_id": "s3", "verifier": "B", "score": 5.0},
        {"subtask_id": "s4", "verifier": "B", "score": 4.0},
    ]
    out = calibration.aggregate(rows)
    check("S_r 在 0-10 范围", 0 <= out["S_r"] <= 10)
    check("四个子任务都有分", len(out["subtask_scores"]) == 4)
    # A 打 9/7 与 B 打 5/4 归一化后应相近（都分别是各自的高/低）
    check("归一化后 A 高分与 B 高分相近", abs(out["subtask_scores"]["s1"] - out["subtask_scores"]["s3"]) < 1.5)

def test_budget():
    print("[预算预检（v15.4 余额感知报告，不拒派）]")
    st = [{"id": "t1", "inputChars": "你好" * 1000}]
    out = budget.precheck(st, 0.15)
    check("恒为报告态（v15.4 不拒派）", out["status"] == "report")
    check("单子任务预估存在", len(out["perSubtask"]) == 1)
    out2 = budget.precheck([{"id": "t1", "inputChars": "x" * 100}] * 50, 0.001)
    check("超大任务仍只报告不拒派", out2["status"] == "report")
    check("预估金额有值", out2["estimatedCny"] >= 0)

def main():
    test_terminator()
    test_calibration()
    test_budget()
    print()
    if FAILED:
        print(f"❌ 失败: {FAILED}")
        sys.exit(1)
    print("✅ 全部通过")

if __name__ == "__main__":
    main()
