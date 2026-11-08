"""Enable ``python -m forge.cli`` as an alternate entry point.

This module exists so that:

1. Windows users whose ``PATH`` does not include ``Scripts/`` can run
   ``python -m forge.cli`` and get the same behaviour as ``forge``.
2. Contributors running tests via ``sys.executable -m forge.cli`` get
   a portable invocation that works on every OS.
3. Future container images or CI jobs that avoid modifying ``PATH``
   can invoke Forge without a symlink.

The behaviour is identical to ``forge`` because it calls the same
``main()`` function. The ``if __name__ == "__main__"`` guard is
required for ``python -m`` execution: without it, importing
``forge.cli.__main__`` (which the ``-m`` machinery does) would
execute the CLI, and any tooling that imports the module to inspect
it would launch the process. The guard confines execution to the
actual ``-m`` invocation.

``main()`` is declared ``NoReturn``; the ``SystemExit`` it raises is
caught by the interpreter's ``-m`` runner, which forwards the code
to the process exit status. No explicit ``sys.exit`` wrapper is
needed — and adding one would be a bug, because it would double-wrap
the exit and could mask the code inside ``SystemExit.__init__``.
"""

from __future__ import annotations

from forge.cli.main import main

if __name__ == "__main__":
    main()
