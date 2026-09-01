"""基准 v2.1 主入口：生成 / 判分 / 断点续传 / 0 分归因。
v15.4：成绩绑定 caseHash（题目内容版本）——换内容即新成绩，旧产物作废重跑；
判分走 score_objective_with_spec（SCORERS 未命中时用 scoringSpec 通用判分，金标晋升题）。

v15.6 改造：实时写 bench-progress.json（model/档位/案例 n/m），
host-bridge plugin 通过 /api/council/bench-progress 端点读这个文件，前端轮询显示进度。"""
import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench import config, cases, llm, scorer, cross_judge, summary as summary_mod

# v15.6 进度文件路径：写在 council 根目录（host-bridge plugin 通过 /api/council/bench-progress 读它）
# 用 PID 区分并发跑分（虽然 host-bridge plugin 同一时刻只 spawn 一个，但作为防御性写法）
_BENCH_PROGRESS_FILE = Path(__file__).resolve().parent.parent.parent / "bench-progress.json"


def _case_hash(case: dict) -> str:
    """v15.4：题目内容版本哈希（内容变了产物作废）。"""
    canonical = json.dumps(case, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

# ---------------- 进度 ----------------

def load_progress() -> dict:
    if config.PROGRESS_FILE.exists():
        try:
            return json.loads(config.PROGRESS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"runs": {}}

def save_progress(p):
    config.save_json(config.PROGRESS_FILE, p)

# ---------------- v15.6 bench-progress.json（实时进度，给 host-bridge plugin 前端轮询） ----------------

# 进程内单例：避免每次 case 完成后重新写整个文件
_progress_state: dict = {}


def _init_progress(total_jobs: int, candidates: list, cases_count: int) -> dict:
    """初始化 progress 文件。在 main() 开头调用一次。"""
    _progress_state.clear()
    _progress_state.update({
        "runId": str(uuid.uuid4())[:8],
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "updatedAt": _progress_state.get("updatedAt", ""),
        "finishedAt": None,
        "status": "running",
        "pid": os.getpid(),
        "phase": "generate",  # generate / score / done
        "candidates": candidates,
        "totalCases": cases_count,
        "totalCands": len(candidates),
        "totalJobs": total_jobs,
        "doneCount": 0,
        "currentModel": candidates[0].split("__")[0] if candidates else "",
        "currentThinking": "__".join(candidates[0].split("__")[1:]) if candidates and "__" in candidates[0] else "",
        "currentCase": "",
        "lastDone": None,  # {model, case, status, note, elapsed_s}
        "errors": [],  # 最近 10 条错误（截断保留）
    })
    _write_progress()
    return _progress_state


def _write_progress():
    """原子写 progress 文件。多次调用安全。"""
    _progress_state["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
    try:
        tmp = _BENCH_PROGRESS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_progress_state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, _BENCH_PROGRESS_FILE)
    except Exception:
        pass  # 进度写失败不阻断跑分


def update_progress_running(model: str, thinking: str, case_id: str):
    """在 _gen_one 进入时调用，记录"正在跑"什么。"""
    _progress_state["currentModel"] = model
    _progress_state["currentThinking"] = thinking
    _progress_state["currentCase"] = case_id
    _write_progress()


def update_progress_done(model: str, case_id: str, status: str, note: str = "", elapsed_s: float = 0.0):
    """在 _gen_one 完成时调用。doneCount++，记录最后完成项。"""
    _progress_state["doneCount"] += 1
    _progress_state["lastDone"] = {
        "model": model, "case": case_id, "status": status,
        "note": note[:120], "elapsed_s": round(elapsed_s, 1),
    }
    if status not in ("done", "skipped") and note:
        errs = _progress_state.get("errors") or []
        errs.append({"model": model, "case": case_id, "note": note[:200]})
        _progress_state["errors"] = errs[-10:]  # 仅保留最近 10 条
    _write_progress()


def finish_progress(status: str = "completed", phase: str = "done"):
    """在 main() 退出前调用。"""
    _progress_state["status"] = status
    _progress_state["phase"] = phase
    _progress_state["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
    _progress_state["currentCase"] = ""  # 跑分结束，清空"正在跑"
    _write_progress()

# ---------------- 生成 ----------------

def resp_path(cand_id: str, case_id: str):
    return config.RESPONSES_DIR / cand_id / f"{case_id}.json"

def load_resp(cand_id: str, case_id: str):
    p = resp_path(cand_id, case_id)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None

def _gen_one(cand_model, cand_thinking, case, args):
    """单个（候选×题）生成任务（线程安全：产物原子写）。"""
    cid = config.cand_id(cand_model, cand_thinking)
    case_id = case["id"]
    case_hash = _case_hash(case)
    existing = load_resp(cid, case_id)
    if args.resume and existing and existing.get("status") == "done" \
            and existing.get("caseHash") == case_hash:
        return f"[SKIP] {cid} / {case_id}"
    if args.only_failed and existing and existing.get("status") != "failed":
        return f"[SKIP] {cid} / {case_id}"
    prompt = cases.build_prompt(case)
    use_tools = cases.needs_tools(case)
    sandbox_root = cases.sandbox_root_for(case)
    retries = (existing or {}).get("retries", 0)

    # v15.6：进入 case 时记录"正在跑什么"（给前端轮询用）
    update_progress_running(cand_model, cand_thinking, case_id)
    t0 = time.time()

    try:
        text, meta = llm.run_case(cand_model, cand_thinking, prompt,
                                  use_tools=use_tools, sandbox_root=sandbox_root)
        status = "done"
        note = ""
        if meta.get("finish_reason") in ("length", "max_tokens"):
            status = "failed"
            note = f"finish_reason={meta['finish_reason']}（截断，加大 max_tokens 重试）"
    except Exception as e:
        text, meta, status, note = "", {}, "failed", str(e)[:300]
    if status == "failed" and retries >= 2:
        status = "give_up"
    record = {"cand_id": cid, "model": cand_model, "thinking": cand_thinking,
              "thinkingWire": config.thinking_param(cand_model, cand_thinking),  # v15.5：wire 参数落盘（数据溯源，问题 20）
              "maxTokens": config.max_tokens_for(cand_model),
              "case_id": case_id, "dimension": case["dimension"],
              "caseHash": case_hash,
              "prompt": prompt, "text": text, "meta": meta,
              "status": status, "note": note,
              "retries": retries + (1 if status == "failed" else 0),
              "ts": time.time()}
    (resp_path(cid, case_id).parent).mkdir(parents=True, exist_ok=True)
    config.save_json(resp_path(cid, case_id), record)

    # v15.6：完成时更新进度（doneCount++，记录 lastDone / errors）
    update_progress_done(cand_model, case_id, status, note, time.time() - t0)

    return f"[GEN] {cid} / {case_id} -> {status} ({note[:60]})"

def generate(args, case_list, cand_list, workers: int = 3):
    """v15.6 默认 3 worker 并发（sandbox._calls 已改 threading.local 隔离，修复 race condition）。

    之前默认 3 并发，但 tool-use case 走 host-bridge 时 3 worker 并发出现 race condition：
    典型症状是部分 case 产物的 text_len=0 tool_calls=0（model 调 tool 但产物空）。
    可能根因：sandbox._calls 全局计数被并发 worker 互相污染 + pi-ai streamSimple 可能有共享状态。
    v15.5 时代无 tool-use case，未暴露此问题。

    race condition 排查前先用 workers=1（顺序），跑 5 档×37 case=185 case 约 8-10 分钟。
    后续排查方向：threading.local() 隔离 sandbox 计数、test pi-ai 并发安全性、host-bridge
    端 ctx.llm.stream() 是否有共享状态。详见 AGENTS.md "Council benchmark 并发 race condition"。
    """
    jobs = [(m, t, c) for m, t in cand_list for c in case_list]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_gen_one, m, t, c, args) for m, t, c in jobs]
        done_count = 0
        for fut in as_completed(futs):
            done_count += 1
            try:
                msg = fut.result()
            except Exception as e:
                msg = f"[ERR] {type(e).__name__}: {str(e)[:120]}"
            print(f"{msg}  ({done_count}/{len(jobs)})", flush=True)

# ---------------- 判分 ----------------

def score_path(cand_id: str, case_id: str):
    return config.SCORES_DIR / cand_id / f"{case_id}.json"

def load_score(cand_id: str, case_id: str):
    p = score_path(cand_id, case_id)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None

def score(args, case_list, cand_list):
    for cand_model, cand_thinking in cand_list:
        cid = config.cand_id(cand_model, cand_thinking)
        for case in case_list:
            case_id = case["id"]
            # v15.4：成绩绑定题目内容版本——caseHash 不符时旧成绩作废（换内容=新成绩）
            case_hash = _case_hash(case)
            existing = load_score(cid, case_id)
            # v15.6：SKIP 逻辑增强——caseHash 一致 + verdict 真实（有分）才 SKIP。
            # 之前 race condition 跑 score 时把 verdict=empty 的空结果写入产物，
            # 下次跑 SKIP 时会"缓存"空结果，导致 glm-5.3-flash 4 档 T1/T2/T3 永远空分。
            # 修法：verdict="real" 且 score 不为 None 才视为已成功判分。
            if (args.resume and existing and existing.get("caseHash") == case_hash
                    and existing.get("verdict") == "real" and existing.get("score") is not None):
                continue
            rec = load_resp(cid, case_id)
            if not rec or rec.get("status") != "done":
                print(f"[SCORE] {cid} / {case_id}: skip (resp {rec and rec.get('status')})", flush=True)
                continue
            text = rec.get("text", "")
            if case_id in cross_judge.CROSS_CASES:
                result, note, judge_meta = cross_judge.score_case(case, text, cand_model)
            else:
                result, note, judge_meta = scorer.score_objective_with_spec(case, text, rec.get("meta", {}))
            out = {"cand_id": cid, "model": cand_model, "thinking": cand_thinking,
                   "case_id": case_id, "dimension": case["dimension"],
                   "caseHash": case_hash,
                   "score": result, "note": note,
                   "verdict": judge_meta.get("verdict", "real"),
                   "judge": judge_meta.get("judge"),
                   "ts": time.time()}
            (score_path(cid, case_id).parent).mkdir(parents=True, exist_ok=True)
            config.save_json(score_path(cid, case_id), out)
            print(f"[SCORE] {cid} / {case_id}: {result} ({note[:60]})", flush=True)

# ---------------- main ----------------

def parse_args():
    ap = argparse.ArgumentParser(description="Benchmark v2.1 runner")
    ap.add_argument("--phase", choices=["generate", "score", "all"], default="all")
    ap.add_argument("--fresh", action="store_true", help="全量重跑（忽略已有产物）")
    ap.add_argument("--only-failed", action="store_true", help="只补失败项")
    ap.add_argument("--candidates", help="逗号分隔候选子集，如 deepseek-v4-pro__off")
    ap.add_argument("--cases", help="逗号分隔题目子集，如 R1,C1")
    ap.add_argument("--summary-only", action="store_true", help="只汇总已判分数据")
    args = ap.parse_args()
    args.resume = not args.fresh
    return args

def main():
    args = parse_args()
    case_list = cases.load_cases()
    if args.cases:
        wanted = set(args.cases.split(","))
        case_list = [c for c in case_list if c["id"] in wanted]
    cand_list = config.CANDIDATES
    if args.candidates:
        wanted = set(args.candidates.split(","))
        cand_list = [(m, t) for m, t in cand_list if config.cand_id(m, t) in wanted]

    # v15.6：初始化 bench-progress.json（前端轮询用）
    cand_ids = [config.cand_id(m, t) for m, t in cand_list]
    total_jobs = sum(1 for _ in cand_list for _ in case_list)
    _init_progress(total_jobs=total_jobs, candidates=cand_ids, cases_count=len(case_list))

    if args.summary_only:
        summary_mod.main()
        finish_progress(status="completed", phase="summary_only")
        return
    try:
        if args.phase in ("generate", "all"):
            _progress_state["phase"] = "generate"
            _write_progress()
            generate(args, case_list, cand_list)
        if args.phase in ("score", "all"):
            _progress_state["phase"] = "score"
            _write_progress()
            score(args, case_list, cand_list)
            summary_mod.main()
        finish_progress(status="completed", phase="done")
    except Exception as e:
        finish_progress(status="failed", phase="error")
        raise

if __name__ == "__main__":
    main()
