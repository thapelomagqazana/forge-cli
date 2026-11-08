"""Public façade for blueprint loading.

Why a façade at all
-------------------
The loading package has six internal modules with distinct jobs:

    loader.py       — orchestration (the pipeline)
    yaml_parser.py  — bytes → ParsedDocument | ValidationError
    aggregator.py   — phase raw output → ValidationError list
    fs_checks.py    — filesystem checks (existence, readability, containment)
    errors.py       — the ValidationError shape
    result.py       — the Result[T, E] sum type
    hints.py        — the path → hint catalogue

Consumers should never need to know that structure exists. `forge validate`
needs a loader and a `Result`; `forge create` needs a loader and a `Result`;
discovery needs a loader and a `Result`; a future HTTP endpoint needs a
loader and a `Result`. That is the *entire* public surface.

If callers imported from `forge.loading.loader` and
`forge.loading.result` directly, then:

    * Refactoring the internal layout (splitting `loader.py`, moving
      `result.py` into a shared module, replacing the hand-rolled
      `Result` with a library) would be a breaking change to every
      consumer, not a refactor confined to one file.
    * The public API would be implicitly defined by what happens to be
      importable, which means it grows unpredictably over time as
      contributors add helpers.
    * Tests written against internals would couple to implementation
      details rather than to the contract, and would need updating on
      every internal change.

A façade fixes all three: one file names the API, `__all__` enumerates
it, and everything else in the package is private-by-convention.

What the façade exports (and why each item)
-------------------------------------------
`BlueprintLoader`
    The service. Every consumer's entry point. If this ever needs to
    become `BlueprintLoaderV2` or gain a strategy parameter, the façade
    re-exports the new one under the same name and callers are unaffected.

`Ok`, `Err`
    Constructors for the two cases of `Result`. Exported because tests
    and any caller that builds an `Ok` or `Err` manually (e.g. mock
    loaders in unit tests) needs them. Constructors are part of the API
    even when the type is also part of the API.

`Result`
    The type alias. Exported because callers write it in type
    annotations: `def run(...) -> Result[Blueprint, list[ValidationError]]`.
    Without this export, every annotated function would import the
    alias from a private module, defeating the façade's purpose.

`ValidationError`
    The error value type. Exported because callers *render* it
    (`error.format()`, `error.message`, `error.hint`) and *test*
    against it (`assert err.path == "version"`). This is the single
    most-used type in the whole package from a caller's perspective,
    even though it is "only data".

What the façade does NOT export
-------------------------------
`Blueprint`
    Lives in `forge.models`. Re-exporting it here would create a
    circular-ish dependency (models already import from core, and a
    future models change could drag loading into it) and would blur the
    line between "loading produces blueprints" and "blueprints are
    models". Callers that need to annotate a variable as a Blueprint
    import it from `forge.models`, which is exactly the mental model we
    want.

`ParsedDocument`
    Internal to `yaml_parser.py`. A caller that needs the raw dict
    should use `yaml.safe_load` themselves — the loader's contract is
    "give me a file, get a Blueprint". Exposing `ParsedDocument` would
    invite callers to bypass the loader.

`from_phase`, `hint_for`, `check_templates`, `parse_yaml`
    Internals. The `from_phase` function is a formatting helper for the
    loader; `hint_for` is queried by the loader, not by callers;
    `check_templates` and `parse_yaml` are phase implementations. Exposing
    any of these would leak the pipeline shape and make it harder to
    change (e.g. adding a phase would require exposing the phase
    function to keep "the same" API, when in fact no caller wants that).

`DEFAULT_LOCAL_ROOT`, `BlueprintDiscoveryService`
    Live in `forge.discovery`. Discovery *uses* the loader, not the
    other way around; re-exporting discovery from loading would invert
    the dependency direction (loading would depend on discovery would
    depend on loading) and create an import cycle at package load time.

The `__all__` list is the API
-----------------------------
`__all__` is not cosmetic. It:

    * **Documents intent** — a reader of this file immediately sees
      "this package exposes four names".
    * **Enforces discipline** — `from forge.loading import *` yields
      exactly the intended names; an accidental new public name in an
      internal module cannot leak through the wildcard.
    * **Enables tooling** — IDEs, `pyright`, and `mypy --strict` use
      `__all__` to distinguish re-exports from private names, so
      "imported but unused" warnings do not fire on `Ok`/`Err`/`Result`/
      `ValidationError`, which appear "unused" from this file's local
      perspective but are the whole point.

Stability commitment
--------------------
The four names above are the loader package's public API. Their
signatures may evolve (additions in keyword-only parameters, additional
methods on `ValidationError`) but removals or renames are breaking
changes and require an ADR amendment. New public names may be added
when a consumer genuinely needs them; they will be added here, in
`__all__`, and in the corresponding docstrings — never exported
accidentally.

Tested by `tests/unit/loading/test_public_api.py`:
    * every name in `__all__` is importable and not `None`;
    * importing each name from `forge.loading` gives the same object
      as importing from the internal module (guards against accidental
      re-definition during refactors);
    * the four names are exactly the documented set (a test that fails
      loudly if `__all__` is changed without updating the docs,
      forcing a conscious decision rather than silent drift).

Import strategy: eager, not lazy
--------------------------------
Each import below is a top-level statement, not wrapped in `__getattr__`
or a `TYPE_CHECKING` guard. Reasons:

    * The four exported names are cheap to import (a class, a
      dataclass, a type alias, and their small transitive dependencies).
      `jsonschema` and `pydantic` are the heaviest transitive imports,
      and they are already imported by `loader.py`, which is imported by
      `BlueprintLoader` — so there is no avoiding them, and no lazy-load
      trick would help.
    * Eager imports fail fast: an import error inside the package
      surfaces at `import forge.loading`, not at the first call to
      `.load()`. That is a better debugging experience and a better
      signal in CI.
    * The alternative — a `__getattr__` based lazy loader — would make
      `dir(forge.loading)` and `hasattr` checks less reliable and would
      not save a measurable amount of startup time for the CLI, whose
      dominant cost is `typer` + `questionary`, not this package.

One caveat for readers who might worry about ordering: `loader.py` is
imported last, after `errors.py` and `result.py`. That is not a
correctness requirement (Python handles the transitive order), but it
keeps the "types first, service second" reading order in this file, so
the import block reads as "here is what the package is about" before
"here is what it does".
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Types first — the vocabulary a caller uses to talk about outcomes.
# ---------------------------------------------------------------------------
from forge.loading.errors import ValidationError

# ---------------------------------------------------------------------------
# The service last — the thing that produces those outcomes.
# ---------------------------------------------------------------------------
from forge.loading.loader import BlueprintLoader
from forge.loading.result import Err, Ok, Result

# ---------------------------------------------------------------------------
# Public API. Keep this list in sync with the docstring above; the API test
# asserts both directions.
# ---------------------------------------------------------------------------
__all__ = [
    "BlueprintLoader",
    "Err",
    "Ok",
    "Result",
    "ValidationError",
]
