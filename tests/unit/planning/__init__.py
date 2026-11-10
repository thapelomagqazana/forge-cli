"""Planning-layer unit tests.

Marking this directory a package changes pytest's import mode
for its contents from ``prepend`` to qualified names — see the
safety package's ``__init__.py`` for the full explanation.

``planning/`` and ``safety/`` both contain a ``test_collisions.py``
and a ``test_destinations.py``; the collision between those
basenames was what surfaced the collection failure that motivated
the package markers. The two directories now import as
``tests.unit.planning.test_collisions`` and
``tests.unit.safety.test_safety_collisions`` respectively, so the
names no longer collide.

The renamed safety-layer files (``test_safety_collisions.py``,
``test_safety_destinations.py``) are the *semantic* half of the
fix — the package markers alone would have resolved the collision,
but the ``test_safety_`` prefix also makes it clear which layer
each file tests.
"""
