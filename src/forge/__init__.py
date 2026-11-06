"""Forge CLI — the engineering foundation system for software projects.

This package exposes a small, stable, library-facing API so that external
tools can introspect Forge without importing the CLI. The public surface
is intentionally minimal — every name here is a promise to downstream
consumers, and every name carries a compatibility obligation across
minor versions.

Public API
----------
- ``forge.__version__`` — the installed Forge version, resolved from
  package metadata at import time. Equivalent to the string printed by
  ``forge --version`` (without the ``"forge, version "`` prefix).
- ``forge.get_version()`` — a callable version resolver. Use this when
  you need to force a fresh read (e.g., after a hot-reload in a dev
  server, or in a test that needs to simulate a different version).

Stability
---------
The names above are part of the stable API. Removing or renaming any of
them is a breaking change and requires a major version bump.

Everything else — ``forge.cli``, ``forge.core``, ``forge.generation``,
``forge.verification``, ``forge.manifests``, ``forge.diagnostics``,
``forge.output`` — is reachable via its fully-qualified import path but
is **not** part of the stable API. These modules may change between
minor versions. Tools that depend on them should import them directly
(``from forge.generation import ...``) rather than through the package
root, so that a future refactor does not silently break them.

Why ``__version__`` is resolved at import time
----------------------------------------------
``forge.__version__`` is set once, at import time, by calling
``get_version()``. This is the standard Python convention (see ``sys``,
``platform``, and every major library), and it means callers can write:

    from forge import __version__
    print(__version__)

without worrying about function-call overhead or lazy resolution. The
resolver itself is cached with ``@lru_cache(maxsize=1)``, so even the
callable form (``forge.get_version()``) is effectively free after the
first call.

If a caller needs to bypass the cache (rare — only in tests that
simulate a version change), they can call::

    from forge.core.version import get_version
    get_version.cache_clear()
    forge.__version__ = get_version()  # not recommended; see below

Resetting ``forge.__version__`` at runtime is **not recommended**. The
import-time value is the correct one for the process. Tests that need
to simulate a different version should patch ``forge.core.version``
directly (which is what the test suite does) rather than mutating the
package attribute.
"""

from __future__ import annotations

from forge.core.version import get_version

# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------
__version__: str = get_version()
"""The installed Forge version, resolved at import time.

Equivalent to ``forge --version`` minus the prefix. Exported for
library consumers who want a synchronous string without calling a
function::

    from forge import __version__
    print(f"Using Forge {__version__}")

For a fresh read (bypassing the process-level cache), call
``forge.get_version()`` after ``get_version.cache_clear()``.
"""

__all__ = ["__version__", "get_version"]
"""The stable public API of the ``forge`` package.

Only the names in this list are part of the compatibility contract.
Everything else is reachable via fully-qualified import paths but may
change between minor versions.
"""
