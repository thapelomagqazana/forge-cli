"""Environment construction — one per process, cached, thread-safe.

Why a dedicated module?
-----------------------
The Jinja2 `Environment` is the *single* place where all render policy is
decided: which filters exist, which globals exist, whether undefined
values are strict, how whitespace is handled, and whether templates can
reach outside their sandbox. Concentrating that policy in one file means:

    * a security reviewer has exactly one file to audit for environment
      configuration (filters/globals live next to it, sandbox hardening
      lives in `_sandbox.py`),
    * the renderer (`renderer.py`) can stay a thin I/O + error-translation
      layer with zero Jinja knowledge,
    * tests can build the environment once and assert its properties
      without spinning up a full render.

Caching rationale
-----------------
Constructing an `Environment` is expensive (bytecode cache setup,
autoescape scanning, filter wiring). Doing it per render would dominate
render time on real projects. `lru_cache(maxsize=1)` gives us a single
shared instance. Jinja's `Environment` is *immutable after configuration*
and its internal template cache is thread-safe, so sharing it across
threads is safe as long as we never mutate it post-construction — which
we don't, and which `overlay()` (below) is designed to preserve.

Why not autoescape?
-------------------
Forge generates *code* — Python, TypeScript, JSON, YAML, Dockerfiles.
Autoescaping rewrites `<`, `>`, `&`, and `"` into HTML entities. For code
output that is wrong, silent, and hard to debug (a generated Python file
would contain `&lt;`). HTML-oriented blueprints can opt in explicitly by
using a future `| e` filter, but the default must not corrupt code.
"""

from __future__ import annotations

from functools import lru_cache

from jinja2 import FileSystemLoader, StrictUndefined

from forge.rendering import filters
from forge.rendering import globals as globals_mod
from forge.rendering._sandbox import ForgeSandboxedEnvironment


@lru_cache(maxsize=1)
def get_environment() -> ForgeSandboxedEnvironment:
    """Return the process-wide, configured, sandboxed environment.

    Cached. Safe to share across threads. Callers **must not** mutate the
    returned object — use `overlay()` (see `_sandbox.ForgeSandboxedEnvironment`)
    if a different loader or context is required.

    Configuration is intentional and reviewed:

    * `undefined=StrictUndefined`
        Missing variables raise `UndefinedError` instead of rendering as
        empty. Empty strings in generated code are the single most
        common source of silent-bug-in-production for scaffolding tools;
        we choose loud failure.

    * `autoescape=False`
        We emit code, not HTML. See module docstring.

    * `trim_blocks=False`, `lstrip_blocks=False`, `keep_trailing_newline=True`
        Whitespace is preserved exactly as authored. Generated files must
        match what blueprint authors wrote; any post-processing here
        would make golden tests brittle and diffs noisy.

    * `extensions=[]`
        No optional Jinja extensions. Extensions add capability and
        therefore attack surface; the allowlist principle applies to
        extensions too. Adding one requires an ADR.

    * loader is `None` at the base level
        Each render attaches a per-blueprint `FileSystemLoader` via
        `overlay()` so `{% include %}` and `{% extends %}` are confined
        to the template's own directory. The base environment has no
        loader by design, so an un-overlaid use cannot accidentally
        resolve files.
    """
    environment = ForgeSandboxedEnvironment(
        loader=None,
        undefined=StrictUndefined,
        autoescape=False,
        trim_blocks=False,
        lstrip_blocks=False,
        keep_trailing_newline=True,
        extensions=[],
    )

    # Install allowlists *after* construction so Jinja's built-in filters
    # and globals cannot survive by accident. `install()` clears then
    # sets, so this is idempotent and auditable in one line per module.
    filters.install(environment)
    globals_mod.install(environment)

    return environment


def make_loader(root: str) -> FileSystemLoader:
    """Return a loader scoped to a single blueprint directory.

    Security properties:

    * `searchpath=[root]`
        The *only* directory templates can read from. `{% include
        "../secrets" %}` and absolute paths fail with `TemplateNotFound`.

    * `followlinks=False`
        A symlink inside a blueprint that points outside the blueprint
        directory will not be followed. This blocks the
        "symlink-to-/etc/passwd" class of include-traversal attacks.

    * `encoding="utf-8"`
        Pinned explicitly (rather than relying on locale) so behaviour
        is identical across machines and CI images. Non-UTF-8 templates
        fail loudly in `renderer.py` with a clear hint.

    * `get_source` is not overridden
        We keep Jinja's stock implementation; overriding it would be a
        security-sensitive change requiring its own review. If we ever
        need custom loading (e.g. a database-backed blueprint), a new
        source class should be introduced here — never ad-hoc in a
        renderer.
    """
    return FileSystemLoader(
        searchpath=[root],
        followlinks=False,
        encoding="utf-8",
    )
