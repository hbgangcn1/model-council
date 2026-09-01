"""golden_guard 契约测试（P2-3）。"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmark import golden_guard  # noqa: E402


def _item(iid, source="domain-private"):
    return {
        "id": iid, "dimension": "research", "task": f"任务{iid}", "answer": f"答案{iid}",
        "rubric": "rubric", "holdout": True,
        # v15.5 评卷考场契约字段
        "expectedScore": 9.5,
        "badAnswers": [{"label": "答非所问", "text": "无法确定", "expectedScore": 0.5}],
        "machineCheckable": int(iid[1:]) <= 2,  # 前 2 条为机器判分锚（占比 25% 保底）
        "provenance": {"author": "human", "curator": "the maintainer",
                       "createdAt": "2026-08-24", "lastReviewedAt": "2026-08-24",
                       "source": source, "excludedFromCandidateCorpus": True},
    }


def test_valid_doc_passes():
    doc = {"items": [_item(f"G{i}") for i in range(1, 9)]}
    assert golden_guard.check_doc(doc) == []


def test_missing_provenance():
    doc = {"items": [{"id": "G1"}]}
    problems = golden_guard.check_doc(doc)
    assert any("provenance" in p for p in problems)


def test_bad_source_enum():
    doc = {"items": [_item("G1", source="unknown")]}
    problems = golden_guard.check_doc(doc)
    assert any("source" in p for p in problems)


def test_hetero_ratio_minimum():
    # 8 条全是 public-generic → 异源 0% 违规
    doc = {"items": [_item(f"G{i}", source="public-generic") for i in range(1, 9)]}
    problems = golden_guard.check_doc(doc)
    assert any("异源" in p for p in problems)


def test_overlap_with_cases_detected():
    doc = {"items": [_item("G1")]}
    cases = {"cases": [{"prompt": "任务G1 长问题"}]}
    problems = golden_guard.check_doc(doc, cases)
    assert any("重合" in p for p in problems)


def test_real_golden_file():
    problems = golden_guard.check_file()
    assert problems == []


def test_real_golden_hash_consistency():
    doc = json.loads(golden_guard.GOLDEN.read_text(encoding="utf-8"))
    assert doc["contentHash"]
    assert doc["contentHash"] == golden_guard.items_hash(doc["items"])
