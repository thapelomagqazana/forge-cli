"""Enable ``python -m forge.cli`` as an alternate entry point.

This module exists so that:

1. Windows users whose ``PATH`` does not include ``Scripts/`` can run
   ``python -m forge.cli`` and get the same behaviour as ``forge``.
2. Contributors running tests via ``sys.executable -m forge.cli`` get
   a portable invocation that works on every OS.
3. Future container images or CI jobs that avoid modifying ``PATH``
   can invoke Forge without a symlink.

The behaviour is identical to ``forge`` because it calls the same
``main()`` function.

Why the ``if __name__ == "__main__"`` guard is required
-------------------------------------------------------
``python -m forge.cli`` does two things, in order:

1. Import ``forge.cli.__main__`` (the module named by ``-m``).
2. Execute the module's top-level code.

If the module's top level unconditionally called ``main()``, importing
``forge.cli.__main__`` -- which any tool that enumerates or introspects
the CLI would do (pytest's collector, importlib-based wheel inspectors,
IDEs, documentation builders) -- would launch the CLI process. That is a
surprising side effect at import time, and a real footgun: a pytest run
that walks the tree could hang on an interactive wizard invoked by an
accidental import.

The guard confines execution to the actual ``-m`` invocation. At that
point Python sets ``__name__ == "__main__"`` for the module it is
executing, and only then does ``main()`` run.

Why no explicit ``sys.exit`` wrapper
------------------------------------
``main()`` is declared ``-> NoReturn`` and terminates by raising
``SystemExit``. When a module is run via ``python -m``, the interpreter's
runpy machinery catches that ``SystemExit`` and forwards its ``code``
attribute to the process exit status. Wrapping ``main()`` in
``sys.exit(main())`` here would:

* double-wrap the exit -- ``main()`` already raises ``SystemExit``, and
  the outer ``sys.exit()`` would catch that raise, discard its code, and
  raise a *new* ``SystemExit`` whose code is the (nonexistent) return
  value of ``main()``. It happens to work only because Python's exit
  handling takes the innermost raise; that is accidental, not designed,
* mask the ``SystemExit`` ``code`` if it is a non-int (for example, a
  string message), because ``sys.exit()`` stringifies non-int arguments
  to stderr on some paths.

The no-wrapper form is the canonical pattern for a ``__main__`` module
whose entry point already raises ``SystemExit``.

Why the module imports ``main`` from ``forge.cli.main`` (not from
``forge.cli``)
----------------------------------------------------------------------
``forge.cli.main`` is the module that *defines* ``main()``.
``forge.cli`` (the package ``__init__``) re-exports it for callers that
prefer the short path. Importing the definition site directly here is
deliberate:

* It makes the dependency explicit. ``__main__`` depends on the module
  named ``main``, and anyone reading this file sees that immediately.
* It avoids a subtle failure mode: if ``forge.cli.__init__`` later grows
  an import that transitively imports ``__main__`` (for documentation,
  for a plugin discovery mechanism, or by accident), the package import
  would cycle through the entry module. Importing the definition site
  sidesteps the whole category.

If a future refactor moves ``main()`` to a different module, this import
is the one line that changes, and the change is visible at the one place
that needs it.

Why this file is ASCII-only
---------------------------
The file's job is to be trivially readable and trivially correct. Em
dashes, curly quotes, and other non-ASCII punctuation do not add
meaning here, and they are a recurring source of corruption when a file
is opened, saved, or copied between tools with different default
encodings. Plain ASCII on this file means every tool, on every
platform, in every locale, reads it exactly as written. Use ``--`` for
an em dash and ``'`` for a curly apostrophe; leave the pretty
typography to prose that is not code.
"""

from __future__ import annotations

from forge.cli.main import main

if __name__ == "__main__":
    main()
