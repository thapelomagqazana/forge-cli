# Conditions fixtures

Blueprints and templates used by:

    tests/unit/conditions/                     — parser, evaluator, errors
    tests/integration/test_conditions_*.py     — end-to-end via create pipeline

## Conventions

1. **Every fixture is a complete, schema-valid blueprint.** Loader-level
   failures have their own fixture directory (`tests/fixtures/loader/`).
   The conditions subsystem is exercised only on documents that already
   passed the loader — that mirrors production and keeps the failure
   domain of each fixture obvious.

2. **Intentional failure fixtures are named after the failure.** A file
   called `bad-syntax.yaml` failing at parse time is not a bug; it is
   the point. The `# EXPECTED-FAILURE:` header comment in each such
   file names the *first* failure the test suite asserts on.

3. **No file references anything outside its own directory.** Templates
   live under `templates/` here, not under `tests/fixtures/blueprints/`,
   so a conditions fixture cannot break when a different fixture set is
   reorganised. Loader-level "missing template" checks are exercised in
   `tests/fixtures/loader/`, not here.

4. **Templates are minimal.** They exist so the loader's template-existence
   check passes; their *content* is exercised by the rendering fixtures
   (`tests/fixtures/templates/`), not by the conditions fixtures. Keeping
   them stubs prevents rendering concerns from leaking into condition
   tests.

5. **Fixtures declare their expected outcome in the header.** Every file
   starts with a `# EXERCISES:` comment naming the scenarios it drives
   (e.g. "equality-true, equality-false, undefined-variable"). Test
   files reference fixtures by that name, so renaming a fixture is
   always accompanied by a test-suite update — no fixture rots silently.

## How tests load a fixture

    from pathlib import Path
    import yaml
    from forge.verification import validate_blueprint_document

    _FIX = Path(__file__).parents[2] / "fixtures" / "conditions"

    def _load(name: str):
        doc = yaml.safe_load((_FIX / name).read_text())
        return validate_blueprint_document(doc)

Never construct a `Blueprint` by hand in these tests — always go through
the loader's façade. That keeps the fixture corpus and the tests honest
about what a real blueprint looks like, and it means loader changes
propagate to the conditions tests automatically (which is what you want:
a real loader that stops accepting a fixture *is* a signal).
