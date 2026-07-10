# Engine design and verified sacctmgr semantics

This collection converges Slurm accounting through `sacctmgr`'s flat-file
dump/load mechanism rather than per-entity `sacctmgr add/modify/delete`
calls or the slurmrestd REST API. This document records **why** the engine
looks the way it does: every rule below was verified empirically against
live Slurm clusters (25.05.4, 25.11.5, 26.05.1 — the ghcr.io test images
built by the terraform-provider-slurm sibling project), and several of them
are invisible failure modes you cannot discover from documentation.

## The flat-file format (as `sacctmgr dump` emits it)

```
QOS - 'standard':Description='default general-purpose qos':MaxWallDurationPerJob=1440:Priority=100
Cluster - 'linux':Fairshare=1:QOS='normal'
Parent - 'root'
User - 'root':DefaultAccount='root':AdminLevel='Administrator':Fairshare=1
Account - 'teaching':Description='teaching and courses':Organization='education':DefaultQOS='short':Fairshare=50
Parent - 'teaching'
User - 'dave':DefaultAccount='teaching':DefaultWCKey='genomics':WCKeys='genomics':Fairshare=15
User - 'john':Partition='cpu':DefaultAccount='lab_physics':Fairshare=1
```

- A `User` line's account is the current `Parent` context; partition-scoped
  associations are separate lines with `Partition=`.
- User-entity fields (`DefaultAccount`, `AdminLevel`, `DefaultWCKey`,
  `WCKeys`, `Coordinator`) are repeated on every line of the same user.
- Dumps emit only explicitly-stored values — inherited values never appear
  on member lines.
- `Fairshare=parent` is accepted literally on load; dumps emit the raw
  sentinel `2147483647` (`SLURMDB_FS_USE_PARENT`), identical across all
  three versions.
- sacctmgr lowercases `Description`/`Organization` on store, sorts QOS
  lists, and canonicalizes key aliases on dump (`MaxJobsPerUser` →
  `MaxJobsPU`). The collection normalizes both sides accordingly.
- There is no escaping for `'` or `:` inside values; the collection refuses
  such values at validation time.

## Verified load semantics

| # | Finding | Consequence in the engine |
|---|---------|---------------------------|
| 1 | `load ... clean` is idempotent on an unchanged file and **preserves association ids (and usage) of unchanged entries** (server-side diff). | Purge mode can safely re-load the full inventory. |
| 2 | `clean` is **not transactional**: a file that errors midway (e.g. an undefined parent) has already applied its deletions. | Strict structural validation happens before anything touches the cluster. |
| 3 | Several mid-file failures **exit 0 with nothing on stderr** (the client prints its normal to-add tables; the real error — e.g. `no association for parent X` — appears only in slurmdbd's log). | Every load's output is scanned for error phrasings AND the module re-dumps after applying, failing loudly if the cluster didn't converge. |
| 4 | `clean` never deletes QOS (they are global, not per-cluster). | QOS removal is a separate reconcile step, gated by the purge flag, never touching `normal`. |
| 5 | Removing an entity's last association leaves an orphaned zero-association entity; **a zero-association entity poisons later loads** — load sees the entity exists and silently skips creating its association. | Zero-association entities the file is about to define are deleted before loading; purge removes the rest (never `root`). |
| 6 | Account/user lines referencing a QOS created **earlier in the same file** fail (`You gave a bad qos`) — QOS validation uses process-start state. | QOS converge in their own plain pre-load; the hierarchy file carries no QOS lines. |
| 7 | A QOS line that differs from the live definition mid-hierarchy-file (e.g. built-in `normal`, whose init-time description bypasses sacctmgr's lowercasing) **silently aborts the rest of the file**. | Same as 6 — no QOS lines in the hierarchy load. |
| 8 | Plain `load` (no `clean`) applies creations and value updates (with a `Changed X ... old -> new` report) but **cannot reliably create associations that depend on other additions in the same file**; a batch containing one permanently-invalid element re-fails identically on every run. | Even additive mode uses `clean` — against a merged live∪desired file, so clean has nothing to delete. |
| 9 | `clean` resets omitted association/account fields (verified for `MaxJobs`, account `QOS=` lists, `Coordinator`) but does **not** reset an omitted `AdminLevel`; an explicit `AdminLevel='None'` does reset it. | Purge mode renders authoritatively (explicit `AdminLevel='None'`); purge-mode comparison is exact ("remove = reset"). |
| 10 | **QOS definition fields never reset when omitted** from a load file. | QOS compare declared-keys-only in both modes; changing a QOS limit requires setting it explicitly. |
| 11 | `WCKeys=`/`DefaultWCKey=` round-trip through dump/load (unlike the REST API, which cannot read `default_wc_key` back at all). | WCKeys are first-class, diffable inventory data. |
| 12 | **Version difference — 25.11+: a `WCKeys=` on any User line silently aborts the entire `load ... clean` transaction** (exit 0, nothing created). On 25.05 the same line merely "flaps": each clean pass drops the user's WCKeys, and the file's value only re-applies when absent beforehand. | The clean pass renders **without** WCKey fields; a plain second pass applies them. Works on all three versions. |
| 13 | `Coordinator='acct1,acct2'` on user lines round-trips and resets when omitted under clean. | Per-account `coordinators` lists are supported (the REST provider cannot manage them at all). |
| 14 | The `root` account's account-level attributes (fairshare, GrpTRES/Max* limits, allowed/default QOS) are stored on the **Cluster line** in the dump — there is no `Account - 'root'` line. A `load ... clean` whose Cluster line carries those fields sets root's association, and clean **resets** root's fields to the Cluster line's values every run. | `root` is converged by rendering the desired fields onto the Cluster line (declared-keys-only overlay on the live line) and letting the existing clean-load apply them — no special-case `sacctmgr modify` needed. |
| 15 | The built-in `normal` QOS is **absent from the dump at pristine default** (it only appears once it has a non-default field), but is always readable via `sacctmgr show qos normal`. `sacctmgr modify qos normal set …` persists, is untouched by `clean` (finding 4), and lowercases `Description` exactly like a load. | `normal` is converged in place with `sacctmgr modify` (never a load file — finding 7), and its live state is read via `show` (durations parsed back into the flat-file integer units), so a declared-value comparison is stable even at default. |

No other behavioral differences were observed across 25.05.4 / 25.11.5 /
26.05.1. Findings 14–15 verified on 25.05.4 and 25.11.5.

## The apply sequence

```
validate → dump + entity listings → plan
  └─ check mode stops here (dump/show only — nothing can mutate slurmdbd)
1. delete zero-association entities the file defines        (finding 5)
2. plain-load a QOS-only file, if QOS changed               (findings 6, 7)
3. `load ... clean` with a WCKey-less hierarchy file        (findings 8, 9, 12)
4. plain `load` of the full file (applies WCKeys)           (findings 11, 12)
4b. `sacctmgr modify qos normal set …` for declared system QOS (findings 7, 15)
5. purge only: delete undeclared QOS, orphan users/accounts (findings 4, 5)
6. re-dump, re-plan, fail on residual diff                  (finding 3)
```

Additive mode's hierarchy file is the **live state with the inventory merged
on top** (so `clean` deletes nothing); purge mode's is exactly the
inventory. In both modes the Cluster line is re-rendered from the live dump
with any inventory-declared `root` fields overlaid (finding 14 — that is how
root's account-level attributes are managed; other cluster-level defaults are
still left untouched), and the fixed `root` User line is always present.

## Managing `root` and `normal` in place

The two entities the collection never creates or deletes — the `root` account
and the `normal` QOS — may still be **converged** when declared, via
mechanisms that sidestep their special-casing:

- **`root` account** — declare it in the `accounts` map with the override
  fields only (`fairshare`, `default_qos`, `allowed_qos`, `max_jobs`, TRES
  limits). These live on the Cluster line (finding 14), so they are applied by
  the ordinary clean-load and round-trip through the dump. Declared-keys-only
  in both modes: undeclared cluster-line fields are never fought over. No
  parent/members/metadata are accepted for `root`, and it is never deleted.
- **`normal` QOS** — declare it in the `qos` map like any QOS. It is applied
  with `sacctmgr modify` (finding 15), never written to a load file (a
  differing built-in QOS line silently aborts a load — finding 7), and never
  deleted. Live state is read via `sacctmgr show qos`, converting the
  `[D-]HH:MM:SS` durations back to the flat-file integer units. Declared-keys
  only — omitted fields are left as-is (system-QOS fields never reset,
  finding 10).

## Comparison / normalization rules

- fairshare `2147483647` ↔ `parent`; declaring the raw sentinel is refused
  with a hint (same guard as the provider's `fairshareValidator`).
- QOS/Flags/WCKeys lists sorted; TRES lists sorted;
  Description/Organization lowercased; `AdminLevel='None'` ≡ absent.
- Additive mode compares only inventory-declared keys (remove = stop
  managing); purge mode compares exactly (remove = reset). QOS fields are
  declared-keys-only in both modes (finding 10).

## Incident worth remembering (test-cluster corruption)

Hand-deleting parent accounts with `sacctmgr delete account` while
soft-deleted children still referenced them left `linux_assoc_table` rows
with `deleted=1` whose `id_parent` pointed at hard-deleted rows. From then
on slurmdbd rejected **every** load batch with `no association for parent X`
(client exit 0, normal-looking output). Recovery:
`delete from linux_assoc_table where deleted=1;` + slurmdbd/slurmctld
restart. The module's own deletion order (clean-load first, orphan entities
after) does not produce this state, but out-of-band `sacctmgr delete`
sequences can.
