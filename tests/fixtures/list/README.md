# `forge list` fixtures

Golden outputs for the `forge list` command, split by contract tier.

| File | Tier | Purpose | Regenerate with |
|------|------|---------|-----------------|
| `golden_official.json` | contract | Default `--json` shape | Hand-edit under ADR-0012 review |
| `golden_local_override.json` | contract | Local-wins precedence through JSON | Hand-edit under ADR-0012 review |
| `golden_stale.json` | contract | `status: "stale"` path | Hand-edit under ADR-0012 review |
| `envelope_empty.json` | contract | Empty catalogue | Hand-edit under ADR-0012 review |
| `snapshot_official.txt` | snapshot | Table appearance | `UPDATE_SNAPSHOTS=1 pytest tests/unit/cli/test_list_table.py` |
| `snapshot_empty.txt` | snapshot | Empty-state message | `UPDATE_SNAPSHOTS=1 pytest tests/unit/cli/test_list_empty.py` |
| `snapshot_truncation.txt` | snapshot | Truncation rule | `UPDATE_SNAPSHOTS=1 pytest tests/unit/cli/test_list_table.py` |

## Rules

1. **Contract fixtures are the JSON shape.** Any change here is
   consumer-visible and requires an ADR-0012 review. If a field is
   added, remove and re-add the field in a *separate commit* so the
   ADR-0012 review sees a diff that only touches that field.
2. **Snapshot fixtures are cosmetic.** They may change between minor
   releases; they exist to catch *accidental* drift, not to freeze
   layout. Regenerate intentionally, never `git checkout --`.
3. **No ANSI in any fixture.** If a fixture contains an escape
   sequence, the renderer is broken, not the fixture.
4. **No trailing newline in snapshot fixtures.** The renderer strips
   trailing whitespace and does not append a final newline. Fixtures
   match that contract byte-for-byte.
5. **Fixtures are loaded by tests, never by the product.** No `src/`
   module may import from this directory. (Enforced by a small test
   that greps `src/` for the path.)
