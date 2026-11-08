# Local blueprint fixtures

On-disk blueprint trees used by discovery tests that need a real
filesystem layout — as opposed to a fake `Source` or a monkeypatched
`Path`. Each subdirectory mimics what a user would place under
`.forge/blueprints/`.

## Conventions

- **One directory per blueprint.** The directory name should match the
  manifest's `id` field. The resolver keys on the manifest id, not the
  directory name, so a mismatch is tolerated at runtime — but these
  fixtures keep them aligned so tests that assert on paths stay
  readable.
- **`valid/`** — manifests that pass schema and model validation.
  Tests that need "a real local blueprint" copy one of these into
  `tmp_path` and point a `LocalDirectorySource` at it.
- **`malformed/`** — manifests that fail validation in specific,
  named ways. Each subdirectory's name describes the failure
  (`invalid_yaml`, `invalid_schema`, `missing_id`) so a test can pick
  the failure mode it needs without reading the file.
- **`duplicates/`** — manifests whose `id` intentionally collides
  with one in `valid/`. Used to test precedence: local overrides
  official, and (in this case) local overrides another local copy
  from a different directory.

## Why files on disk

Some failures can only be caught with real files:

- `importlib.resources` behaves differently for real wheels vs.
  editable installs. A fixture on disk exercises the same code path.
- Path resolution (`Path.cwd() / ".forge" / "blueprints"`) is
  meaningful only when there is an actual directory to resolve to.
- A test that monkeypatches `Path.read_bytes` proves the error
  handling; a test that reads a real file proves the loader can find
  the file at all. Both matter; the second is what these fixtures
  provide.

## Adding a fixture

1. Create `<category>/<descriptive_name>/blueprint.yaml`.
2. Keep the manifest minimal — a fixture that exercises every construct
   is `valid/custom.full`, not the default.
3. Add a comment at the top of the manifest naming the fixture's
   purpose (e.g. `# malformed/invalid_yaml: refuses to parse`), so a
   reader scanning the tree knows what each directory is for.
4. If the fixture is used by more than one test, add it to that test's
   docstring so future readers can find its dependents.

## Deleting a fixture

Grep for the directory name first. Fixtures are referenced by string
path in tests, so a `git grep` is the cheapest way to confirm nothing
depends on them.
