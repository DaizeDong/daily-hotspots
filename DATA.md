# Private data and retention

Real observations belong in a separate, verified PRIVATE companion repository. The pinned
Guards resolver and `scripts/private_storage.py` prove every runtime destination; there is no
public-repository or unversioned fallback. [CONFIG.md](CONFIG.md) defines configuration schemas,
and [storage.contract.json](storage.contract.json) records each artifact's producer and consumer.
Contract paths are relative to the companion, with separate entries for `archive/` and
`data/archive/`. Alternate run roots must belong to the same companion for automatic compaction.

| Artifact | Retention condition |
|---|---|
| Configuration, roster, credentials and private operational adapters | Keep while the installation uses them; replace only through reviewed configuration changes. |
| Opportunities, dedup state, digests, pull receipts and source rotation | Keep the canonical evidence and state required by delivery, replay, yield review and cursor recovery. |
| `runs/<run-slug>/candidates.json` and `result.json` | Keep the compact replay core. Promotion refuses different existing bytes. Older incomplete slices stay intact for inspection. |
| `finalizations/finalization-*.json` and `delivery-claims/finalization-*.json` | Keep logical-run reservations while this run identity can be presented again. Completed and uncertain claims both block resends. These are separate namespaces for the outer finalizer and inner driver. |
| `workspaces/<run-id>/` | Temporary collection and finalization files. Remove only after a completed claim and both exact replay files are preserved; uncertain, incomplete or conflicting workspaces remain recovery inputs. |
| Logs and derived reports | Retain for active diagnosis; routine logs may be retired after 14 days when no unresolved run references them. No automatic log TTL is implied by this contract. |
| `retired-source-residue/daily-hotspots-sie-runs/` | Temporary retired artifacts pending reviewed final deletion. They have no runtime consumer and must not enter routine archive publication. |

The normal wrapper writes the result before completing its delivery claim, promotes the replay
core, and compacts successful workspaces before its scoped private archive commit. Promotion or
compaction failures return a nonzero status. An acknowledged delivery with a later archive failure
must be inspected, since the retained claims still forbid retrying the logical run.

For an existing workspace, first preview the same domain checks from the source checkout:

```powershell
python skills/daily-hotspots/scripts/runstore.py compact daily-2000-01-01 --src "$env:DAILY_HOTSPOTS_CONFIG/archive/workspaces/daily-2000-01-01" --archive-dir "$env:DAILY_HOTSPOTS_CONFIG/archive" --dry-run
```

Use the actual run ID and resolved archive layout. After reviewing the preview, omit `--dry-run`
to apply. The command requires a safe run-directory name, a shared PRIVATE worktree, a completed
reservation, a matching candidate hash, a valid result for that run and byte-identical promotion.
It preserves legacy workspace reservations outside the workspace before removing any file.
Candidates share the handoff's 20,000,000-byte limit; result files are limited to 1 MiB. Links,
junctions, hardlinks, nested repositories, lock or PID markers and changed files refuse deletion.
Source files are inspected before removal, and newly appearing files prevent directory removal.

The shared `skill-smith` storage checker validates and inventories this contract without reading
payload bodies. Its generic retirement command must not remove workspaces or claim files; only
`runstore.py compact` understands their completion and replay requirements. Unknown artifacts need
classification and dependency review before retirement. `runstore.py prune` remains restricted to
explicit legacy scratch outside Git worktrees. Compaction removes working files; it does not rewrite
private Git history or delete older replay slices.
