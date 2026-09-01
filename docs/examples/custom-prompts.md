# Customizing Prompts

The decomposer, verifier, and synthesizer prompts are defined as Python
constants in `orchestrator/council_v14.py`:

```python
DECOMPOSE_PROMPT = "..."
VERIFY_PROMPT = "..."
SYNTH_PROMPT = "..."
FRESHNESS_RULES = "..."
```

You can override any of these by subclassing `council_v14.py` or by writing a
custom entry point that imports from it.

## Example: domain-specific decomposer

```python
# my_council.py
from orchestrator import council_v14

CUSTOM_DECOMPOSE_PROMPT = """You are a task decomposer specialized in
medical-research review. Decompose the task into 2-3 subtasks:

{{
  "subtasks": [
    {{"id": "s1", "title": "...", "description": "...",
     "weightVector": {{"research": 0.5, "safety": 0.3, "chinese": 0.2}},
     "dependencies": []}}
  ],
  "synthesisNotes": "Focus on evidence quality and patient safety implications."
}}

Task: {task}
"""

# Patch the orchestrator's prompt
council_v14.DECOMPOSE_PROMPT = CUSTOM_DECOMPOSE_PROMPT

if __name__ == "__main__":
    result = council_v14.run_council(
        task="Review this clinical-trial protocol",
        tier="deep",
        mode="report",
    )
    print(result["report"])
```

## Example: custom verifier rubric

```python
from orchestrator import council_v14

CUSTOM_VERIFY_PROMPT = """You are a code-review verifier. Score the
following code change on these specific dimensions:

- correctness (0-10)
- test_coverage (0-10)
- security (0-10)
- performance (0-10)

Code change:
{output}

Output JSON:
{{
  "dimScores": {{"correctness": 0-10, "test_coverage": 0-10,
                "security": 0-10, "performance": 0-10}},
  "overallScore": 0-10,
  "hardGateFailed": true/false,
  "reworkList": [...]
}}
"""

council_v14.VERIFY_PROMPT = CUSTOM_VERIFY_PROMPT
# ... run council as before
```

## When to customize vs when to use defaults

The default prompts are designed to be domain-agnostic. They work well for:
- Design decisions
- Code review
- Research summaries
- Strategic planning

You may want to customize when:
- The task requires domain-specific quality criteria (medical, legal,
  financial) that the generic dimensions miss
- You want stricter or looser hard-gate criteria
- The output format must match a downstream consumer (e.g. JSON schema)
- You want to bias toward specific thinking levels

## Prompt-templating gotchas

The prompts use Python `.format()` syntax. If your task text contains curly
braces (e.g. JSON snippets), you must escape them as `{{` and `}}`, or use a
different templating approach.

The `FRESHNESS_RULES` constant is included in both `VERIFY_PROMPT` and
`SYNTH_PROMPT`. If you customize either, you probably want to keep the
freshness rules — they're what enforce "the model must cite timestamps and
acknowledge its evidence is from a snapshot, not a live web fetch".