# Basic Usage Examples

## Setup

After cloning the repo and `pip install -e .`, create a working directory:

```bash
mkdir my-council-workspace
cd my-council-workspace
```

Initialize an empty capability archive and configuration:

```bash
python -m scripts.bootstrap_capabilities
# Creates ./capabilities.json (empty), ./council-params.json (from template)
```

Edit `council-params.json` to add your provider credentials. By default, the
LLM transport layer reads credentials from environment variables:

```bash
export DEEPSEEK_API_KEY="..."
export MINIMAX_CN_API_KEY="..."
export OPENROUTER_API_KEY="..."   # if using openrouter
```

## First council run

The simplest invocation:

```bash
python -m orchestrator.council_v14 \
  --task "Should we use PostgreSQL or MongoDB for our analytics workload?" \
  --tier fast \
  --mode inline
```

This will:
1. Load `capabilities.json` (empty initially, will log a warning)
2. Decompose the task into 2-3 subtasks
3. For each subtask, select a model from the pool (any model that has been
   benchmarked) and execute it
4. Have a different model verify each output
5. Synthesize the verified outputs into a final report

Since the capability archive is empty, the selector will fall back to
`defaults` (the lowest-cost model in the pool). To get useful selection, you
need to run the benchmark first.

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