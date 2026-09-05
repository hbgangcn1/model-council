"""vendor 数硬校验：跨厂商互验（ADR-002）是 Council 可靠性的核心硬假设。

v15.10（2026-09-05 跨厂商硬校验）：orchestrator 启动 / selector 加载 capabilities.json
时，去重 vendorGroup 数 < min_vendors 直接抛 VendorMinError 阻断，不再静默自评。

背景与设计：
- ADR-002 明确"不同 baseModel" ≠ "不同 vendor"——v4-pro / v4-flash 共享训练管线/微调流程，
  同 baseModel 不同 thinking 视为同厂商，不能跨厂商互验；
- 元评审实证：selector 跨厂商互验依赖 ≥3 家，少于 3 家会回退到同厂商互评（共享失败模式）
  → 静默自评，违背设计意图；启动期需 fail-fast 让用户立即可见；
- min_vendors 参数化（council-params.json 的 selection.minVendors，默认 3）：保留逃生口，
  默认与 ADR-002 一致；测试可降级但生产不建议。

调用点：
- selector.load_capabilities() 末尾：配置加载时即时校验；
- council_v14.run_council() 入口：用户面 fast-fail，错误文案完整可读；
- pytest 单元测试：直接构造 caps dict 验证抛错/放行。

兼容性：
- 仅 stable + 非 identityUnknown 的候选计入（与 council_v14.avail_vendors 口径一致，
  否则不可用候选虚增 vendor 数造成假阳性放行）；
- vendorGroup 字段缺失时按 provider 推断（与 council_v14._vendor_of_cand 同规则），
  兼容协议对齐前的旧档案。
"""
from typing import Iterable


class VendorMinError(ValueError):
    """vendor 数硬校验失败异常。ValueError 子类，pytest.raises(ValueError) 可捕获。"""


# 与 council_v14._vendor_of_cand 一致：档案 vendorGroup 优先，缺失时按 provider 推断。
# 协议对齐前旧档案靠这张推断表不致少算 vendor 数（推断会回到默认值）。
_PROVIDER_TO_VENDOR = {
    "deepseek-official": "deepseek",
    "minimax-cn": "minimax",
    "zai-cn": "zai",
    "z.ai": "zai",
    "meta-cn": "meta",
    "openai-cn": "openai",
    "anthropic-cn": "anthropic",
}


def _infer_vendor_group(cand: dict) -> str:
    """档案 vendorGroup 优先；缺失按 provider 推断；都缺失返回 'unknown'（不计入）。"""
    vg = cand.get("vendorGroup")
    if isinstance(vg, str) and vg.strip():
        return vg.strip()
    p = (cand.get("provider") or "")
    if not isinstance(p, str):
        return "unknown"
    p = p.strip()
    if p in _PROVIDER_TO_VENDOR:
        return _PROVIDER_TO_VENDOR[p]
    return p or "unknown"


def collect_vendor_groups(caps: dict, *, only_eligible: bool = True) -> set:
    """从 capabilities 档案提取去重后的 vendorGroup 集合。

    only_eligible=True（默认）只计 stable=True 且 identityUnknown!=True 的候选，
    与 council_v14.run_council 的 avail_vendors 口径一致——不可用候选不应虚增 vendor 数。

    返回值为 set[str]，去重后不含 'unknown'（无法归类的不计）。"""
    if not isinstance(caps, dict):
        return set()
    models = caps.get("models")
    if not isinstance(models, dict) or not models:
        return set()
    out = set()
    for cand in models.values():
        if not isinstance(cand, dict):
            continue
        if only_eligible:
            if cand.get("identityUnknown"):
                continue
            if not cand.get("stable", True):
                continue
        vg = _infer_vendor_group(cand)
        if vg and vg != "unknown":
            out.add(vg)
    return out


def _format_error(actual: int, min_vendors: int, vendor_groups: Iterable[str],
                  model_count: int, source: str) -> str:
    """错误文案：含实测数 / 阈值 / 当前 vendorGroups / 候选池规模 / 修复建议。"""
    vg_list = sorted(vendor_groups)
    return (
        f"vendor 数硬校验失败（{source}）：去重后 vendorGroup 数 = {actual} < {min_vendors} "
        f"（cross-verification hard assumption，见 ADR-002）。"
        f"跨厂商互验需要至少 {min_vendors} 家供应商，少于 {min_vendors} 会导致 selector 静默自评："
        f"同厂商内 baseModel 互评 → 共享训练管线/微调流程 → 相关失败模式，违背 ADR-002 设计意图。"
        f"当前候选池 vendorGroups: {vg_list or '[]'}；"
        f"当前候选池 models: {model_count} 条。"
        f"修复方式："
        f"(1) 在 capabilities.json 增加第 {min_vendors} 家 vendor 的 model@thinking 条目"
        f"（如 vendorGroup='meta' / 'openai' / 'anthropic' 等），"
        f"并通过 benchmark/capability_ingest.py 回填能力分；"
        f"(2) 临时绕过：调低 council-params.json 的 selection.minVendors"
        f"（仅本地/测试用，不推荐——生产 cross-verification 假设失效）。"
    )


def assert_vendor_min(caps: dict, min_vendors: int = None, *,
                       source: str = "selector",
                       only_eligible: bool = True) -> int:
    """校验 caps 中去重 vendorGroup 数 ≥ min_vendors。失败抛 VendorMinError。

    参数：
    - caps: capabilities.json 解析后的 dict（{models: {cid: cand}}）；非 dict 视为空；
    - min_vendors: 阈值；None 时从 params.py 的 selection.minVendors 读取（默认 3）；
    - source: 错误文案前缀标识（"selector" / "council" 等）；
    - only_eligible: 仅 stable + 非 identityUnknown 计入（默认 True）。

    返回：去重后的 vendorGroup 数（成功路径）。"""
    if min_vendors is None:
        try:
            # 避免循环依赖：params.py 不依赖 vendor_guard；此处走 params.get 软加载
            from . import params as _params
            min_vendors = int(_params.get(
                _params.load(), "selection.minVendors", 3))
        except Exception:
            min_vendors = 3
    min_vendors = max(1, int(min_vendors))
    groups = collect_vendor_groups(caps, only_eligible=only_eligible)
    actual = len(groups)
    if actual < min_vendors:
        model_count = len((caps or {}).get("models") or {}) if isinstance(caps, dict) else 0
        raise VendorMinError(_format_error(actual, min_vendors, groups, model_count, source))
    return actual
