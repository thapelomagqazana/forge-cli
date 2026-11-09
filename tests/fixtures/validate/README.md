# `forge validate --json` — golden fixtures

These files are the **frozen artefacts** that
`tests/contract/test_validate_output_contract.py` snapshot-locks. They
exist so that a change to the JSON envelope's shape — a new field, a
renamed field, a bumped `schema_version` — cannot land without a
reviewer seeing the diff as a *contract change*, not as a test
adaptation.

## Files

| File | Represents | Consumer |
|------|-----------|----------|
| `valid.json`   | The envelope for a blueprint that validates cleanly | CI happy-path, IDE "no errors" branch |
| `invalid.json` | The envelope for a blueprint with two schema violations | CI error branch, IDE "render errors" branch |

## Regeneration policy

**Do not hand-edit these files.** They are byte-for-byte the output of
`render_validate_json(ValidateOutput(...))` — the same pure function the
CLI calls. Editing them by hand risks a fixture that does not match what
the command produces, which would make the test fail even though the
command is fine.

When a shape change is *intentional*:

1. Change the code (`ValidateOutput`, `_render_json`).
2. Change `docs/reference/CLI_VALIDATE_OUTPUT.md` to match.
3. Regenerate:
   ```bash
   UPDATE_SNAPSHOTS=1 uv run pytest tests/contract/test_validate_output_contract.py
   ```
4. Re-run without the env var to confirm:
    ```bash
    uv run pytest tests/contract/test_validate_output_contract.py
    ```
5. Review `git diff tests/fixtures/validate/` as a **contract diff** —
the question is "is this the new contract, and do we accept it?",
not "do the tests still pass?".
6. Land the code, doc, and fixture changes in one commit
