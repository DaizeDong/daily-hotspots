# Contributing

daily-hotspots follows the Skill Repo Spec v1 and [PHILOSOPHY.md](PHILOSOPHY.md).

- Keep `SKILL.md` thin; push detail into `skills/daily-hotspots/reference/<shard>.md` (progressive
  loading) and logic into `skills/daily-hotspots/scripts/` (stdlib only).
- Every behavioral change ships with / updates a pytest case. Run before every PR:
  ```bash
  cd skills/daily-hotspots && python -m pytest tests/ -q
  ```
- Scoring weights and thresholds are **data**, not code, they live in the companion repo's
  `watchlist.json`. Changing scoring should be a config diff, not a code change.
- Public changes contain only code and generated synthetic examples. Runtime data and credentials
  belong in the separate PRIVATE companion, under its own versioned backup policy.
- For a release, align `plugin.json`, README badges, ROADMAP's Current version and the latest
  CHANGELOG release. Run the pinned documentation check:
  `python style/tools/doc_contract.py --root . --profile skill --stage accepted`.
  Documentation-only maintenance does not require a version bump; preserve released history.
- CI runs pinned `guards/tools/pii_guard.py`, `guards/tools/data_boundary.py`,
  `style/tools/load_budget.py` and `style/tools/dash_guard.py`. Security checks also run through
  `.githooks/` forwarding hooks, which require `guards/hooks/`; restore missing submodules with
  `git submodule update --init --recursive`. Missing scanners must fail visibly. Never bypass
  hooks or add a successful-exit fallback.
- Keep root entry documents and their operational references consistent. Use generated synthetic
  fixtures from `tools/make_fixtures.py`, and record actual run evidence only in PRIVATE DATA.
