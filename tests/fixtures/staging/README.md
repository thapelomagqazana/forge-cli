# Staging fixtures

Fixtures for `forge.staging.StagingEngine` and the phases that consume
its output (`forge.staging._validate`, `forge.staging.result`).

## Files

| File | Purpose |
|------|---------|
| `blueprint.yaml` | The happy-path blueprint: one render-kind file (`main.py.j2`), one render-kind file at the root (`pyproject.toml.j2`), one copy-kind file in a subdirectory (`assets/logo.png`). Exercises directory-preservation (nested path) and kind-dispatch (render vs. copy) in a single plan. |
| `missing-var.yaml` | The failure blueprint: `main.py.j2` references `{{ undefined_thing }}` which is intentionally not declared in `variables:[]`. Used to assert that a render failure aborts the whole staging run and cleans up the staging tree. |
| `templates/main.py.j2` | Small Python template that interpolates `project_name`. Chosen to be valid Python after rendering so a future verification test can `compile()` the output. |
| `templates/pyproject.toml.j2` | Minimal `pyproject.toml` template. Renders to valid TOML so a future verification test can parse it. |
| `templates/assets/logo.png` | Four-byte PNG signature followed by zeros. Not a real PNG (would fail validation on parse) but has the correct magic bytes so `_has_known_binary_extension` recognises it and the test can assert byte-for-byte preservation without depending on a real image decoder. |

## Invariants tests rely on

- `templates/main.py.j2` renders to a string that starts with `print("Hello `. If the template content changes, update the assertion in `test_engine_happy.py::test_all_files_staged_in_preserved_structure`.
- `templates/assets/logo.png` starts with `\x89PNG`. Tests assert on the first four bytes; a different magic number would break the copy-kind assertion that the file is written byte-for-byte.
- `missing-var.yaml` declares `project_name` (so the resolver succeeds) but references `undefined_thing` in a template body (so the renderer fails). If a future test wants to fail on a *missing variable declaration* rather than a *missing reference*, that is a different fixture — don't change this one.
- No file in this tree uses absolute paths, traversal, or symlinks. Path-safety tests live in `tests/adversarial/` and use `tmp_path`-built fixtures; they never reach into this directory.

## Why these fixtures are files, not inline strings

Three reasons:

1. **Directory preservation** requires an actual subdirectory (`templates/assets/`) to prove that the engine walks the tree, not just a flat list.
2. **Binary preservation** requires bytes the test cannot reasonably embed as a string literal (PNGs are the canonical example; `\x89` in a `.py` string literal is easy to get wrong across editors).
3. **Reuse across tests** — `test_engine_happy.py`, `test_copy_kind.py`, `test_engine_progress.py`, and `test_engine_invisibility.py` all read `blueprint.yaml` + `templates/`. Inline fixtures would duplicate the content four times and drift.

No `conftest.py` is needed: the tests compute `_FIX = Path(__file__).parents[2] / "fixtures" / "staging"` and read directly. That keeps each test file self-contained and avoids the "where does this fixture come from?" question that conftest indirection creates.
