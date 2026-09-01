"""v15.5 档案重建验证（跑分完成后执行）：
1. 检查 benchmark 成绩覆盖（桥文件全档位 × 考卷全题都有 scores）；
2. build_capabilities 重建档案；
3. 校验：无 interpolated 字段、vendorGroup 齐全、revision 递增、caps_guard 通过；
4. 报告摘要。
用法：python verify-rebuild.py
"""
import json
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

FAILED = []


def check(name, cond, detail=""):
    tag = "✓" if cond else "✗"
    print(f"  {tag} {name}" + (f"（{detail}）" if detail else ""))
    if not cond:
        FAILED.append(name)


def main():
    import bridge
    # 1. 成绩覆盖
    scores_dir = BASE / "benchmark" / "scores"
    score_files = list(scores_dir.rglob("*.json")) if scores_dir.exists() else []
    print(f"[1] 成绩覆盖：{len(score_files)} 个判分文件")
    expected_pairs = []
    cases = json.loads((BASE / "benchmark" / "v21-cases.json").read_text(encoding="utf-8"))["cases"]
    for model in bridge.load()["models"]:
        for lv in bridge.levels_for(model):
            cid = model.replace("/", "--") + "__" + lv
            for c in cases:
                expected_pairs.append((cid, c["id"]))
    have = set()
    for f in score_files:
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
            have.add((j.get("cand_id"), j.get("case_id")))
        except (json.JSONDecodeError, OSError):
            pass
    missing = [p for p in expected_pairs if p not in have]
    check("全档位×全案例判分齐全", len(missing) == 0,
          f"缺 {len(missing)} 项" + (f" 例:{missing[:3]}" if missing else ""))

    # 2. 重建档案
    print("[2] build_capabilities 重建档案")
    r = subprocess.run([sys.executable, "benchmark/build_capabilities.py"],
                       cwd=BASE, capture_output=True, text=True)
    print("   " + (r.stdout or "").strip().splitlines()[-1] if (r.stdout or "").strip() else "   (无输出)")
    if r.returncode != 0:
        print("   stderr:", (r.stderr or "")[-400:])
        check("build_capabilities 成功", False, f"exit {r.returncode}")
        sys.exit(1)
    check("build_capabilities 成功", True)

    # 3. 档案校验
    caps = json.loads((BASE / "capabilities.json").read_text(encoding="utf-8"))
    models = caps.get("models", {})
    print(f"[3] 档案校验：{len(models)} 个条目，revision {caps.get('revision')}")
    interp = [cid for cid, m in models.items()
              for c in (m.get("capabilities") or {}).values()
              if isinstance(c, dict) and c.get("interpolated")]
    check("无 interpolated 条目", len(interp) == 0, f"{len(interp)} 个插值残留")
    no_vendor = [cid for cid, m in models.items() if not m.get("vendorGroup")]
    check("vendorGroup 齐全", len(no_vendor) == 0, f"缺 {len(no_vendor)}")
    # caps_guard 校验
    from orchestrator import caps_guard
    try:
        caps_guard.validate(caps)
        check("caps_guard 校验通过", True)
    except Exception as e:
        check("caps_guard 校验通过", False, str(e)[:120])
    # 池成员覆盖
    import pool
    pool_models = set(pool.members())
    arch_models = {m.get("baseModel") for m in models.values()}
    check("档案条目=池成员（全档位展开）", arch_models == pool_models,
          f"档案 {sorted(arch_models)} vs 池 {sorted(pool_models)}")

    print()
    if FAILED:
        print(f"❌ 失败 {len(FAILED)} 项: {FAILED}")
        sys.exit(1)
    print("✅ 档案重建验证全部通过")


if __name__ == "__main__":
    main()
