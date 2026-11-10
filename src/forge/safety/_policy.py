"""The single safety policy — every rule, in one file, with its reasoning.

Why a policy object exists
--------------------------
A safety layer's rules are its *contract with users*. Two failure modes
destroy that contract:

    * **Rules scattered across modules.** A path-length limit lives in
      `paths.py`, a symlink toggle in `destinations.py`, an overwrite
      default in `collisions.py`. Changing "we no longer allow symlinks"
      becomes a multi-file search, and a reviewer cannot answer "what
      does the safety layer allow?" without reading six files.

    * **Rules as inline constants.** `if len(path) > 400:` in five
      modules. The constant looks like "a detail", so it gets tuned
      independently, and now the safety layer has three different
      notions of "too long".

The policy object solves both: one file, one set of rules, and a single
object passed (by default) to every check. The reasoning for each rule
lives *next to* the rule, so the "why" cannot be separated from the
"what" in a future refactor.

Why frozen
----------
A policy that can be mutated at runtime is a policy that *will* be
mutated at runtime — by a plugin, a test that forgot to restore, an
`atexit` handler. Any of those turns a security check into a
configuration puzzle. Frozen + slots means:

    * mutation raises (a bug, not a feature),
    * hashable (usable as a cache key, comparable in tests),
    * safe to share across threads without locks,
    * cheap to construct in tests (`SafetyPolicy(overwrite_requires_backup=False)`).

A caller that *needs* different rules constructs a new policy and
passes it — that is explicit, reviewable, and local. Two policies can
never accidentally affect each other.

Why `DEFAULT_POLICY` is a module constant
-----------------------------------------
`DEFAULT_POLICY = SafetyPolicy()` is constructed once and shared. Two
consequences:

    * **Identity stability.** `policy is DEFAULT_POLICY` is true for
      every default-arg call. A test can assert "the CLI used the
      default policy", and future caching (`functools.lru_cache`) can
      key on policy identity.

    * **A single source of truth.** There is one object whose values
      define "the production rules". If someone changes a default in
      the `SafetyPolicy` class, `DEFAULT_POLICY` reflects it
      immediately — no parallel constant to keep in sync.

Why some knobs exist and others do not
--------------------------------------
Every field below answers a real question the safety layer must
answer. Fields that were considered and *rejected* are documented at
the bottom, so a future contributor knows the omission was deliberate,
not an oversight. That is what keeps a policy object from becoming a
dumping ground for "any setting we might want".
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class SafetyPolicy:
    """Immutable rule set for the filesystem safety layer.

    All fields have defaults that encode the *most restrictive*
    behaviour we can defend. Loosening any of them is a decision that
    should be justified in a code review, not enabled by accident.
    """

    # ------------------------------------------------------------------
    # Path length
    # ------------------------------------------------------------------
    # 400 characters is below every relevant filesystem's limit
    # (PATH_MAX on Linux is 4096, but the *practical* limit for a
    # single path component is 255, and stacking components hits
    # filesystem-specific walls well before 4096). We are not
    # protecting against the OS; we are protecting against accidental
    # or adversarial "very long path" bugs, where the *bug* is being
    # hidden by a permissive value. A 400-char limit means "this is
    # almost certainly not a real destination" — no legitimate
    # scaffolded path exceeds it, and a hostile one is rejected with
    # a clear message.
    max_path_length: int = 400

    # ------------------------------------------------------------------
    # Characters never allowed in a path
    # ------------------------------------------------------------------
    # NUL is checked separately (its presence anywhere is fatal and
    # deserves its own finding code). Newlines and carriage returns
    # deserve the same treatment but are grouped under one code
    # because they share a single remediation ("remove the offending
    # character"). The set is deliberately small: expanding it is a
    # policy change, and the default must be the smallest set that
    # closes the known classes of bug.
    forbidden_chars: frozenset[str] = field(default_factory=lambda: frozenset({"\n", "\r"}))

    # ------------------------------------------------------------------
    # Symlink policy
    # ------------------------------------------------------------------
    # Default: reject symlinks *anywhere* in a destination path. The
    # reasoning:
    #
    #   * A symlink at any component can redirect a write outside the
    #     root, even if the *resolved* path (after following the link)
    #     is inside — because the *link itself* may later be retargeted
    #     (a classic TOCTOU vector).
    #
    #   * A symlink at the *leaf* is a redirect of a file, not a file
    #     — overwriting it writes through it, not to it.
    #
    #   * A symlink at an *intermediate* component is a redirect of a
    #     directory — writing "under" it writes wherever it points,
    #     which may be outside the root.
    #
    # Legitimate blueprints do not need symlinks. A future blueprint
    # that does (e.g. "create a symlink to a config file") is a
    # *generation* concern, not a *safety-check* concern — it belongs
    # in the template layer with an explicit, documented escape, not
    # in the policy default.
    allow_symlinks_in_destination: bool = False

    # ------------------------------------------------------------------
    # Overwrite policy
    # ------------------------------------------------------------------
    # When `--overwrite` is passed, must a backup be created? Default:
    # yes. The two values have very different consequences:
    #
    #   * `True`  — overwrite is *recoverable*: a user who regrets it
    #               can restore from `*.bak.<ts>`. Failures during the
    #               overwrite do not lose data.
    #
    #   * `False` — overwrite is *destructive*: a user who regrets it
    #               has nothing to restore. A partial overwrite is
    #               unrecoverable.
    #
    # No CLI flag currently sets this to `False`. The knob exists so
    # that a future scenario (e.g. a CI-specific "no-backups, trust
    # me" mode) is a documented policy change rather than an
    # undocumented code path. Until then, leaving it `True` means the
    # property "overwrite is safe" is enforced by the type system, not
    # by remembering to pass a flag.
    overwrite_requires_backup: bool = True

    # ------------------------------------------------------------------
    # Case-insensitive collision warnings
    # ------------------------------------------------------------------
    # The safety layer cannot know whether the target filesystem is
    # case-sensitive (Linux) or case-insensitive (macOS, Windows,
    # most Docker volumes). It *can* warn when the *plan itself*
    # contains two destinations differing only in case — a plan that
    # will behave differently on different filesystems is a bug
    # regardless of where it runs.
    #
    # Default: warn. The warning is a `Finding`, not an error — it
    # does not prevent the operation. A future "strict" mode could
    # promote it to an error, and that decision belongs in this file,
    # not scattered through collision checks.
    warn_on_case_insensitive_collisions: bool = True

    # ------------------------------------------------------------------
    # Rejected fields (documented to prevent future drift)
    # ------------------------------------------------------------------
    #
    # These were considered and deliberately *not* included. Each is
    # omitted for a specific reason; including them "for flexibility"
    # would make the policy object a compatibility surface we would
    # be locked into maintaining:
    #
    #   * `max_path_components: int` — the OS enforces its own limit;
    #     a configurable component count invites blueprints that work
    #     on Linux and fail on Windows. Length is the portable check.
    #
    #   * `allowed_extensions: frozenset[str]` — extension policy is
    #     a *blueprint* concern (which files a blueprint produces),
    #     not a *safety* concern (whether the produced files can be
    #     written safely). Mixing them would force policy changes for
    #     new file types.
    #
    #   * `require_root_exists: bool` — whether the root must already
    #     exist is a *caller* concern (create should mkdir it, dry-run
    #     should not). A policy field would freeze that decision for
    #     every caller, which is the wrong axis of configuration.
    #
    #   * `backup_suffix: str` — the suffix is part of the *file name
    #     format*, which belongs next to the code that composes it
    #     (`overwrite.py`), not in a policy object. A configurable
    #     suffix would invite drift between "what the CLI tells the
    #     user" and "what the backup actually is named".
    #
    #   * `max_file_size: int` — resource exhaustion is an OS concern
    #     (quotas, container limits). A policy limit would either be
    #     arbitrarily small (breaking legitimate blueprints with large
    #     fixtures) or arbitrarily large (not protecting anything).
    #
    # If a future requirement genuinely needs one of these, the correct
    # response is: add it *here*, with its reasoning, and update the
    # non-goals section of `docs/security/FILESYSTEM_SAFETY.md`. The
    # omission is a decision, not a gap.


# The production policy. Shared by every check by default. Constructed
# once at import; changing a default in the class changes it here too,
# without a parallel constant to maintain.
DEFAULT_POLICY = SafetyPolicy()


# A strictly-more-restrictive variant for tests that want to exercise
# "the safety layer rejects even though production would accept". Used
# in `tests/unit/safety/` to prove that turning a knob has the
# documented effect, rather than to change behaviour anywhere in
# production code.
STRICT_POLICY = SafetyPolicy(
    max_path_length=200,
    allow_symlinks_in_destination=False,
    overwrite_requires_backup=True,
    warn_on_case_insensitive_collisions=True,
)
