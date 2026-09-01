#!/usr/bin/env python3
"""audit_council_orchestrator.py — council 入口与模型来源审计。

目的：任何人在改 council / 分析 council 之前，**先跑这个脚本**，避免误判：
  - 误以为 presets.json 是 council 的"模型配置中心"
  - 误以为 council.py 是 council 的真正 orchestrator 入口

事实（v15.6+）：
  - host-bridge plugin 调的是 council_v14.py（不是 council.py）
  - council_v14.py 用 selector.select() 按 capabilities.json 能力分自动选模型
  - presets.json 只给 decomposer 一个角色分配 model；评审/验证/综合完全自动化

用法：
  python audit_council_orchestrator.py          # 打印完整审计报告
  python audit_council_orchestrator.py --json  # JSON 输出供其它脚本消费
"""
import json
import re
import sys
from pathlib import Path

COUNCIL_BASE = Path(__file__).resolve().parent
DSH_BASE = COUNCIL_BASE.parent
DSH_COUNCIL_PLUGIN = DSH_BASE / "profiles" / "web" / "node_modules" / "host-bridge plugin"


def find_spawn_targets():
    """扫描 host-bridge plugin/index.js 找所有 spawn / runPy 调用，提取 python 入口脚本名。
    返回相对 COUNCIL_BASE 的路径（如 'orchestrator/council_v14.py' 或 'benchmark/summary.py'）。"""
    index_js = DSH_COUNCIL_PLUGIN / 'index.js'
    if not index_js.exists():
        return []
    text = index_js.read_text(encoding='utf-8')
    targets = set()
    for m in re.finditer(r"(?:spawnPy|runPy)\s*\(\s*['\"]([^'\"]+\.py)['\"]", text):
        targets.add(m.group(1))
    return sorted(targets)


def resolve_path(tgt: str) -> Path:
    """把 spawn target 字符串解析为绝对路径。
    host-bridge plugin spawn 路径相对的是 COUNCIL_BASE（如 'orchestrator/council_v14.py'）。
    找不到时尝试常见子目录 fallback（benchmark/bench/ 等），避免误报"file not found"。"""
    name = Path(tgt).name
    candidates = [
        COUNCIL_BASE / tgt,
        COUNCIL_BASE / "orchestrator" / name,
        COUNCIL_BASE / "benchmark" / "bench" / name,
        COUNCIL_BASE / "orchestrator" / ("test_" + name),  # test_*.py 模式
    ]
    for p in candidates:
        if p.exists():
            return p
    return candidates[0]  # 让 caller 看到 exists=False


def find_model_sources_in_file(path: Path):
    """扫描指定 Python 文件，找模型来源（preset / selector / capability / bridge / hardcoded）。"""
    if not path.exists():
        return {"exists": False}
    text = path.read_text(encoding='utf-8')
    sources = []

    # 1. preset 引用（preset_config / preset.get / load_preset / presets.json）
    if re.search(r"load_preset|preset_config|preset\.json|preset\.get", text):
        # 进一步定位：preset 用于什么？
        preset_for_decomposer = bool(re.search(
            r"preset.*decomposer|decomposer.*preset|\.get\(\s*['\"]decomposer['\"]", text))
        preset_for_roles = bool(re.search(
            r"roles\s*=\s*preset\.get|roles\s*=\s*preset\[|preset\.get\(['\"]roles['\"]\s*\)", text))
        if preset_for_decomposer or preset_for_roles:
            sources.append({
                "source": "preset.json",
                "usage": "decomposer only" if preset_for_decomposer and not preset_for_roles else
                         "decomposer + roles (deprecated v15.6)" if preset_for_roles else "preset roles mapping",
            })

    # 2. selector.select() — v15.6 主路径
    if re.search(r"selector\.select\s*\(", text):
        sources.append({
            "source": "selector.select() + capabilities.json",
            "usage": "primary model selection (exec / verifier / synthesize)",
        })

    # 3. capabilities.json 直接读
    if re.search(r"capabilities\.json|load_capabilities", text):
        sources.append({
            "source": "capabilities.json",
            "usage": "model pool (caps = scored model × thinking tuples)",
        })

    # 4. host-bridge 引用
    if re.search(r"bridge_stream|/api/council/llm-stream|llm-pi-ai", text):
        sources.append({
            "source": "host-bridge /api/council/llm-stream bridge",
            "usage": "v15.6 L3: HTTP call to DSH, DSH internally invokes pi-ai",
        })

    # 5. 直接硬编码 model id（兜底）
    hardcoded = re.findall(r"['\"](?:glm-5\.\d|MiniMax-M\d|deepseek-v\d-\w+|kimi-k\d\.\d-code|qwen\d\.\d-\w+)['\"]", text)
    if hardcoded:
        sources.append({
            "source": "hardcoded model id",
            "usage": f"fallback / default (found: {sorted(set(hardcoded))[:5]})",
        })

    return {"exists": True, "sources": sources}


def is_trap_name(filename: str) -> bool:
    """判断文件名是否容易被误认为"council 入口"。
    trap 触发条件：文件名看起来像"orchestrator 入口"（如 council*.py / orchestrator.py / main.py / run.py），
    但实际不是 host-bridge plugin 实际 spawn 的脚本。
    """
    base = Path(filename).name
    # 真实入口白名单（host-bridge plugin 实际 spawn 的脚本）
    real_entries = {"council_v14.py"}  # 当前 v15.6 唯一真正的 council orchestrator
    if base in real_entries:
        return False
    # trap 模式
    trap_patterns = [
        r"^council\.py$",  # ← 最经典的 trap（早期版本，名字像入口）
        r"^council_legacy.*\.py$",
        r"^main\.py$",
        r"^orchestrator\.py$",
        r"^run\.py$",
    ]
    return any(re.match(p, base) for p in trap_patterns)


def main():
    targets = find_spawn_targets()

    # 同时列出 orchestrator 目录里所有 .py（看哪些是 trap）
    orchestrator_dir = COUNCIL_BASE / "orchestrator"
    all_py = sorted(p.name for p in orchestrator_dir.glob("*.py")) if orchestrator_dir.exists() else []

    report = {
        "dsh_council_spawn_targets": targets,
        "orchestrator_py_files": all_py,
        "actual_orchestrator": "council_v14.py" if "council_v14.py" in targets else "(unknown)",
        "per_file_model_sources": {},
        "warnings": [],
        "trap_files": [],  # 容易被误认为入口的 .py（仅匹配 trap 命名）
    }

    for tgt in targets:
        path = resolve_path(tgt)
        report["per_file_model_sources"][tgt] = find_model_sources_in_file(path)

    # 陷阱检查：哪些 .py 文件**不是** host-bridge plugin 入口但**名字像入口**容易被误认为？
    actual_entry_basenames = {Path(t).name for t in targets}
    for p in all_py:
        if p in actual_entry_basenames:
            continue  # 是真实入口
        if p == "__init__.py":
            continue
        if is_trap_name(p):
            src = find_model_sources_in_file(orchestrator_dir / p)
            report["trap_files"].append({
                "file": p,
                "model_sources": src.get("sources", []),
                "warning": f"⚠️ {p} 名字像入口但 host-bridge plugin 不调它——可能误导（{len(src.get('sources', []))} 处模型来源定义）"
            })

    # preset 误判检查
    presets_path = COUNCIL_BASE / "presets.json"
    if presets_path.exists():
        presets = json.loads(presets_path.read_text(encoding='utf-8'))
        roles = presets.get("roles", {})
        if len(roles) > 1:
            report["preset_misconception_warning"] = (
                f"presets.json 有 {len(roles)} 个角色映射（{list(roles.keys())}）。"
                f"v15.6 实际只有 'decomposer' 角色由 preset 决定，其它由 selector 自动选。"
                f"**不要修改 presets.json 让新模型参与评审**——正确做法是 capabilities.json + 跑分。"
            )

    # preset 误判检查
    if "preset.json" in [p.name for p in (COUNCIL_BASE / "*.json").glob("*.json")] + \
       [p.name for p in (COUNCIL_BASE / "**/*.json").glob("*.json")]:
        presets = json.loads((COUNCIL_BASE / "presets.json").read_text(encoding='utf-8'))
        roles = presets.get("roles", {})
        if len(roles) > 1:
            report["warnings"].append(
                f"⚠️ presets.json 有 {len(roles)} 个角色映射（{list(roles.keys())}）。"
                f"v15.6 实际只有 'decomposer' 角色由 preset 决定，其它角色由 selector 自动选。"
                f"presets.json 的 roles 字段是 v15.5 时代遗留，**不要修改**来让新模型参与评审！"
            )

    if "--json" in sys.argv:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return

    # 人类可读输出
    print("=" * 70)
    print(" Council Orchestrator 审计报告（v15.6）")
    print("=" * 70)
    print()
    print(f"📍 host-bridge plugin 实际 spawn 的 Python 入口:")
    for tgt in targets:
        print(f"   • {tgt}")
    print()
    print(f"🎯 真正的 orchestrator: {report['actual_orchestrator']}")
    print()
    print(f"📁 orchestrator/ 目录下所有 .py 文件（共 {len(all_py)} 个）:")
    actual_entry_basenames = {Path(t).name for t in targets}
    trap_basenames = {t['file'] for t in report['trap_files']}
    for p in all_py:
        marker = " ← 真正入口" if p in actual_entry_basenames else ""
        trap_marker = " 🪤 易误认为入口" if p in trap_basenames else ""
        print(f"   • {p}{marker}{trap_marker}")
    print()
    print("🔍 各入口的模型来源:")
    for tgt, src in report["per_file_model_sources"].items():
        print(f"   📄 {tgt}")
        if not src.get("exists"):
            print(f"      (file not found)")
            continue
        for s in src.get("sources", []):
            print(f"      → {s['source']}：{s['usage']}")
    print()
    if report["warnings"]:
        print("⚠️  警告:")
        for w in report["warnings"]:
            print(f"   {w}")
        print()

    if report["trap_files"]:
        print(f"🪤 易误判陷阱（{len(report['trap_files'])} 个文件 — 名字像入口但不是）：")
        for t in report["trap_files"]:
            print(f"   • {t['file']}")
            for s in t['model_sources']:
                print(f"      - {s['source']}：{s['usage']}")
        print()

    if "preset_misconception_warning" in report:
        print("🚨 presets.json 误判预警：")
        print(f"   {report['preset_misconception_warning']}")
        print()
    print("=" * 70)
    print(" 事实摘要（防止误判）：")
    print("=" * 70)
    print("• host-bridge plugin 调的是 council_v14.py，**不是** council.py")
    print("• council_v14.py 用 selector.select() 按 capabilities.json 自动选模型")
    print("• presets.json 的 roles 字段**只用于** decomposer 角色")
    print("• exec / verifier / synthesize 角色完全由 selector 自动按能力分选")
    print("• 让新模型参与评审的正确做法：host adds model → Council UI 加进池 → 跑分 →")
    print("  capabilities.json 自动写入 → selector 下次自动按能力分选中")
    print("• **不要**通过改 presets.json 来让新模型参与评审（v15.5 时代的做法，已废）")
    print("=" * 70)


if __name__ == "__main__":
    main()