# Loading test fixtures

Shared YAML inputs for the loading pipeline tests. Each file is
referenced by at least two test modules, which is why it lives here
rather than inline in a test. A one-line fixture that is used by a
single test belongs inline in that test, not here.

## Naming convention

Files are named for the *scenario* they exercise, not for the schema
fields they touch. `missing-id.yaml` says what is wrong; `no-id.yaml`
could mean the field was never written, or that the writer removed it
by mistake. Naming by scenario makes a failing test's parameter
readable without opening the fixture.

## Consumer map

| Fixture                  | Consumed by                                         |
|--------------------------|-----------------------------------------------------|
| `malformed.yaml`         | `test_yaml_parser.py`, `test_loader_malformed_yaml.py` |
| `multi-error.yaml`       | `test_loader_multi_error.py`, `test_validate_command.py` |
| `missing-template.yaml`  | `test_loader_missing_template.py`                   |
| `unknown-field.yaml`     | `test_loader_multi_error.py`                        |
| `not-a-mapping.yaml`     | `test_yaml_parser.py`, `test_loader_malformed_yaml.py` |
| `empty.yaml`             | `test_yaml_parser.py`, `test_loader_malformed_yaml.py` |
| `non-utf8.yaml`          | `test_yaml_parser.py`, `test_loader_malformed_yaml.py` |

When you add a fixture, add its row here. When you delete a fixture,
delete its row here. When you split a fixture across files, add a row
for each.

## What does *not* live here

* **Valid blueprints.** Those live under
  `tests/fixtures/blueprints/valid/` and are also consumed by the
  schema tests, the model tests, and the discovery tests. Sharing them
  across *every* validation layer is the point.
* **Invalid-by-schema blueprints** (missing `id`, extra field, bad
  version, bad variable type). Those live under
  `tests/fixtures/blueprints/invalid/` because they are consumed by
  both the schema tests and the loader tests — the loader is proving
  it correctly *invokes* schema validation, not re-proving schema rules.
* **Discovery fixtures.** Those live under
  `tests/fixtures/local_blueprints/` because they exercise precedence
  rules, not loading.
