# Tuning Loop

1. Record a baseline distill run with `--record`.
2. Adjust prompts in `prompts/`.
3. Replay with `--replay-mode prompt-only` or `allow-live-on-miss`.
4. Compare product text with `scripts/prompt_ab.py` and render with `scripts/prompt_ab_view.py`.
5. Check golden eval and run metrics separately before promotion.

The current MVP tools provide readable text diffs only. Structured frontmatter,
citation, quality, and cost comparisons are a planned follow-up extension.
