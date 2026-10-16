# Contributing

daily-hotspots follows the Skill Repo Spec v1 and [PHILOSOPHY.md](PHILOSOPHY.md).

- Keep `SKILL.md` thin; push detail into `skills/daily-hotspots/reference/<shard>.md` (progressive
  loading) and logic into `skills/daily-hotspots/scripts/` (stdlib only).
- Scoring weights and thresholds are **data**, not code, they live in the companion repo's
  `watchlist.json`. Changing scoring should be a config diff, not a code change.
- Public changes contain only code and generated synthetic examples. Runtime data and credentials
  belong in the separate PRIVATE companion, under its own versioned backup policy.
- For a release, align `plugin.json`, README badges, ROADMAP's Current version and the latest
  CHANGELOG release. Run the documentation check from the fleet-style checkout:
  `python "$(git config --get fleet.style)/tools/doc_contract.py" --root . --profile skill --stage accepted`.
  Documentation-only maintenance does not require a version bump; preserve released history.
- Set `git config core.hooksPath .githooks`. The `.githooks/` shims run the privacy checks from
  the local fleet-guards checkout named by `git config --global fleet.guards`, and the dash check
  from `fleet.style`; an unset key blocks the commit. Install the runtime resolver with
  `pip install -e "$(git config --get fleet.guards)"`. Never bypass hooks or add a
  successful-exit fallback.
- Keep root entry documents and their operational references consistent. Use generated synthetic
  examples from `tools/make_fixtures.py`, and record actual run evidence only in PRIVATE DATA.
