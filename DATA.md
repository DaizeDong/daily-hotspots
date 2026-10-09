# Private data and retention

Real observations belong in a separate, verified PRIVATE companion repository. The pinned
Guards resolver and `skills/daily-hotspots/scripts/private_storage.py` prove every runtime destination; there is no
public-repository or unversioned fallback. [CONFIG.md](CONFIG.md) defines configuration schemas,
and [storage.contract.json](storage.contract.json) records each artifact's producer and consumer.
Contract paths are relative to the companion, with separate entries for `archive/` and
`data/archive/`. Alternate run roots must belong to the same companion for automatic compaction.

## Runtime write admission

The pinned Guards `authorize_artifact_write` API checks each produced file against this
source's storage.contract.json, the exact PRIVATE companion root and current Git ignore
policy. Undeclared, ambiguous, retired or ignored versioned destinations fail before a
write. Structural parent directories grant no permission to their future contents.
Atomic staging lives in the narrow `.staging/` patterns declared by the source, with
explicit transient persistence and operation-bound retention; it does not replace durable
recovery records. Selected credential backup stays under the companion's A/B policy.

## Artifact retention

| Artifact | Retention condition |
|---|---|
| Configuration, roster, credentials and private operational adapters | Keep while the installation uses them; replace only through reviewed configuration changes. |
| Opportunities, dedup state, digests, pull receipts and source rotation | Keep the canonical evidence and state required by delivery, replay, yield review and cursor recovery. |
| `runs/<run-slug>/candidates.json` and `result.json` | Keep the compact replay core. Promotion refuses different existing bytes. Older incomplete slices stay intact for inspection. |
| `finalizations/finalization-*.json` and `delivery-claims/finalization-*.json` | Keep logical-run reservations while this run identity can be presented again. Completed and uncertain claims both block resends. These are separate namespaces for the outer finalizer and inner driver. |
| `workspaces/<run-id>/` | Temporary collection and finalization files. Remove only after a completed claim and both exact replay files are preserved; uncertain, incomplete or conflicting workspaces remain recovery inputs. |
| Logs and derived reports | Retain for active diagnosis; routine logs may be retired after 14 days when no unresolved run references them. No automatic log TTL is implied by this contract. |
| `retired-source-residue/daily-hotspots-sie-runs/` | Temporary retired artifacts pending reviewed final deletion. They have no runtime consumer and must not enter routine archive publication. |

## Run identity and workspace recovery

`run.py --sources` freezes the selected roster batch and stores source plans, pull receipts and
cursor state in the PRIVATE companion. It advances once after every selected handle succeeds;
partial batches retain successful receipts for replay. Inspect source locks and prior process
ownership before recovery. [Roster evolution](skills/daily-hotspots/reference/roster-evolution.md)
defines batch conflicts and replay details.

Every non-preview `run.py --in` reserves its logical run ID under `delivery-claims/` before
processing. Successful and uncertain claims remain reserved; changing candidate input does not
authorize another send. Dry runs reserve nothing. Inspect retained claims and downstream receipts
after interruption before deciding how to recover.

The wrapper uses `archive/workspaces/<run-id>/` within the selected layout. It also supports
`data/archive/`. `DAILY_HOTSPOTS_RUN_ROOT` may select another verified PRIVATE versioned
location; automatic compaction requires it to share the archive's companion. The owner must
retain any unresolved workspaces outside the default archive in its commits and backups.
Keep raw responses, helper scripts, logs, readiness receipts and snapshots in the selected
`DAILY_HOTSPOTS_RUN_DIR`; transport helpers use `transport-<attempt>/` beneath it.
Failed runs retain evidence and attempt scoped publication to the proved PRIVATE upstream,
preserving the failure exit code without retrying delivery.

The normal wrapper writes the result before completing its delivery claim, preserves exact
`candidates.json` and `result.json` bytes under `archive/runs/`, and compacts successful workspaces
before its scoped private archive commit. Promotion or
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

## Reviewed legacy import

The bounded `archive/logs/legacy-user-root-20261006/` import is retired as a writer
namespace. Current wrappers create dated run/yield logs directly under
`archive/logs/`; targeted source and tracked non-log reference checks found no
consumer or selected recovery pointer into the import. This supports refusing new
writes there and removing its automatic archive-publication allow rule. It does
not establish that every historical log lacks diagnostic value: retain physical
files and the original unpublished recovery ref until selected incident evidence
and publication history have been reconciled. Active logs retain their existing
incident and recovery obligations.


The frozen `archive/imports/legacy-run-20261006/` import has exact declarations
for its observed candidate, lane, normalized source, roster and result files.
These structured observations stay on hold with their original context until
provenance and reference closure are reconciled. They are not canonical
`archive/runs/` replay files or delivery claims, and no runtime writer should
append to this import. Storage ownership does not prove that the historical
run completed or that its payload schema passes.

The two observed zero-byte stderr placeholders are rebuildable diagnostics.
Their retirement still requires an inactive reference review. This declaration
does not delete imported files or change existing workspace compaction rules.

## Working storage review budget

The source contract sets a 64 MiB review budget for the companion's working
files, excluding Git metadata. The shared check reports budget excesses even when
every artifact is declared. This threshold does not evict core observations,
claims, unresolved workspaces or incident evidence, and it is not enforced as
a new runtime admission limit by this documentation change.

Routine diagnostic logs older than 14 days need an exact review against active
writers, unresolved incidents and retained run claims. Preserve the necessary
final diagnostic conclusion and evidence before retiring a reviewed log; do
not copy the whole log into core storage. Workspace compaction still requires
completed claims and byte-identical candidate/result preservation. Retired
source residue must not grow through additional snapshots or copies. A blocked
physical retirement leaves the budget failure visible; it does not justify a
higher limit or a claim that cleanup completed.
