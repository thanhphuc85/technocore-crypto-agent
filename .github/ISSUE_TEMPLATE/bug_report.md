---
name: Bug report
about: Report something that isn't working as documented
title: "bug: "
labels: bug
assignees: ''
---

## Description

A clear and concise description of what the bug is.

## To reproduce

Steps to reproduce the behavior:

1. Set env `...`
2. Run `python agent_cron.py` / call helper `...`
3. See error

Minimal code snippet, if applicable:

```python
```

## Expected behavior

What you expected to happen.

## Actual behavior

What actually happened. Include the full traceback / log output (redact any
secret — never paste `AGENT_PRIVATE_KEY`, API keys, or a 64-hex seed).

```
```

## Environment

- SDK version / commit: <!-- `pip show technocore-agent-sdk` or git SHA -->
- Python version: <!-- `python --version` -->
- OS:
- Install method: <!-- pip install -e . / from PyPI / clone -->
- LLM provider (if relevant): <!-- gemini / openai / none -->

## Additional context

Anything else that might help — related issues, recent changes, whether it
reproduces on a clean checkout.
