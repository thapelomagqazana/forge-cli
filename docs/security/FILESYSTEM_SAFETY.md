# Filesystem Safety

**Status:** Stable — matches the guarantees implemented by `src/forge/safety/` and the ADRs it references.
**Applies to:** every filesystem write in the generation pipeline (`forge create`, the future `forge generate`, the future `forge commit`).
**Not applicable to:** read-only commands (`forge list`, `forge validate`, `forge --version`), which do not touch the filesystem beyond reading.

---

## 1. Why this document exists

A scaffolding tool's failure modes are unforgiving. When `forge create` runs, it produces files in a directory the user chose, often one that already has content. If the tool corrupts that directory — overwrites a file the user forgot about, writes outside the project into `/etc`, follows a symlink to the wrong place, or leaves a half-finished project after a mid-run error — the user loses work they cannot recover without an external backup.

The safety layer exists to make those failures *impossible by construction*, not merely *unlikely by convention*. This document names the guarantees it provides, the reasoning behind each, and the specific limits of each — because a guarantee whose limits are undocumented is a bug waiting to happen when a future contributor assumes more than it delivers.

The document is not aspirational. It describes the *current* behaviour of the code. Where a guarantee has an acknowledged gap (e.g., TOCTOU windows), the gap is stated explicitly so a reviewer can decide whether it is acceptable for their deployment and a contributor can decide whether closing it is worth the complexity.

---

## 2. The guarantees

The safety layer makes **seven** guarantees. Each is a property a caller can rely on without reading the implementation; each is enforced by at least one primitive in `src/forge/safety/` and pinned by at least one test in `tests/unit/safety/` or `tests/adversarial/`.

### G1. No path escapes the destination root

**What it means.** A path passed to any safety-layer primitive — as a candidate destination, a source template, or an intermediate component — is rejected if its resolved form is not a *component-wise descendant* of the resolved root. Paths containing `..` segments that would escape, absolute paths, and paths that resolve outside the root after following symlinks are all refused.

**How it is enforced.** `safe_resolve` in `src/forge/safety/paths.py` canonicalises the candidate with `Path.resolve()` and compares it against the canonical root with `Path.relative_to()`. The comparison is component-wise, so a shared prefix (`/srv/foo` vs `/srv/foobar`) does not falsely pass.

**What it does *not* guarantee.** `safe_resolve` reads the filesystem as it is at the moment of the call. A path that is inside the root now may be moved outside between the check and the write (see §5, TOCTOU). The guarantee is "safe as of the check", not "safe under concurrent modification".

**Where it is tested.**
- `tests/unit/safety/test_paths.py::TestTraversalAndCanonicalisation` (traversal rejection, within-root acceptance).
- `tests/adversarial/test_path_traversal.py` (six spellings of `..` attack).
- `tests/adversarial/test_absolute_paths.py` (POSIX, Windows drive letter, UNC forms).

### G2. Absolute paths are rejected in every spelling

**What it means.** A destination is treated as absolute — and refused — if it takes any of these three forms:

- POSIX absolute: `"/etc/passwd"`, `"/tmp/foo"`, `"/"`.
- Windows drive-letter: `"C:/Windows"`, `"D:\Users"`, `"c:\lower"` (both slash directions, both cases).
- UNC: `"\\server\share"`, `"//server/share"`, `"\\?\C:\foo"` (extended-length prefix).

**How it is enforced.** `safe_resolve` checks three things in sequence: `Path(candidate).is_absolute()`, `raw[1] == ":"` for drive letters, and `raw.startswith(("\\\\", "//"))` for UNC. All three produce the same finding code (`path.absolute`) because the remediation is the same: "provide a path relative to the root".

**What it does *not* guarantee.** A path that *looks* relative but is interpreted as absolute by a future consumer (a Windows-only subprocess, a network client) is outside the safety layer's scope — the caller must not pass the returned path to a context where it could be reinterpreted. The safety layer's job is to reject the three forms above; it is not a validator for every possible consumer.

**Where it is tested.**
- `tests/unit/safety/test_paths.py::TestAbsolutePaths` (all three forms, parametrised).
- `tests/adversarial/test_absolute_paths.py` (five spellings, including UNC).

### G3. Symlinks are rejected by default

**What it means.** A destination whose *resolved* path traverses a symlink — at any component, leaf or intermediate — is refused. This includes symlinks that point *inside* the root; the default policy rejects all symlinks in destination paths, because a symlink can be retargeted between the check and the write (see §5).

**How it is enforced.** Two checks, both in `safe_resolve` (and mirrored in `validate_destination` for the leaf):

1. **Unresolved walk** — every component of `root / candidate` is inspected with `os.lstat` (not `stat`, which follows links). If any is a symlink, `path.symlink_component` is returned. This catches the symlink the user can find and remove.
2. **Resolved walk** — every component of `resolved(candidate)` is likewise inspected. This catches redirects where the *effect* of a symlink moved the write to a different in-root path, even though the caller's spelling had no symlink at that position.

**Why both walks.** The unresolved walk catches the specific symlink the caller wrote; the resolved walk catches the *effect* of a redirect whose source the caller did not spell as a symlink. Neither walk subsumes the other. See `src/forge/safety/paths.py`'s "Why both walks" section for a worked example of each.

**What it does *not* guarantee.** A symlink that is *inside* a directory the caller writes to, but not part of any destination path the caller declared, is not inspected. A caller that writes `root/data/file.txt` where `root/data` contains unrelated symlinks is safe (those symlinks are not traversed); a caller that writes through any of them is refused.

**Where it is tested.**
- `tests/unit/safety/test_paths.py::TestSymlinkWalk` (leaf, intermediate, loop, benign-inside).
- `tests/adversarial/test_symlink_escapes.py` (four vectors, including the loop).

### G4. Every write goes through staging; no partial file is ever visible

**What it means.** The pipeline produces files by writing them into a hidden `.forge-stage-*` directory *inside the destination root*, then committing all of them atomically. A failure before commit leaves the destination untouched; a failure during commit leaves the already-committed files (each moved atomically — they are complete) and the staging directory cleaned up. In no case is a partially-written file visible at the destination.

**How it is enforced.** `StagingArea` in `src/forge/safety/staging.py`:

- `for_root(root)` creates the staging directory via `tempfile.mkdtemp(prefix=".forge-stage-", dir=root)`, guaranteeing same-filesystem placement (so `os.replace` is atomic per file).
- `write_bytes` / `write_text` place files inside the staging tree; `_prepare` refuses any relpath that escapes.
- `commit()` iterates the staged files and moves each with `os.replace` — atomic within the filesystem.
- The context manager's `finally` removes the staging directory on *every* exit path.

**What it does *not* guarantee.** Cross-file transactionality. A commit that fails on file 4 of 10 leaves files 1–3 committed. The caller (the CLI) surfaces this as an error; the user can re-run. Rollback across files is intentionally not implemented — see `staging.py`'s "Why not roll back on partial commit failure?" section for the reasoning.

**Where it is tested.**
- `tests/unit/safety/test_staging.py` (happy path, escape rejection, commit semantics, cleanup, duplicate writes).
- `tests/integration/test_safety_pipeline.py::TestNoStagingLeaks` (no staging directory leaks on success, collision refusal, structural refusal).

### G5. Existing files are not overwritten without explicit consent

**What it means.** A plan that targets an existing file is refused unless the caller has passed `--overwrite` (which becomes `overwrite=True` in the primitives). The refusal is a `CollisionError` (exit code 6), not a `SafetyError` (exit code 3), because the two have different remediations and CI scripts branch on the distinction.

**How it is enforced.** `detect_collisions` in `src/forge/safety/collisions.py` lstat-s every planned destination and produces a `collision.exists` finding for each existing one when `overwrite=False`. `raise_if_collisions` translates those findings into `CollisionError`. The check uses `lstat` (not `Path.exists()`), so a broken symlink occupying the destination counts as existing.

**What it does *not* guarantee.** That the existing file was *meant* to be there. The pipeline refuses to overwrite, but it cannot distinguish "an unrelated file that happens to share a name" from "the file the previous run of `forge create` wrote". The user (or a `--overwrite` invocation) decides.

**Where it is tested.**
- `tests/unit/safety/test_collisions.py::TestExactDiskCollision` (existing file, existing directory, broken symlink, `overwrite=True` suppression).
- `tests/integration/test_safety_pipeline.py::TestCollisionWithoutOverwrite` and `TestCollisionWithOverwrite`.

### G6. `--overwrite` is recoverable: every replaced file is backed up first

**What it means.** When `overwrite=True` and a destination file exists, the file is copied to a timestamped sibling (`<name>.bak.<UTC-timestamp>`) *before* the write. The original content is recoverable from the backup. A failure during backup aborts the whole operation before any file is replaced, so a partial overwrite is not possible.

**How it is enforced.** `backup_existing` in `src/forge/safety/overwrite.py`:

- Copies each existing target with `shutil.copy2` (preserving mtime and mode bits).
- Uses the atomic-copy pattern: `copy2` into a hidden temp file in the same directory, then `os.replace` to the backup name.
- Rolls back (removes) all backups created in the call if any copy fails, so the caller sees either "every file backed up" or "nothing was changed".

**What it does *not* guarantee.** Durability against filesystem corruption. `copy2` uses the OS's normal write path; if the disk crashes mid-copy, the backup may be incomplete. The pipeline does not fsync explicitly (the OS does it according to its own policy). Callers who need stronger durability should use `--overwrite` *and* a filesystem-level backup (snapshots, RAID, etc.).

**Where it is tested.**
- `tests/unit/safety/test_overwrite.py` (happy path, atomicity, rollback on failure, nonexistent inputs, deduplication, timestamp validation, broken symlinks).
- `tests/integration/test_safety_pipeline.py::TestCollisionWithOverwrite` (backup created with original content).

### G7. Refusals are batched, actionable, and typed

**What it means.** When the safety layer refuses an operation, it reports *every* violation in one pass, each with:

- A `code` (stable string a script can branch on),
- A `path` (the path the user supplied or the intermediate component, in a form they can act on),
- A `message` (what is wrong, in a sentence),
- A `hint` (what to do about it, in a sentence — mandatory).

The refusal surfaces as a typed exception: `CollisionError` (exit code 6) for overwrite-recoverable problems, `SafetyError` (exit code 3) for structural ones.

**How it is enforced.** Every safety primitive returns `list[Finding]` (or a tuple of findings bundled into a `SafetyError` / `CollisionError`). The `Finding` dataclass makes `code`, `path`, `message`, and `hint` mandatory at construction — a missing hint is a `TypeError` at build time, not a silent dead-end at runtime.

**What it does *not* guarantee.** That the *language* of the hint is optimal for every user. The hints are written for a technical audience (the user has run a CLI tool and can fix a path or a symlink); a hint that says "remove the symlink" is actionable for that audience and may be too terse for another.

**Where it is tested.**
- `tests/unit/safety/test_paths.py::TestFindingShape` (every finding has all four fields).
- `tests/unit/safety/test_destinations.py::TestFindingShape` (multiple findings aggregated).
- `tests/unit/safety/test_collisions.py::TestFindingShape` and `TestRaiseIfCollisions::test_collision_error_carries_findings`.

---

## 3. The pipeline's structure

The guarantees compose into a pipeline with four phases, in this order. Each phase is preceded by a comment in `src/forge/cli/commands/create.py::_generate` explaining why the order is what it is.

```
    Phase 1: Structure          safe_resolve + validate_destination(overwrite=True)
                                └─ refuses traversal, absolute paths, symlinks,
                                   unwritable parents, leaf symlinks, NUL bytes,
                                   too-long paths, control characters
                                └─ raises SafetyError (exit 3)

    Phase 2: Collision          detect_collisions + raise_if_collisions
                                └─ refuses existing destinations without consent
                                └─ raises CollisionError (exit 6)

    Phase 3: Backup             backup_existing (only when overwrite=True)
                                └─ copies every existing destination before any write
                                └─ rolls back on failure

    Phase 4: Stage + commit     StagingArea.for_root + write_bytes/commit
                                └─ atomic per file, atomic-per-file-visibility
                                └─ cleans up the staging directory on exit
```

**Why this order.**

- **Structure before collision.** A path that is structurally unsafe (traversal, absolute) has no meaningful "collision" — you cannot ask "does `/etc/passwd` collide" as if it were a valid destination. Phase 1 establishes that the plan's destinations are *valid*; phase 2 checks whether they are *available*.
- **Collision before backup.** A file that will not be replaced (because it collides and the user refused) should not be backed up — a spurious backup is noise. Phase 2 stops the pipeline before phase 3.
- **Backup before staging.** If staging fails (a render error, an unwritable staging directory), the backups are already in place. The user can restore from them. Backing up *after* staging would leave a window where the file is neither backed up nor committed.
- **`overwrite=True` in phase 1's `validate_destination` call.** This suppresses the *existence* check (which is phase 2's job) while preserving the *structural* checks (leaf symlink, parent writability). Without this, a collision would surface as `SafetyError` (exit 3) from phase 1 instead of `CollisionError` (exit 6) from phase 2 — the wrong type, the wrong exit code, the wrong remediation. See `src/forge/cli/commands/create.py`'s "Why `validate_destination(overwrite=True)`" comment.

---

## 4. Explicit policy decisions

The guarantees above are *enforced*. The table below documents the *choices* behind them, so a reviewer can see what the layer does and does not attempt.

| Decision | Choice | Reasoning |
|---|---|---|
| Path safety | Component-wise comparison (`relative_to`) | String-prefix checks are defeated by shared prefixes; `relative_to` matches the OS's own resolution behaviour. |
| Broken symlink at destination | Refused (`path.symlink_component`) | The name is in use; silently writing through the symlink would either fail or write at the target, both surprising. |
| Symlink pointing inside the root | Refused by default | TOCTOU: the link can be retargeted between check and write. Users who need symlink destinations can opt in via `SafetyPolicy(allow_symlinks_in_destination=True)`. |
| Overwrite backups | Per-file timestamped siblings | Inspectable with `ls`, sortable, copy-paste-able. Tarballs are opaque and all-or-nothing. |
| Backup atomicity | Copy to temp, rename into place | `os.replace` is atomic within a filesystem; partial backups never appear at the expected name. |
| Backup failure handling | All-or-nothing (rollback on any failure) | A partially-populated backup set is a lie — the user trusts it and finds a file missing later. |
| Staging directory placement | Inside the destination root | Same filesystem, so `os.replace` is atomic. `/tmp` staging would break atomicity across devices. |
| Commit failure handling | No cross-file rollback | Rolling back requires copying originals over new files, which is more likely to fail and can lose data. Backups (from phase 3) are the recovery mechanism. |
| Duplicate writes to staging | Last-write-wins, single commit | Matches what writing directly to the destination would do. Transparent layer. |
| Exit codes | Distinct 3 (structural) vs 6 (collision) | CI scripts can branch on the distinction: 6 means "retry with `--overwrite`", 3 means "fix the plan". |

---

## 5. Known limits and non-goals

The safety layer is honest about what it does not attempt. This section names the gaps so a reviewer can decide whether they matter for their deployment and a future contributor can decide whether closing them is worth the complexity.

### 5.1 TOCTOU windows (time-of-check / time-of-use)

**The gap.** Every check that reads the filesystem can have its result invalidated by a concurrent process between the check and the corresponding write. Examples:

- `safe_resolve` confirms a component is not a symlink; another process replaces it with a symlink between the check and the write; the write follows the new symlink.
- `validate_destination` confirms the parent is writable; another process removes the parent's `w` bit; the write fails at `open` time.

**How it is mitigated.**

- **Staging** — writes happen inside a `.forge-stage-*` directory the pipeline created itself, so between-check-and-write races on the *destination* are limited to the commit step (`os.replace`), which is atomic at the filesystem level.
- **Backups** — a race that changes the destination between backup and commit leaves the pre-race content in the backup, so the user can recover the pre-commit state.
- **Defence in depth** — the pipeline runs multiple checks (safe_resolve, validate_destination, detect_collisions) that each independently reach the same conclusion; a race that invalidates one check usually does not invalidate all three.

**What a caller should not assume.** That a successful `safe_resolve` / `validate_destination` / `detect_collisions` sequence proves the destination is safe *when the write happens*. It proves the destination *was* safe at the time of the check. A caller with adversarial concurrent modification of the destination root is outside the safety layer's threat model — that caller should use OS-level isolation (containers, mount namespaces).

### 5.2 Cross-filesystem staging

**The gap.** The atomicity of `os.replace` depends on the source and destination being on the same filesystem. `StagingArea.for_root` creates the staging directory *inside* the destination root, which on any normal system is the same device. If the destination root is a mount point whose parent is on a different device, and the root itself has no writable storage, the staging directory could land on a different device and `os.replace` would fail (or, worse, silently fall back to copy-then-delete).

**How it is mitigated.** `for_root` requires the destination root's *own* parent to be writable; if the root is a mount point, the staging directory is created inside the mount, which is by definition on the same device as the destination. A caller whose destination root is a mount point with no writable space gets an `OSError` from `mkdtemp` and the pipeline aborts (correctly) before any write.

**What a caller should not assume.** That a very unusual filesystem layout (nested mounts, btrfs subvolumes, etc.) is handled automatically. A caller with such a layout should verify that the staging directory lands on the same device as the destination (e.g., `stat -c %d <staging_dir> == stat -c %d <destination>`).

### 5.3 Resource exhaustion

**The gap.** A caller producing an enormous number of files (`forge create` from a pathological blueprint with 10,000,000 `files[]` entries) will consume time, memory, and disk proportional to the file count. The safety layer does not enforce limits on these.

**How it is mitigated.** The layer *does* enforce a `max_path_length` (400 characters) on each destination, which bounds the plan's memory footprint per file. It does *not* bound the number of files in the plan.

**What a caller should not assume.** That a hostile blueprint cannot consume unbounded resources. A caller executing blueprints from untrusted sources should enforce resource limits at the OS level (cgroups, container quotas, CI job timeouts).

### 5.4 Case-sensitive vs case-insensitive filesystems

**The gap.** The safety layer's collision detection is *case-sensitive* — `README.md` and `readme.md` are treated as distinct destinations. On a case-insensitive filesystem (macOS, Windows, many Docker volumes) they are the same file, and one will silently overwrite the other at commit time.

**How it is mitigated.** `detect_collisions` reports a `plan.case_insensitive_duplicate` finding when the *plan itself* contains two destinations that differ only in case. This is not a disk check — it does not need to be, because the plan is the source of truth for what the pipeline *will* write. The finding is *not* suppressed by `overwrite=True` (the flag consents to replacing disk files, not to a plan that will behave differently on different filesystems).

**What a caller should not assume.** That a plan with a case-duplicate will be caught on case-sensitive filesystems only. It is caught on every filesystem, which is the correct behaviour — the plan is defective regardless of where it runs.

**Where it is tested.** `tests/unit/safety/test_collisions.py::TestCaseInsensitiveDuplicate`.

### 5.5 Encodings other than UTF-8

**The gap.** The safety layer does not check the *encoding* of file content it copies or the *character class* of paths it accepts beyond NUL, newline, and carriage return. A path with arbitrary Unicode codepoints is accepted as long as it satisfies the structural checks. A file with, say, malformed UTF-8 bytes is copied as-is by `copy2` (which operates on bytes, not characters).

**How it is mitigated.** The *renderer* (`src/forge/rendering/`) does check UTF-8 for template files, but the safety layer does not check content encoding for copied files. A caller who needs encoding validation on copied files should validate the source before passing it to the pipeline.

**What a caller should not assume.** That a file with malformed bytes is rejected by the safety layer. It is copied verbatim (that is what "copy" means); the renderer handles the encoding check for the files it renders.

---

## 6. What the safety layer depends on

The safety layer is a leaf in the module graph — it imports only from `forge.safety.*` and the standard library. It has no dependencies on `forge.cli`, `forge.core`, `forge.loading`, `forge.rendering`, or any other application module. This is deliberate (see `src/forge/safety/__init__.py`'s "dependency direction" section):

- A *primitive* must not depend on its consumers. Making `safety` depend on `cli` would invert the direction and make `safety` impossible to test in isolation.
- The CLI translates the safety layer's exceptions (`SafetyError` / `CollisionError` from `forge.safety.errors`) into the CLI's exception types (`SafetyError` / `CollisionError` from `forge.core.exceptions`) at the boundary (`create._generate::_raise_safety_error`). That translation is *not* the safety layer's concern.
- A future non-CLI caller (a `forge` HTTP server, a batch tool) can use the safety layer directly without depending on the CLI, and can make its own choice about how to surface the findings.

The tests enforce this: `tests/unit/safety/test_*.py` do not import anything from `forge.cli` or `forge.core`. A future change that added such an import would be caught by the fact that the tests would still pass — and by `tests/unit/discovery/test_no_cli_imports.py`, which asserts the same discipline for discovery.

---

## 7. Where to find the guarantees in code

For a contributor about to change `src/forge/safety/`, the table below maps each guarantee to the primitive that enforces it, the test class that pins it, and the ADR that documents the decision.

| Guarantee | Primitive | Unit test class | ADR |
|---|---|---|---|
| G1 — no traversal | `safe_resolve` | `TestTraversalAndCanonicalisation` | ADR-0017 |
| G2 — absolute rejection | `safe_resolve` | `TestAbsolutePaths` | ADR-0017 |
| G3 — symlink rejection | `safe_resolve`, `validate_destination` | `TestSymlinkWalk`, `TestLeafSymlink` | ADR-0017 |
| G4 — staging | `StagingArea` | `TestWriteAndCommit`, `TestCommitSemantics`, `TestCleanup` | ADR-0017 |
| G5 — no silent overwrite | `detect_collisions`, `raise_if_collisions` | `TestExactDiskCollision`, `TestRaiseIfCollisions` | ADR-0017 |
| G6 — overwrite backups | `backup_existing` | `TestBacksUpExistingFiles`, `TestRollbackOnFailure` | ADR-0017 |
| G7 — actionable findings | `Finding`, `SafetyError` | `TestFindingShape` (all safety test files) | ADR-0017 |

**A change to any of these primitives must update three things together:** the code, the corresponding test class (which is the *contract* for that primitive), and this document (which is the *narrative* for the layer as a whole). A change that updates the code and the tests but not this document would be a documentation debt; a change that updates the document but not the tests would be an unenforced promise.

---

## 8. Reporting a vulnerability

The safety layer is security-relevant code, and vulnerabilities in it are treated accordingly. If you find a way to make the layer violate any of the guarantees in §2, please report it via the process in `SECURITY.md` rather than opening a public issue. A working exploit — even a hypothetical one — is treated as a security bug regardless of whether it requires an unusual environment to trigger.
