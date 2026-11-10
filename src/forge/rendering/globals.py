"""The globals allowlist — deliberately, defensibly empty.

A "global" in Jinja is a name a template can reference without it being in
the render context: `{{ range(10) }}`, `{{ lipsum() }}`, `{{ dict(a=1) }}`.
Every global is an implicit capability grant, and every capability is:

    * **Attack surface** — `lipsum()` generates megabytes, `cycler` and
      `joiner` are stateful, `dict()`/`namespace()` create objects whose
      attributes can be probed in ways the sandbox must then defend
      against. The safest global is the one that does not exist.
    * **A determinism hazard** — Jinja's stock `range` is fine, but
      `lipsum` uses random text, `cycler` depends on call order, and any
      future global that touches time, environment, or random state
      would break the "same template + same variables ⇒ byte-identical
      output" contract (ADR-0014).
    * **A surprise** — a template author who can see `range` will use it,
      which then requires it in tests, docs, and eventually a security
      review to remove. Starting from nothing means anything added later
      was *asked for*, not inherited.

Design principle: templates should be entirely driven by their context.
If a template needs to loop, iterate over a variable the blueprint author
declared (`{% for item in items %}`). If it needs an enumerated range,
that range belongs in the variables map — where it is validated, typed,
and documented as part of the blueprint's contract.

Adding a global
---------------
This is not a convenience change; it is a capability change. Any addition
requires:

    1. A security review note in the PR explaining why the context is
       insufficient.
    2. An entry in `docs/reference/TEMPLATE_FILTERS.md` under "Globals".
    3. An update to `tests/unit/rendering/test_globals_docs.py` (the
       doc-sync test, mirror of the filter one).
    4. A determinism test proving the global does not depend on time,
       randomness, environment, or iteration order.

Until then, this dict is empty — and that is a feature.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only import: ``jinja2.Environment`` is the only parameter
    # type ``install`` needs, and importing it here means this module
    # does not pull Jinja into sys.modules at import time. That matters
    # because ``forge.rendering.globals`` is imported by
    # ``forge.rendering``'s façade, which the public API promises is
    # importable without jinja2 unless a renderer is actually used.
    from jinja2 import Environment

# Intentionally empty. See module docstring for the reasoning and for the
# process required to add anything here.
#
# Examples of what this deliberately does NOT contain, and why:
#
#   "range"    — encourages range-based loops whose length comes from
#                template logic rather than declared variables. Use
#                `{% for i in range_list %}` with `range_list` in the
#                blueprint's variables instead.
#   "dict"     — creates objects whose attribute probing expands the
#                sandbox's threat model. Use a variable.
#   "lipsum"   — non-deterministic (random text). Forbidden by ADR-0014.
#   "cycler"   — stateful; iteration order and state depend on call
#                sequence, which interacts badly with template inheritance
#                and macro reuse.
#   "joiner"   — same statefulness concern as cycler.
#   "namespace" — creates mutable attribute-bearing objects specifically
#                to work around template immutability; the sandbox forbids
#                attribute write attempts, so allowing this would invite
#                confusing runtime errors.
#   "url_for"  — Flask-specific, and it does I/O; nothing about a
#                scaffolded project depends on it.
ALLOWED_GLOBALS: dict[str, object] = {}


def install(environment: Environment) -> None:
    """Install the allowlist on an environment, replacing Jinja's defaults.

    `clear()` is essential and non-negotiable. Jinja copies its default
    globals (`range`, `dict`, `lipsum`, `cycler`, `joiner`, `namespace`,
    and in some configurations `url_for`) into every new `Environment`
    at construction. Calling only `update(ALLOWED_GLOBALS)` would leave
    all of them reachable — the empty allowlist would be a lie.

    Idempotent by design: called from `get_environment()` once and from
    `ForgeSandboxedEnvironment.overlay()` on every per-render clone, so
    that a future change to Jinja's overlay semantics cannot silently
    restore default globals.
    """
    environment.globals.clear()
    environment.globals.update(ALLOWED_GLOBALS)
