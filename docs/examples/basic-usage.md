# Basic Usage Examples

## Setup

After cloning the repo and `pip install -e .`, run the first-run wizard
from the repo root (it checks the prebuilt data snapshot, credentials,
offline tests and a free dry-run — no API cost):

```bash
python scripts/first_run.py
```

The repo ships a prebuilt snapshot (`capabilities.json`: 18 model×thinking
entries, `benchmark/golden/golden-set.json`: 36 items), so the selector works
out of the box. If keys are missing the wizard tells you exactly what to do
(`export DEEPSEEK_API_KEY=...` or `first_run.py --init-credentials`).

Edit `council-params.json` to tune parameters (`python -m orchestrator.params
--show` to inspect). Provider credentials are read from (first found wins):
`MODEL_COUNCIL_CREDENTIALS` file > env vars > `~/.model-council/credentials`.

## First council run

The simplest invocation:

```bash
python -m orchestrator.council_v14 \
  --task "Should we use PostgreSQL or MongoDB for our analytics workload?" \
  --tier fast \
  --mode inline
```

This will:
1. Load `capabilities.json` (prebuilt 18-entry snapshot ships with the repo)
2. Decompose the task into 2-4 subtasks with dimension weights
3. For each subtask, let the selector pick a model by capability score
   (cross-vendor, no self-verify) and execute it
4. Have a different-vendor model verify each output
5. Synthesize the verified outputs into a final report

To replace the shipped scores with your own measurements, run the benchmark
(`./scripts/first-bench.sh`, 1-2h, ~$15-25) and ingest the results below.

## First benchmark run

```bash
# 1-2 hours, ~$15-25 in API costs
python -m benchmark.bench.runner \
  --cases benchmark/v21-cases.json \
  --output benchmark/scores/

# Then ingest the results into the capability archive
python -m benchmark.capability_ingest --diff    # preview
python -m benchmark.capability_ingest --apply   # apply (after manual review)
```

Now `capabilities.json` has real scores, and subsequent council runs will use
the selector properly.

## Inspect a previous run

```bash
# List recent runs
ls -lt runs/ | head

# Look at the most recent run
cd runs/2026-XX-XX_HH-MM-SS
cat task.md
cat report.md
```

A single run produces:
- `task.md` — the task as submitted (with `--facts` snapshot appended)
- `decisions.jsonl` — every model-selection decision (which model, why)
- `rounds.jsonl` — every round (verdicts, hard-gate hits, rework lists)
- `cost.jsonl` — every model call's estimated and actual cost
- `budget.jsonl` — pre-flight balance check
- `result.json` — final summary (status, rounds, cost, warnings)
- `report.md` — the synthesized report (Markdown)

## Dry run

To preview budget and subtask decomposition without actually calling models:

```bash
python -m orchestrator.council_v14 \
  --task "Review the Q4 architecture proposal" \
  --tier standard \
  --dry
```