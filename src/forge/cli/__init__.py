"""Forge CLI — the command-line interface package.

The CLI's entry point is ``forge.cli.main:main`` (see ``[project.scripts]``
in ``pyproject.toml``). This package's ``__init__.py`` intentionally does
**not** re-export ``main`` as a package attribute.

Why not re-export?
------------------
Re-exporting ``main`` from here would bind the *function* ``main`` to the
attribute ``forge.cli.main`` — which shadows the *module*
``forge.cli.main``. After that shadowing:

- ``import forge.cli.main as mod`` still returns the module (the import
  machinery bypasses ``__init__.py``), but
- ``monkeypatch.setattr("forge.cli.main.<anything>", ...)`` and any other
  attribute-lookup-based access resolves to the *function*, not the module,
  and fails.

This is a well-known Python footgun. The fix is not to rename the function
(it is referenced by ``[project.scripts]`` and by tests), but to avoid
publishing it under the same name as the submodule it lives in.

How to import the public API
----------------------------
Consumers who want the function should import it from its module::

    from forge.cli.main import create_app, main

Consumers who want the module (e.g., for monkeypatching in tests)::

    import forge.cli.main as main_module

Both work. Neither is shadowed by this file.

What is exported here
---------------------
Nothing. ``__all__`` is empty. This is deliberate. The package's public
API is reached through ``forge.cli.main``, not through ``forge.cli``.
Keeping ``__init__.py`` empty means:

- ``forge.cli.main`` is always the module.
- There is no ambiguity about where ``main`` lives.
- A future contributor cannot accidentally reintroduce the shadowing by
  adding a re-export — they would have to edit this file, which has a
  docstring explaining why not to.
"""

from __future__ import annotations

# Intentionally empty. Do not add ``from forge.cli.main import ...`` here.
# See the module docstring for the reason.
__all__: list[str] = []
