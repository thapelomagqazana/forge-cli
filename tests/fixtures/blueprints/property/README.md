# Property-test seed corpus

These YAML files are **not** exhaustively tested fixtures in the usual
sense. They are the *seed corpus* for the Hypothesis-driven
schema↔model parity test (`tests/unit/models/test_schema_model_parity.py`,
ADR-0008).

## Why a seed corpus

Hypothesis generates documents by mutating examples from a strategy.
Left to itself, the strategy will explore a broad space across runs, but
the *specific* examples it tries on any given run depend on the
Hypothesis version, the Python version, the random seed, and the phase
ordering. That makes a failure reproducible *on the failing machine* but
often not on another developer's.

`@example(...)`-decorated tests and `@seed(0)` fix that, but they force
us to hand-write the examples in Python and then embed YAML literals —
noisy and hard to keep in sync with the schema. The seed corpus takes the
third path: put the seeds in files, load them once in a module-scoped
fixture, and feed them to `@example` via a parametrised wrapper.

The result:

- **Deterministic:** every run starts from these exact documents, in
  file order, before Hypothesis explores further.
- **Reviewable:** a contributor can open a YAML file and see the exact
  document that is seeded, without decoding a Python literal.
- **Extensible:** adding a new invariant means adding a file here, not
  editing test code.

## What belongs here

Only **valid** documents. The parity test asserts that both the schema
and the models *accept* every seed, and that mutating the seed in
specific ways causes *both* to reject. Invalid documents are constructed
by mutation inside the test, because that is the property under test:
"any mutation the schema rejects, the model rejects too." Hand-written
invalid fixtures would test a weaker property ("this one specific
mutation is rejected"), and the mutation space is too large to enumerate
by hand.

The complementary hand-written invalid fixtures live in
`tests/fixtures/blueprints/invalid/` and are consumed by
`test_schema_rejects.py` — a different, narrower test that pins the
*specific* error pointers for the four cases named in the schema
acceptance criteria. Those are the documents a user is likely to
actually write; this directory is the corpus Hypothesis starts from.

## Naming convention

`NN-<what-is-distinctive>.yaml` — the numeric prefix enforces load
order (Python's `Path.glob` does not guarantee it), and the descriptive
suffix makes a failing seed identifiable from a test log line.

Never renumber an existing file: the test's `@example` order is
meaningful for reproduction. Append new files at the next free number.
