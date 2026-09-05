#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Archive expired runtime-feedback.jsonl rows into per-month files (v15.10 retention).

每晚 update_capabilities 全文加载 evals/runtime-feedback.jsonl 融合能力档案——
无 retention 时按 24-50 行/天累积，三年后每晚多读几十 MB。

策略：
- 默认保留 90 天（retention_days=90，CLI 可覆盖）；
- 超过保留期的行按 ts 字段（fallback timestamp）的 YYYY-MM 月分桶追加到
  evals/archive/YYYY-MM.jsonl；
- 归档前后行数严格对账：原始总行数 = 保留行数 + 各月归档行数之和，
  对账不一致直接抛错，源文件保持不动；
- 默认 dry-run（--apply 才落盘），便于先看计划再批。

CLI:
  python scripts/archive-feedback.py                     # dry-run（默认）
  python scripts/archive-feedback.py --apply             # 实际归档
  python scripts/archive-feedback.py --retention-days 60 # 自定义保留期
  python scripts/archive-feedback.py --now 2026-09-05    # 测试用：固定"今天"
  python scripts/archive-feedback.py --feedback PATH     # 自定义文件路径
  python scripts/archive-feedback.py --archive-dir PATH  # 自定义归档目录

用法（建议加进日常运维链 §1 04:30 judge 之后、update_capabilities --apply 之前
或 02:00 auto_evolve 之前——把陈旧反馈先压缩，避免每晚融合多读历史数据）：
  python scripts/archive-feedback.py            # 先 dry-run 看板
  python scripts/archive-feedback.py --apply    # 确认无异常再实际归档
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_FEEDBACK = REPO / "evals" / "runtime-feedback.jsonl"
DEFAULT_ARCHIVE_DIR = REPO / "evals" / "archive"
DEFAULT_RETENTION_DAYS = 90


class ArchiveError(Exception):
    """归档对账/IO 异常：源文件保持不动（写前对账 + tmp+rename 原子写）。"""


def _parse_ts(row: dict) -> datetime.datetime | None:
    """解析行的 ts 字段；缺失或格式错返回 None（保守保留，不归档）。"""
    val = row.get("ts") or row.get("timestamp")
    if val is None:
        return None
    if isinstance(val, (int, float)):
        # epoch 秒（10 位）/毫秒（13 位）兼容
        try:
            if val > 1e12:
                return datetime.datetime.fromtimestamp(val / 1000, tz=datetime.timezone.utc)
            return datetime.datetime.fromtimestamp(val, tz=datetime.timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    s = str(val).strip()
    if not s:
        return None
    # ISO 8601（带 / 不带时区、Z 后缀都接受）
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except ValueError:
        return None


def _month_bucket(dt: datetime.datetime) -> str:
    """UTC 月桶（YYYY-MM），归档文件名按月切分。"""
    return dt.strftime("%Y-%m")


def _count_jsonl_lines(path: Path) -> int:
    """数 jsonl 文件非空行数（空行不计）。提取为模块函数便于 monkeypatch 触发对账失败路径。"""
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def _read_rows(path: Path) -> list[tuple[int, str, dict | None]]:
    """读 jsonl → [(line_no, raw_line, parsed_dict | None), ...]。解析失败 None。"""
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rows.append((i, line, json.loads(stripped)))
            except json.JSONDecodeError:
                rows.append((i, line, None))
    return rows


def archive(feedback_path: Path = DEFAULT_FEEDBACK,
            archive_dir: Path = DEFAULT_ARCHIVE_DIR,
            retention_days: int = DEFAULT_RETENTION_DAYS,
            now: datetime.datetime | None = None,
            apply: bool = False) -> dict:
    """归档超过 retention_days 的行到 per-month 归档文件。

    返回 dict: total / kept / expired / by_month / archived_to / dry_run / source_intact /
    cutoff_utc / retention_days / unparseable / no_ts / applied_at_utc。
    异常路径（ArchiveError）源文件保持不动——写前对账 + 写后行数验算 + tmp+rename 原子写。"""
    feedback_path = Path(feedback_path)
    archive_dir = Path(archive_dir)
    retention_days = int(retention_days)
    if retention_days < 1:
        raise ValueError(f"retention_days 必须 ≥ 1，当前={retention_days}")
    now = now or datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=retention_days)

    rows = _read_rows(feedback_path)
    total = len(rows)

    if total == 0:
        return {"total": 0, "kept": 0, "expired": 0, "by_month": {},
                "archived_to": [], "dry_run": not apply, "source_intact": True,
                "cutoff_utc": cutoff.isoformat(), "retention_days": retention_days,
                "unparseable": 0, "no_ts": 0,
                "hint": "feedback 文件不存在或为空，跳过归档"}

    keep_rows = []                       # 保留行（line_no, raw, parsed）
    expire_buckets = defaultdict(list)   # YYYY-MM → [(line_no, raw, parsed)]
    unparseable = 0                      # 解析失败的行
    no_ts = 0                            # 无 ts 字段的行（保守保留）

    for _line_no, raw, parsed in rows:
        if parsed is None:
            unparseable += 1
            keep_rows.append((_line_no, raw, parsed))
            continue
        ts = _parse_ts(parsed)
        if ts is None:
            no_ts += 1
            keep_rows.append((_line_no, raw, parsed))
            continue
        if ts < cutoff:
            expire_buckets[_month_bucket(ts)].append((_line_no, raw, parsed))
        else:
            keep_rows.append((_line_no, raw, parsed))

    expired = sum(len(v) for v in expire_buckets.values())
    kept = len(keep_rows)

    # 写前对账：total == kept + expired（双闭区间无遗漏）
    if total != kept + expired:
        raise ArchiveError(
            f"行数对账失败（写前）：total={total} != kept={kept} + expired={expired}；"
            f"unparseable={unparseable} no_ts={no_ts} "
            f"by_month={ {k: len(v) for k, v in expire_buckets.items()} }"
        )

    plan = {
        "total": total,
        "kept": kept,
        "expired": expired,
        "by_month": {k: len(v) for k, v in sorted(expire_buckets.items())},
        "archived_to": [str(archive_dir / f"{m}.jsonl") for m in sorted(expire_buckets)],
        "dry_run": not apply,
        "source_intact": True,
        "cutoff_utc": cutoff.isoformat(),
        "retention_days": retention_days,
        "unparseable": unparseable,
        "no_ts": no_ts,
    }

    if not apply:
        return plan

    # ---- 写：原子 + 对账（任何异常不污染源文件）----
    archive_dir.mkdir(parents=True, exist_ok=True)

    # 1) 追加各月归档文件（先读现有行数，写后验算）
    archive_pre_counts = {}
    for month in sorted(expire_buckets):
        ap = archive_dir / f"{month}.jsonl"
        archive_pre_counts[month] = _count_jsonl_lines(ap)

    for month, items in expire_buckets.items():
        ap = archive_dir / f"{month}.jsonl"
        with ap.open("a", encoding="utf-8") as f:
            for _line_no, raw, _parsed in items:
                f.write(raw if raw.endswith("\n") else raw + "\n")

    # 写后验算归档
    for month, items in expire_buckets.items():
        ap = archive_dir / f"{month}.jsonl"
        post = _count_jsonl_lines(ap)
        expected = archive_pre_counts[month] + len(items)
        if post != expected:
            raise ArchiveError(
                f"归档文件行数对账失败：{ap} 期望 {expected} 行，实测 {post} 行；"
                f"源文件保持不动。"
            )

    # 2) 写新源文件（tmp + rename 原子；保留行 raw 写回——保持原行序与字节）
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=".runtime-feedback.", suffix=".jsonl.tmp",
        dir=str(feedback_path.parent))
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            for _line_no, raw, _parsed in keep_rows:
                f.write(raw if raw.endswith("\n") else raw + "\n")
        # 写后立即验算行数
        post_kept = _count_jsonl_lines(Path(tmp_path))
        if post_kept != kept:
            raise ArchiveError(
                f"新源文件行数对账失败：期望 {kept} 行，实测 {post_kept} 行；"
                f"源文件保持不动。"
            )
        # 原子替换
        os.replace(tmp_path, feedback_path)
    except Exception:
        # 任何异常清理 tmp，不动源
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    plan["source_intact"] = True
    plan["applied_at_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return plan


def main():
    parser = argparse.ArgumentParser(
        description="归档过期的 runtime-feedback.jsonl 行到 evals/archive/YYYY-MM.jsonl。"
                    "默认 dry-run；--apply 落盘（写后行数严格对账）。",
    )
    parser.add_argument("--apply", action="store_true",
                        help="实际写入新源文件并追加归档（默认 dry-run）")
    parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS,
                        help=f"保留天数（默认 {DEFAULT_RETENTION_DAYS}）")
    parser.add_argument("--feedback", type=Path, default=DEFAULT_FEEDBACK,
                        help=f"feedback 文件路径（默认 {DEFAULT_FEEDBACK}）")
    parser.add_argument("--archive-dir", type=Path, default=DEFAULT_ARCHIVE_DIR,
                        help=f"归档目录（默认 {DEFAULT_ARCHIVE_DIR}）")
    parser.add_argument("--now", type=str, default=None,
                        help="测试用：固定 now（ISO 8601，UTC 优先）")
    args = parser.parse_args()

    now = None
    if args.now:
        s = args.now.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            now = datetime.datetime.fromisoformat(s)
        except ValueError as e:
            print(f"❌ --now 格式错：{e}（期望 ISO 8601，如 2026-09-05T00:00:00Z）",
                  file=sys.stderr)
            sys.exit(2)
        if now.tzinfo is None:
            now = now.replace(tzinfo=datetime.timezone.utc)

    try:
        plan = archive(args.feedback, args.archive_dir,
                       retention_days=args.retention_days,
                       now=now, apply=args.apply)
    except ArchiveError as e:
        print(f"❌ 归档失败：{e}", file=sys.stderr)
        sys.exit(2)
    except ValueError as e:
        print(f"❌ 参数错：{e}", file=sys.stderr)
        sys.exit(2)

    # 打印计划（json 格式便于机器读）
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    if plan["dry_run"]:
        print("\n[DRY-RUN] 加 --apply 才落盘", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
