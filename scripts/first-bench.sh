#!/usr/bin/env bash
# Run the first benchmark + capability ingest workflow.
#
# Usage:
#   ./scripts/first-bench.sh [WORKSPACE_DIR]
#
# Defaults to the current directory. Runs the full benchmark pipeline and
# populates the capability archive. Takes 1-2 hours, ~$15-25 in API costs.

set -euo pipefail

WORKSPACE="${1:-.}"
cd "$WORKSPACE"

echo "=== First-Bench Workflow ==="
echo "Workspace: $PWD"
echo

# Step 1: Initialize workspace if needed
if [[ ! -f "capabilities.json" ]]; then
  echo "[1/4] Bootstrapping workspace..."
  python -m scripts.bootstrap_capabilities .
else
  echo "[1/4] Workspace already initialized (capabilities.json exists)"
fi
echo

# Step 2: Run benchmark
echo "[2/4] Running benchmark (1-2 hours, \$15-25)..."
python -m benchmark.bench.runner \
  --cases benchmark/v21-cases.json \
  --output benchmark/scores/ \
  --workers 1   # start conservative; bump after first run
echo

# Step 3: Preview the pending diff
echo "[3/4] Generating capability ingest diff (preview)..."
python -m benchmark.capability_ingest --diff
echo

# Step 4: Apply (requires manual confirmation)
echo "[4/4] Apply? Inspect the diff above. If it looks correct, run:"
echo "  python -m benchmark.capability_ingest --apply"
echo
echo "After applying, you can run your first council:"
echo "  python -m orchestrator.council_v14 --task 'Your question' --tier fast"