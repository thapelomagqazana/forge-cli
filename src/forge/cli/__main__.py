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
"""

from __future__ import annotations

from forge.cli.main import main

if __name__ == "__main__":
    main()
