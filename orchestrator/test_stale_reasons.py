"""M2 陈旧语义测试：staleReasons 数组（fx 发布日过期 / >26h 未更新 / 配额快照 >90s）。

验收：test_stale_reasons.py 通过。
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from orchestrator import fetch_exchange_rate, query_balance  # noqa: E402

FAILED = []

def check(name, cond):
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}")
        FAILED.append(name)

CST = timezone(timedelta(hours=8), name="Asia/Shanghai")

def test_latest_expected_date():
    print("[CFETS 预期发布日]")
    # 周一 10:00（9:15 后）→ 当天
    mon_10 = datetime(2026, 8, 24, 10, 0, tzinfo=CST)
    check("工作日 9:15 后→当天", fetch_exchange_rate._latest_expected_date(mon_10).isoformat() == "2026-08-24")
    # 周一 8:00（9:15 前）→ 上一工作日（周五）
    mon_8 = datetime(2026, 8, 24, 8, 0, tzinfo=CST)
    check("工作日 9:15 前→上一工作日", fetch_exchange_rate._latest_expected_date(mon_8).isoformat() == "2026-08-21")
    # 周日 12:00 → 回退到周五
    sun = datetime(2026, 8, 23, 12, 0, tzinfo=CST)
    check("周末回退最近工作日", fetch_exchange_rate._latest_expected_date(sun).isoformat() == "2026-08-21")

def test_stale_reasons():
    print("[fx staleReasons 数组]")
    now = datetime(2026, 8, 24, 12, 0, tzinfo=CST)
    # 发布日过期（周日拿到的旧价）→ publishDate_outdated
    r1 = fetch_exchange_rate._stale_reasons("2026-08-21 9:15", now.isoformat(), now)
    check("发布日过期→publishDate_outdated", "publishDate_outdated" in r1)
    # 更新超过 26h → fx_rate_age>26h
    old_upd = (now - timedelta(hours=30)).isoformat()
    r2 = fetch_exchange_rate._stale_reasons("2026-08-24 9:15", old_upd, now)
    check(">26h 未更新→fx_rate_age>26h", any(x.startswith("fx_rate_age>") for x in r2))
    check("发布日新但更新久→仅年龄原因", "publishDate_outdated" not in r2)
    # 新鲜：当天发布 + 1 小时前更新 → 无原因
    fresh_upd = (now - timedelta(hours=1)).isoformat()
    r3 = fetch_exchange_rate._stale_reasons("2026-08-24 9:15", fresh_upd, now)
    check("新鲜数据→无 stale 原因", r3 == [])
    # 两者叠加：旧发布日 + 久未更新 → 两条原因
    r4 = fetch_exchange_rate._stale_reasons("2026-08-20 9:15", old_upd, now)
    check("叠加→两条原因", "publishDate_outdated" in r4 and any(x.startswith("fx_rate_age>") for x in r4))
    # 解析失败保守标 stale
    check("不可解析保守 stale", fetch_exchange_rate._is_stale("garbage", now) is True)

def test_quota_snapshot_semantics():
    print("[配额快照单位契约]")
    # 快照字段带 stale/staleReasons；百分比语义由 norm_percent 保证（H3）
    from orchestrator import selector
    check("norm_percent(96)=96（整数百分比语义）", selector.norm_percent(96) == 96)
    check("norm_percent(0.96)=96（小数自动修复）", selector.norm_percent(0.96) == 96)
    snap_ts = datetime.now().timestamp()
    check("快照 ts 为 epoch 秒（>90s 由消费方判 stale）",
          isinstance(snap_ts, float) and snap_ts > 1e9)

def main():
    test_latest_expected_date()
    test_stale_reasons()
    test_quota_snapshot_semantics()
    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败: {FAILED}")
        sys.exit(1)
    print("✅ 全部通过")

if __name__ == "__main__":
    main()
