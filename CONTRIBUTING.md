# Contributing

daily-hotspots follows the Skill Repo Spec v1 and the **prove-don't-vibe** bar.

- Keep `SKILL.md` thin; push detail into `skills/daily-hotspots/reference/<shard>.md` (progressive
  loading) and logic into `skills/daily-hotspots/scripts/` (stdlib only).
- Every behavioral change ships with / updates a pytest case. Run before every PR:
  ```bash
  cd skills/daily-hotspots && python -m pytest tests/ -q
  ```
- Scoring weights and thresholds are **data**, not code — they live in the companion repo's
  `watchlist.json`. Changing scoring should be a config diff, not a code change.
- Never commit secrets. The Discord token lives only in the gitignored companion `secrets/`.
- Keep the four version sources in lock-step (`plugin.json` == README badge == ROADMAP "Current" ==
  CHANGELOG latest) and re-run `check_conformance.py` before publishing.
