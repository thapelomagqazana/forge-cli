"""Fixture package marker.

Empty on purpose: fixtures are data (YAML, JSON), not code. The marker
exists only so `pytest` can discover this directory as a package when
someone imports a fixture through `tests.fixtures.dryrun` in a future
test. Adding executable code here would break the "fixtures are data"
rule the rest of the codebase's test discipline follows.
"""
