"""汇率每日更新（§7.1a）：CFETS 官方中间价 → exchange-rates.json + 每日历史。"""
import json
import re
import urllib.request
from datetime import datetime, date, timedelta
from pathlib import Path

try:  # 包内导入；JS 定时任务直接执行本脚本时退回 standalone
    from .config_loader import now_shanghai
except ImportError:
    from config_loader import now_shanghai  # type: ignore

BASE = Path(__file__).resolve().parent.parent  # council/
RATES_FILE = BASE / "exchange-rates.json"
HISTORY_FILE = BASE / "exchange-rates-history.jsonl"

# M2 陈旧语义：更新超过 26 小时（跨过一个工作日发布窗口）即标记过期
FX_STALE_MAX_HOURS = 26

CFETS_URL = "https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/fx/ccpr.json"
BACKUP_URL = "https://open.er-api.com/v6/latest/USD"  # v15.4 备用源（2026-08-24 实测可用：6.7291）
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
           "Referer": "https://www.chinamoney.com.cn/"}

def _latest_expected_date(now: datetime) -> date:
    """当前时刻「应该已经发布」的最新中间价日期。

    CFETS 每个工作日 9:15 发布当日中间价，周末/节假日不发。
    9:15 前 → 最新应为上一个工作日；9:15 后 → 今天（周末回退到最近工作日）。
    节假日未建模：节假日的预期日期按工作日算，会保守地标 stale（宁可提示，不隐藏旧价）。
    """
    ref = now.date()
    if (now.hour, now.minute) < (9, 15):
        ref -= timedelta(days=1)
    while ref.weekday() >= 5:
        ref -= timedelta(days=1)
    return ref

def _is_stale(publish_date: str, now: datetime) -> bool:
    """publishDate（如 '2026-08-24 9:15'）是否早于当前应已发布的最新价日期。"""
    try:
        pd = datetime.strptime(str(publish_date)[:10], "%Y-%m-%d").date()
    except Exception:
        return True  # 日期解析不出 → 保守标 stale
    return pd < _latest_expected_date(now)

def _stale_reasons(publish_date: str, updated_at: str | None, now: datetime) -> list:
    """M2：stale 具体原因数组（staleReasons: string[]），供 UI/指标/告警消费。"""
    reasons = []
    if _is_stale(publish_date, now):
        reasons.append("publishDate_outdated")
    if updated_at:
        try:
            upd = datetime.fromisoformat(updated_at)
            if upd.tzinfo is None:
                upd = upd.replace(tzinfo=now.tzinfo)
            if (now - upd).total_seconds() > FX_STALE_MAX_HOURS * 3600:
                reasons.append(f"fx_rate_age>{FX_STALE_MAX_HOURS}h")
        except Exception:
            reasons.append("updatedAt_unparsable")
    return reasons

def fetch_usd_cny() -> dict:
    """从 CFETS 抓 USD/CNY 中间价。返回 {rate, publishDate, source}。"""
    req = urllib.request.Request(CFETS_URL, headers=HEADERS, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    for rec in data.get("records", []):
        if rec.get("vrtEName") == "USD/CNY":
            return {"rate": float(rec["price"]),
                    "publishDate": data.get("data", {}).get("lastDate"),
                    "source": "CFETS"}
    raise ValueError("USD/CNY not found in CFETS response")


def fetch_usd_cny_backup() -> dict:
    """v15.4 备用源：open.er-api.com（市场价口径，无 key，2026-08-24 实测可用）。
    返回 {rate, publishDate, source}。"""
    req = urllib.request.Request(BACKUP_URL, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    rate = (data.get("rates") or {}).get("CNY")
    if not rate:
        raise ValueError("CNY not found in backup response")
    upd = data.get("time_last_update_utc") or ""
    return {"rate": float(rate),
            "publishDate": (upd[:10] if len(upd) >= 10 else None) or None,
            "source": "open.er-api.com"}


def _load_previous() -> dict:
    """最近一次成功更新的汇率（fallback 链最后一环）。"""
    if RATES_FILE.exists():
        try:
            prev = json.loads(RATES_FILE.read_text(encoding="utf-8"))
            if prev.get("usdToCny"):
                return prev
        except Exception:
            pass
    return {}


def update() -> dict:
    """v15.4 三级 fallback：主源 CFETS → 备用源 open.er-api.com → 最近一次成功汇率。
    前两级任一成功即正常更新；全失败则沿用最近值 + stale 标记（不再停机，只降级）。"""
    now = now_shanghai()
    chain = []          # v15.4：sourceChain 记录本次用了哪一级
    errors = []
    got = None
    # 第一级：CFETS 官方中间价
    try:
        got = fetch_usd_cny()
        chain.append(got["source"])
    except Exception as e:
        errors.append(f"CFETS: {str(e)[:120]}")
    # 第二级：备用公开源
    if got is None:
        try:
            got = fetch_usd_cny_backup()
            chain.append(got["source"])
            if got.get("publishDate") is None:
                got["publishDate"] = now.strftime("%Y-%m-%d")  # 备用源按当日计，stale 判定仍按交易日历
        except Exception as e:
            errors.append(f"backup: {str(e)[:120]}")
    if got is not None:
        rates = {"usdToCny": got["rate"], "publishDate": got["publishDate"],
                 "source": got["source"], "sourceChain": chain,
                 "fallbackUsed": got["source"] != "CFETS",
                 "updatedAt": now.isoformat(),
                 # 抓到旧日期（周末/节假日/未发布）也标 stale，不依赖抓取失败
                 "stale": _is_stale(got["publishDate"], now),
                 "staleReasons": _stale_reasons(got["publishDate"], now.isoformat(), now)}
        tmp = RATES_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rates, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(RATES_FILE)
        with HISTORY_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rates, ensure_ascii=False) + "\n")
        return rates
    # 第三级：全失败 → 最近一次成功汇率（the maintainer decided：单次 run 消耗仅几分钱，
    # 汇率波动影响微乎其微，出结果是终极使命——绝不停机，只降级+标记）
    prev = _load_previous()
    if prev:
        prev["stale"] = True
        prev["lastError"] = "; ".join(errors)[:300]
        prev["sourceChain"] = ["previous"]
        prev["fallbackUsed"] = True
        prev["staleReasons"] = list(dict.fromkeys(
            (prev.get("staleReasons") or []) + ["all_sources_failed"]))
        tmp = RATES_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(prev, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(RATES_FILE)
        return prev
    return {"error": "; ".join(errors)[:200], "updatedAt": now.isoformat(),
            "stale": True, "staleReasons": ["fetch_failed", "no_previous_rate"]}

if __name__ == "__main__":
    print(json.dumps(update(), ensure_ascii=False, indent=2))
