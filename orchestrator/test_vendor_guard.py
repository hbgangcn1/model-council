"""vendor 数硬校验测试（v15.10）：<N 抛错阻断，≥N 放行。

验收：
- 2 家场景必抛 VendorMinError（含完整文案：实测 N / 阈值 / 当前 vendorGroups / 修复建议）；
- 3 家场景放行；
- 边界：0/1/2 抛错，3/4 放行；
- vendorGroup 字段缺失走 provider 推断（与 council_v14._vendor_of_cand 同规则）；
- identityUnknown / unstable 候选不计入（与 avail_vendors 口径一致）；
- min_vendors 参数化（min=4 时 3 家也抛错）。

全离线，无副作用：直接构造 caps dict，不触碰 capabilities.json / params 文件。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from orchestrator import vendor_guard  # noqa: E402

FAILED = []


def check(name, cond):
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}")
        FAILED.append(name)


def _cand(vendor_group: str = None, provider: str = None, *,
           base: str = "m", thinking: str = "off",
           stable: bool = True, identity_unknown: bool = False) -> dict:
    """构造单条 candidate；vendorGroup/provider 可独立测试缺失推断路径。"""
    out = {"baseModel": base, "thinking": thinking, "stable": stable}
    if vendor_group is not None:
        out["vendorGroup"] = vendor_group
    if provider is not None:
        out["provider"] = provider
    if identity_unknown:
        out["identityUnknown"] = True
    return out


def test_collect_vendor_groups_three_vendors():
    print("[collect_vendor_groups 3 家 → 3]")
    caps = {"models": {
        "d1__off": _cand("deepseek", "deepseek-official"),
        "m1__off": _cand("minimax", "minimax-cn"),
        "z1__off": _cand("zai", "zai-cn"),
    }}
    groups = vendor_guard.collect_vendor_groups(caps)
    check("3 家去重", groups == {"deepseek", "minimax", "zai"})
    check("同 vendor 不同 candidate 只算一次",
          len(groups) == 3 and "deepseek" in groups)


def test_collect_vendor_groups_provider_inference():
    print("[collect_vendor_groups provider 推断（缺 vendorGroup）]")
    # 档案未填 vendorGroup，应按 provider 推断
    caps = {"models": {
        "d1__off": _cand(vendor_group=None, provider="deepseek-official"),
        "m1__off": _cand(vendor_group=None, provider="minimax-cn"),
        "z1__off": _cand(vendor_group=None, provider="z.ai"),  # 另一常见拼写
    }}
    groups = vendor_guard.collect_vendor_groups(caps)
    check("deepseek-official → deepseek", "deepseek" in groups)
    check("minimax-cn → minimax", "minimax" in groups)
    check("z.ai → zai（双拼写兼容）", "zai" in groups)


def test_collect_vendor_groups_eligibility_filter():
    print("[collect_vendor_groups eligibility 过滤]")
    caps = {"models": {
        "ok1__off": _cand("deepseek", "deepseek-official"),
        "ok2__off": _cand("minimax", "minimax-cn"),
        # unstable + identityUnknown 都不应计入
        "bad1__off": _cand("meta", "meta-cn", stable=False),
        "bad2__off": _cand("openai", "openai-cn", identity_unknown=True),
        "ok3__off": _cand("zai", "zai-cn"),
    }}
    groups = vendor_guard.collect_vendor_groups(caps)
    check("eligible 3 家计入", groups == {"deepseek", "minimax", "zai"})
    check("unstable 不计", "meta" not in groups)
    check("identityUnknown 不计", "openai" not in groups)


def test_collect_vendor_groups_empty_and_unknown():
    print("[collect_vendor_groups 空 / unknown]")
    check("空 caps → 空 set", vendor_guard.collect_vendor_groups({}) == set())
    check("models=None → 空 set",
          vendor_guard.collect_vendor_groups({"models": None}) == set())
    check("非 dict caps → 空 set",
          vendor_guard.collect_vendor_groups("nope") == set())
    # 全部 unknown vendorGroup → 空 set（unknown 字面不计入）
    # 显式 "unknown" 与空 provider 都推断为 "unknown"，去重 + 过滤后空 set。
    caps_unknown = {"models": {
        "u1__off": _cand(vendor_group="unknown"),
        "u2__off": {"baseModel": "u2", "thinking": "off"},  # 无 vendorGroup 无 provider → unknown
    }}
    check("全部 unknown → 空 set（不计入）",
          vendor_guard.collect_vendor_groups(caps_unknown) == set())


def test_assert_two_vendors_raises():
    print("[assert_vendor_min 2 家 → 抛 VendorMinError]")
    caps = {"models": {
        "d1__off": _cand("deepseek", "deepseek-official"),
        "m1__off": _cand("minimax", "minimax-cn"),
    }}
    try:
        vendor_guard.assert_vendor_min(caps, min_vendors=3, source="test")
        check("2 家应抛错", False)
    except vendor_guard.VendorMinError as e:
        msg = str(e)
        check("异常为 ValueError 子类",
              isinstance(e, ValueError))
        check("文案含实测数 (=2)", "= 2" in msg)
        check("文案含阈值 (=3)", "= 3" in msg or "3 家" in msg)
        check("文案含当前 vendorGroups",
              "deepseek" in msg and "minimax" in msg)
        check("文案含源标签 (test)", "（test）" in msg)
        check("文案含 ADR-002 引用", "ADR-002" in msg)
        check("文案含修复方式", "修复方式" in msg)
        check("文案含静默自评说明", "静默自评" in msg)
        check("文案含模型数",
              "2 条" in msg or "models: 2" in msg)


def test_assert_three_vendors_passes():
    print("[assert_vendor_min 3 家 → 放行]")
    caps = {"models": {
        "d1__off": _cand("deepseek", "deepseek-official"),
        "m1__off": _cand("minimax", "minimax-cn"),
        "z1__off": _cand("zai", "zai-cn"),
    }}
    n = vendor_guard.assert_vendor_min(caps, min_vendors=3, source="test")
    check("3 家放行，返回 3", n == 3)
    # 4 家放行（min_vendors=3）
    caps["models"]["o1__off"] = _cand("openai", "openai-cn")
    n4 = vendor_guard.assert_vendor_min(caps, min_vendors=3, source="test")
    check("4 家放行，返回 4", n4 == 4)


def test_assert_boundary():
    print("[assert_vendor_min 边界 0/1/2 抛，3/4 放]")
    for n_vendors in (0, 1, 2):
        caps = {"models": {
            f"v{i}__off": _cand(f"vendor{i}", f"provider-{i}")
            for i in range(n_vendors)
        }}
        try:
            vendor_guard.assert_vendor_min(caps, min_vendors=3, source=f"test-{n_vendors}")
            check(f"{n_vendors} 家应抛错", False)
        except vendor_guard.VendorMinError:
            check(f"{n_vendors} 家抛 VendorMinError", True)
    # 3、4 家放行
    for n_vendors in (3, 4):
        caps = {"models": {
            f"v{i}__off": _cand(f"vendor{i}", f"provider-{i}")
            for i in range(n_vendors)
        }}
        try:
            vendor_guard.assert_vendor_min(caps, min_vendors=3,
                                             source=f"test-{n_vendors}")
            check(f"{n_vendors} 家放行", True)
        except vendor_guard.VendorMinError:
            check(f"{n_vendors} 家放行", False)


def test_assert_min_vendors_param():
    print("[assert_vendor_min min_vendors 参数化]")
    caps = {"models": {
        "d1__off": _cand("deepseek", "deepseek-official"),
        "m1__off": _cand("minimax", "minimax-cn"),
        "z1__off": _cand("zai", "zai-cn"),
    }}
    # min=4 时 3 家也抛错
    try:
        vendor_guard.assert_vendor_min(caps, min_vendors=4, source="test-param")
        check("min=4, 3 家应抛错", False)
    except vendor_guard.VendorMinError as e:
        check("min=4, 3 家抛错", "= 3 < 4" in str(e))
    # min=2 时 3 家放行
    n = vendor_guard.assert_vendor_min(caps, min_vendors=2, source="test-param")
    check("min=2, 3 家放行", n == 3)
    # min=1 时 3 家放行（极端兜底）
    n1 = vendor_guard.assert_vendor_min(caps, min_vendors=1, source="test-param")
    check("min=1, 3 家放行", n1 == 3)


def test_assert_only_eligible_filter():
    print("[assert_vendor_min only_eligible 过滤]")
    # 3 家但其中 1 家 unstable + 1 家 identityUnknown → 实际只有 1 家 → 抛错
    caps = {"models": {
        "ok__off": _cand("deepseek", "deepseek-official"),
        "unstable__off": _cand("minimax", "minimax-cn", stable=False),
        "unknown__off": _cand("zai", "zai-cn", identity_unknown=True),
    }}
    try:
        vendor_guard.assert_vendor_min(caps, min_vendors=3, source="test-elig")
        check("3 候选但只 1 家 eligible 应抛错", False)
    except vendor_guard.VendorMinError as e:
        check("只 1 家 eligible 抛错",
              "= 1" in str(e) and "minimax" not in str(e) and "zai" not in str(e))
    # only_eligible=False 关闭过滤：3 家都算（用于 debug / 排查）
    n = vendor_guard.assert_vendor_min(caps, min_vendors=3, source="test-elig",
                                        only_eligible=False)
    check("only_eligible=False 计入全部 3 家", n == 3)


def test_default_min_from_params():
    print("[assert_vendor_min 默认 min_vendors 从 params 读取]")
    caps = {"models": {
        "d1__off": _cand("deepseek", "deepseek-official"),
        "m1__off": _cand("minimax", "minimax-cn"),
        "z1__off": _cand("zai", "zai-cn"),
    }}
    # 不传 min_vendors，应走 params.load() 的 selection.minVendors（默认 3）
    n = vendor_guard.assert_vendor_min(caps, source="test-default")
    check("默认 min=3，3 家放行", n == 3)


def test_main():
    test_collect_vendor_groups_three_vendors()
    test_collect_vendor_groups_provider_inference()
    test_collect_vendor_groups_eligibility_filter()
    test_collect_vendor_groups_empty_and_unknown()
    test_assert_two_vendors_raises()
    test_assert_three_vendors_passes()
    test_assert_boundary()
    test_assert_min_vendors_param()
    test_assert_only_eligible_filter()
    test_default_min_from_params()
    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败: {FAILED}")
        sys.exit(1)
    print("✅ 全部通过")


if __name__ == "__main__":
    test_main()
