"""runtime-feedback retention 测试（v15.10）：archive-feedback.py 行为与对账。

验收（v15.10 TASK-002）：
- 过期行被归档（按月分桶到 evals/archive/YYYY-MM.jsonl）；
- 新行保留在源文件；
- 归档前后行数严格对账，不一致拒绝执行且源文件不动；
- 默认 90 天，可自定义 retention_days；
- 多种 ts 格式（ISO 8601 / Z / epoch 毫秒 / epoch 秒）兼容；
- 解析失败 / 无 ts 字段的行保守保留（不归档）；
- 多次归档累加（不破坏已有归档）；
- dry-run 不动文件，--apply 落盘后行数对账一致；
- archive 目录自动创建。

测试通过 importlib.util 加载 scripts/archive-feedback.py（scripts 不在 pyproject
packages 内，避免污染 import 路径；保留"CLI + 库函数"双形态）。
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "archive-feedback.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("archive_feedback", SCRIPT)
    assert spec and spec.loader, f"无法加载 {SCRIPT}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def af():
    return _load_module()


def _row(model: str, ts: str, **extra) -> dict:
    return {"ts": ts, "model": model, "thinking": "off", "verifierScore": 8.0,
            "success": True, **extra}


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


# ============== 核心行为（TASK-002 验收点） ==============

def test_expiry_archived_by_month(tmp_path, af):
    """过期行（>90 天）按月分桶到 evals/archive/YYYY-MM.jsonl，新行保留。"""
    fb = tmp_path / "runtime-feedback.jsonl"
    ad = tmp_path / "archive"
    now = datetime.datetime(2026, 9, 5, 12, 0, 0, tzinfo=datetime.timezone.utc)
    rows = [
        _row("m1", "2026-01-15T10:00:00Z"),  # 203 天前，2026-01 桶
        _row("m2", "2026-03-20T10:00:00Z"),  # 169 天前，2026-03 桶
        _row("m3", "2026-06-01T10:00:00Z"),  # 96 天前，2026-06 桶（cutoff=06-07 12:00）
        _row("m4", "2026-08-01T10:00:00Z"),  # 35 天前，保留
    ]
    _write_rows(fb, rows)

    plan = af.archive(fb, ad, retention_days=90, now=now, apply=True)

    # 计划对账一致
    assert plan["total"] == 4
    assert plan["kept"] == 1
    assert plan["expired"] == 3
    assert plan["dry_run"] is False
    assert plan["by_month"] == {"2026-01": 1, "2026-03": 1, "2026-06": 1}

    # 归档文件按月分桶存在
    assert (ad / "2026-01.jsonl").exists()
    assert (ad / "2026-03.jsonl").exists()
    assert (ad / "2026-06.jsonl").exists()
    jan = _read_jsonl(ad / "2026-01.jsonl")
    mar = _read_jsonl(ad / "2026-03.jsonl")
    jun = _read_jsonl(ad / "2026-06.jsonl")
    assert [r["model"] for r in jan] == ["m1"]
    assert [r["model"] for r in mar] == ["m2"]
    assert [r["model"] for r in jun] == ["m3"]

    # 源文件只剩新行（m4）
    kept = _read_jsonl(fb)
    assert len(kept) == 1
    assert kept[0]["model"] == "m4"
    assert kept[0]["ts"] == "2026-08-01T10:00:00Z"


def test_dry_run_does_not_modify(tmp_path, af):
    """dry-run（默认）不修改源文件，不创建归档目录。"""
    fb = tmp_path / "runtime-feedback.jsonl"
    ad = tmp_path / "archive"
    now = datetime.datetime(2026, 9, 5, tzinfo=datetime.timezone.utc)
    rows = [_row("old", "2026-01-01T00:00:00Z"),
            _row("new", "2026-08-01T00:00:00Z")]
    _write_rows(fb, rows)
    src_bytes = fb.read_bytes()

    plan = af.archive(fb, ad, retention_days=90, now=now, apply=False)
    assert plan["dry_run"] is True
    assert plan["expired"] == 1
    assert plan["kept"] == 1
    # 源文件 byte-level 不变
    assert fb.read_bytes() == src_bytes
    # 归档目录不应创建
    assert not ad.exists()


def test_post_archive_count_mismatch_raises(tmp_path, af, monkeypatch):
    """写后归档行数对账不一致 → ArchiveError + 源文件不变 + 归档文件未污染。"""
    fb = tmp_path / "runtime-feedback.jsonl"
    ad = tmp_path / "archive"
    now = datetime.datetime(2026, 9, 5, tzinfo=datetime.timezone.utc)
    rows = [_row("m1", "2026-01-15T10:00:00Z"),
            _row("m2", "2026-03-20T10:00:00Z")]
    _write_rows(fb, rows)
    src_bytes = fb.read_bytes()

    # monkeypatch _count_jsonl_lines：让 archive_dir 下的 jsonl 文件返回错误行数
    # pre_count 被注入为 999，但 post_count 仍是真实值 → expected 999+1=1000 ≠ post=1 → 抛错
    real_count = af._count_jsonl_lines

    def fake_count(path):
        if "archive" in str(path) and path.suffix == ".jsonl":
            return 999  # 注入错误 pre/post count
        return real_count(path)

    monkeypatch.setattr(af, "_count_jsonl_lines", fake_count)

    with pytest.raises(af.ArchiveError, match="归档文件行数对账失败"):
        af.archive(fb, ad, retention_days=90, now=now, apply=True)

    # 源文件 byte-level 不变
    assert fb.read_bytes() == src_bytes


def test_os_replace_failure_keeps_source_intact(tmp_path, af, monkeypatch):
    """原子替换失败（os.replace 抛错）→ 异常向上抛，源文件 byte-level 不变。"""
    fb = tmp_path / "runtime-feedback.jsonl"
    ad = tmp_path / "archive"
    now = datetime.datetime(2026, 9, 5, tzinfo=datetime.timezone.utc)
    rows = [_row("m1", "2026-01-15T10:00:00Z"),
            _row("m2", "2026-08-01T00:00:00Z")]
    _write_rows(fb, rows)
    src_bytes = fb.read_bytes()

    def fail_replace(*a, **kw):
        raise OSError("simulated disk failure during atomic replace")
    monkeypatch.setattr("os.replace", fail_replace)

    with pytest.raises(OSError, match="simulated disk failure"):
        af.archive(fb, ad, retention_days=90, now=now, apply=True)

    # 源文件 byte-level 不变
    assert fb.read_bytes() == src_bytes


# ============== 边界与稳健性 ==============

def test_empty_file_noop(tmp_path, af):
    fb = tmp_path / "runtime-feedback.jsonl"
    ad = tmp_path / "archive"
    fb.write_text("", encoding="utf-8")
    plan = af.archive(fb, ad, retention_days=90, apply=False)
    assert plan["total"] == 0
    assert plan["kept"] == 0
    assert plan["expired"] == 0
    assert not ad.exists()


def test_missing_file_noop(tmp_path, af):
    fb = tmp_path / "nope.jsonl"
    ad = tmp_path / "archive"
    plan = af.archive(fb, ad, retention_days=90, apply=False)
    assert plan["total"] == 0
    assert plan["hint"]


def test_unparseable_and_no_ts_lines_kept(tmp_path, af):
    """JSON 解析失败 / 无 ts 字段的行保守保留（不归档）。"""
    fb = tmp_path / "runtime-feedback.jsonl"
    ad = tmp_path / "archive"
    now = datetime.datetime(2026, 9, 5, tzinfo=datetime.timezone.utc)
    with fb.open("w", encoding="utf-8") as f:
        f.write(json.dumps(_row("ok_old", "2026-01-01T00:00:00Z")) + "\n")
        f.write(json.dumps(_row("ok_new", "2026-08-01T00:00:00Z")) + "\n")
        f.write("{bad json line\n")  # unparseable
        f.write(json.dumps({"model": "no_ts", "verifierScore": 8.0}) + "\n")  # 无 ts
        f.write(json.dumps({"ts": "not-a-date", "model": "bad_ts"}) + "\n")  # ts 格式错
    plan = af.archive(fb, ad, retention_days=90, now=now, apply=True)

    assert plan["total"] == 5
    assert plan["expired"] == 1
    assert plan["kept"] == 4
    assert plan["unparseable"] == 1
    assert plan["no_ts"] == 2  # 无 ts + ts 格式错
    # 源文件行数：ok_new + unparseable + no_ts + bad_ts = 4（坏行 raw 保留无法 JSON 解析）
    with fb.open("r", encoding="utf-8") as f:
        kept_lines = [l for l in f if l.strip()]
    assert len(kept_lines) == 4
    raw = "".join(kept_lines)
    assert '"model": "ok_new"' in raw
    assert '"model": "no_ts"' in raw
    assert '"model": "bad_ts"' in raw
    assert "{bad json line" in raw
    # 归档文件只含 ok_old
    assert _read_jsonl(ad / "2026-01.jsonl")[0]["model"] == "ok_old"


def test_multiple_ts_formats(tmp_path, af):
    """多种 ts 格式兼容：ISO 8601 / Z 后缀 / epoch 毫秒 / epoch 秒。"""
    fb = tmp_path / "runtime-feedback.jsonl"
    ad = tmp_path / "archive"
    now = datetime.datetime(2026, 9, 5, tzinfo=datetime.timezone.utc)
    # 2026-01-15 epoch: 1768432800 秒；毫秒：1768432800000
    rows = [
        _row("iso_z", "2026-01-15T10:00:00Z"),
        _row("iso_offset", "2026-03-20T10:00:00+00:00"),
        _row("iso_naive", "2026-04-10T10:00:00"),  # 无时区，按 UTC 处理
        _row("epoch_ms", 1768432800000),
        _row("epoch_s", 1768432800),
        _row("new_iso_z", "2026-08-01T10:00:00Z"),
    ]
    _write_rows(fb, rows)
    plan = af.archive(fb, ad, retention_days=90, now=now, apply=True)
    # 5 个过期 + 1 新
    assert plan["expired"] == 5
    assert plan["kept"] == 1
    # epoch_s 和 epoch_ms 落在 2026-01 同一月（同一秒）
    assert plan["by_month"]["2026-01"] == 3  # iso_z + epoch_ms + epoch_s
    assert plan["by_month"]["2026-03"] == 1
    assert plan["by_month"]["2026-04"] == 1


def test_append_to_existing_archive(tmp_path, af):
    """多次归档累加，不破坏已有归档。"""
    fb = tmp_path / "runtime-feedback.jsonl"
    ad = tmp_path / "archive"
    # 第一次归档：2026-01 一行
    now1 = datetime.datetime(2026, 9, 5, tzinfo=datetime.timezone.utc)
    _write_rows(fb, [_row("first", "2026-01-15T10:00:00Z")])
    plan1 = af.archive(fb, ad, retention_days=90, now=now1, apply=True)
    assert plan1["expired"] == 1
    assert _read_jsonl(ad / "2026-01.jsonl")[0]["model"] == "first"

    # 第二次归档：换一个月的过期行（cutoff=2026-09-06 → 2026-08-15 过期，归 2026-08）
    now2 = datetime.datetime(2026, 12, 5, tzinfo=datetime.timezone.utc)
    _write_rows(fb, [_row("second", "2026-08-15T10:00:00Z")])
    plan2 = af.archive(fb, ad, retention_days=90, now=now2, apply=True)
    assert plan2["expired"] == 1
    # 2026-01.jsonl 仍 1 行（first 不动）
    assert len(_read_jsonl(ad / "2026-01.jsonl")) == 1
    # 新建 2026-08.jsonl 含 second
    assert (ad / "2026-08.jsonl").exists()
    assert len(_read_jsonl(ad / "2026-08.jsonl")) == 1
    assert _read_jsonl(ad / "2026-08.jsonl")[0]["model"] == "second"


def test_archive_dir_auto_created(tmp_path, af):
    fb = tmp_path / "runtime-feedback.jsonl"
    ad = tmp_path / "subdir" / "deep" / "archive"  # 多层不存在
    now = datetime.datetime(2026, 9, 5, tzinfo=datetime.timezone.utc)
    _write_rows(fb, [_row("m", "2026-01-15T10:00:00Z")])
    plan = af.archive(fb, ad, retention_days=90, now=now, apply=True)
    assert plan["expired"] == 1
    assert (ad / "2026-01.jsonl").exists()


def test_default_retention_90_days(tmp_path, af):
    """默认 retention=90 天；自定义 retention 生效。"""
    fb = tmp_path / "runtime-feedback.jsonl"
    ad = tmp_path / "archive"
    now = datetime.datetime(2026, 9, 5, tzinfo=datetime.timezone.utc)
    # 89 天前→保留；91 天前→过期（90 天临界外）
    rows = [_row("just_kept", "2026-06-08T10:00:00Z"),  # 89 天前
            _row("just_expired", "2026-06-06T10:00:00Z")]  # 91 天前
    _write_rows(fb, rows)

    # 默认 90
    plan = af.archive(fb, ad, now=now, apply=True)
    assert plan["kept"] == 1
    assert plan["expired"] == 1
    assert plan["retention_days"] == 90


def test_custom_retention_days(tmp_path, af):
    """retention_days=30 时，91 天前的也归档；89 天前保留。"""
    fb = tmp_path / "runtime-feedback.jsonl"
    ad = tmp_path / "archive"
    now = datetime.datetime(2026, 9, 5, tzinfo=datetime.timezone.utc)
    rows = [_row("kept_29", "2026-08-07T10:00:00Z"),  # 29 天前
            _row("expired_60", "2026-07-07T10:00:00Z")]  # 60 天前
    _write_rows(fb, rows)
    plan = af.archive(fb, ad, retention_days=30, now=now, apply=True)
    assert plan["kept"] == 1
    assert plan["expired"] == 1
    assert plan["retention_days"] == 30


def test_keep_preserves_original_lines(tmp_path, af):
    """保留行 raw 写回，源文件保留行的原始字节（包括非 ASCII / 自定义字段）保持。"""
    fb = tmp_path / "runtime-feedback.jsonl"
    ad = tmp_path / "archive"
    now = datetime.datetime(2026, 9, 5, tzinfo=datetime.timezone.utc)
    raw_new_line = json.dumps(
        {"ts": "2026-08-01T10:00:00Z", "model": "m1",
         "extra": "中文 / emoji 🦄", "n": 42},
        ensure_ascii=False)
    raw_old_line = json.dumps(
        {"ts": "2026-01-01T00:00:00Z", "model": "old"},
        ensure_ascii=False)
    fb.write_text(raw_new_line + "\n" + raw_old_line + "\n", encoding="utf-8")

    af.archive(fb, ad, retention_days=90, now=now, apply=True)
    assert fb.read_text(encoding="utf-8") == raw_new_line + "\n"
    assert "中文 / emoji 🦄" in fb.read_text(encoding="utf-8")


# ============== CLI 集成（subprocess） ==============

def test_cli_dry_run_exits_zero_no_changes(tmp_path):
    fb = tmp_path / "runtime-feedback.jsonl"
    ad = tmp_path / "archive"
    _write_rows(fb, [_row("old", "2026-01-01T00:00:00Z"),
                     _row("new", "2026-08-01T00:00:00Z")])
    src_bytes = fb.read_bytes()
    result = subprocess.run(
        [sys.executable, str(SCRIPT),
         "--feedback", str(fb),
         "--archive-dir", str(ad),
         "--now", "2026-09-05T00:00:00Z"],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0
    plan = json.loads(result.stdout)
    assert plan["dry_run"] is True
    assert plan["expired"] == 1
    assert plan["kept"] == 1
    assert "[DRY-RUN]" in result.stderr
    # 源文件不变
    assert fb.read_bytes() == src_bytes
    assert not ad.exists()


def test_cli_apply_exits_zero_archives(tmp_path):
    fb = tmp_path / "runtime-feedback.jsonl"
    ad = tmp_path / "archive"
    _write_rows(fb, [_row("old", "2026-01-01T00:00:00Z"),
                     _row("new", "2026-08-01T00:00:00Z")])
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--apply",
         "--feedback", str(fb),
         "--archive-dir", str(ad),
         "--now", "2026-09-05T00:00:00Z"],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"stderr={result.stderr}"
    plan = json.loads(result.stdout)
    assert plan["dry_run"] is False
    assert plan["applied_at_utc"]
    # 实际归档落盘
    assert (ad / "2026-01.jsonl").exists()
    assert len(_read_jsonl(fb)) == 1
