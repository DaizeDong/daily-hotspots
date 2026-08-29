# Contributing

daily-hotspots follows the Skill Repo Spec v1 and the **prove-don't-vibe** bar.

- Keep `SKILL.md` thin; push detail into `skills/daily-hotspots/reference/<shard>.md` (progressive
  loading) and logic into `skills/daily-hotspots/scripts/` (stdlib only).
- Every behavioral change ships with / updates a pytest case. Run before every PR:
  ```bash
  cd skills/daily-hotspots && python -m pytest tests/ -q
  ```
- Scoring weights and thresholds are **data**, not code, they live in the companion repo's
  `watchlist.json`. Changing scoring should be a config diff, not a code change.
- Never commit secrets. The Discord token lives only in the gitignored companion `secrets/`.
- Keep the four version sources in lock-step (`plugin.json` == README badge == ROADMAP "Current" ==
  CHANGELOG latest). **No gate checks this**, so it is a manual read of those four lines. This
  bullet named `check_conformance.py` for a long time; that file has never existed in this repo, so
  anybody who followed the instruction ran nothing and read the result as a pass.
- The gates that DO exist are four, and **CI is the authority for all four** because `--no-verify`
  and a broken local shell cannot reach it. `tools/pii_guard.py` and `tools/data_boundary.py` also
  run locally in `.githooks/pre-commit` and `.githooks/pre-push`; `tools/load_budget.py` (the
  always-loaded SKILL.md budget) and `tools/dash_guard.py` run in CI only, so run them by hand
  before publishing. Each one fails closed when the scanner or its target is absent, because an
  absent scan is not a clean scan. Never restore a `|| exit 0` to any of them.
