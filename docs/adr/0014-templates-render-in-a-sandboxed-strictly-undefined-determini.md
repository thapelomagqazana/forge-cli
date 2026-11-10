# 0014. Templates Render in a Sandboxed, Strictly-Undefined, Deterministic Environment with a Documented Filter Allowlist

- **Status:** Accepted
- **Date:** 2026-11-10
- **Deciders:** thapelo
- **Consulted:** — (security review pending; see Follow-up actions)
- **Informed:** — (to be shared with blueprint authors via `docs/reference/TEMPLATE_FILTERS.md`)

## Context

Forge generates code from templates that ship inside blueprints. A
blueprint may be authored by the same person who runs `forge create`, but
it may equally be cloned from a third-party repository, contributed
through a pull request, or downloaded from a marketplace. The tool cannot
tell the difference between a trusted and an untrusted template, and it
should not try: **all templates are treated as untrusted input.**

Three properties matter, and none of them are provided by Jinja's default
`Environment`:

1. **Safety.** A template must not be able to execute arbitrary code,
   read or write arbitrary files, or traverse the Python object graph to
   reach `os`, `sys`, `subprocess`, or `__builtins__`. The classic
   Jinja escape chain — `{{ ''.__class__.__init__.__globals__['__builtins__']['__import__']('os').system('id') }}` —
   is public knowledge, and a rendering engine that permits it is not a
   rendering engine; it is a remote code execution vulnerability with a
   templating wrapper.

2. **Determinism.** The same template rendered with the same variables
   must produce byte-identical output. Scaffolding tools are used in CI,
   in golden-file tests, in diff reviews, and in reproducible-build
   workflows. A renderer that inserts a timestamp (`{{ now() }}`), draws
   from a random source (`{{ random() }}`), or depends on dict iteration
   order silently breaks every one of those workflows — and the failure
   surfaces as a flaky diff, not as a render error, which makes it
   expensive to diagnose.

3. **Loud failure.** A missing variable must be an error, not an empty
   string. A blank line where a class name should be produces code that
   parses and misbehaves at runtime; a render error produces a message
   that names the variable, the file, and the line. The first is a
   debugging session measured in hours; the second is measured in
   seconds. Jinja's default `Undefined` silently renders missing names
   as empty strings, which is precisely the behaviour a code-generation
   tool cannot afford.

Jinja provides building blocks for each of these — `SandboxedEnvironment`,
`StrictUndefined`, and a large default filter set — but the defaults are
pitched at HTML page rendering, not code generation. The default filter
set includes non-deterministic filters (`random`, `now`), parsing filters
that widen the attack surface (`urlize`, `xmlattr`), and attribute-access
filters (`attr`, `map(attribute=...)`, `selectattr`) that defeat the
sandbox's own attribute policy. The default globals include `lipsum`,
`cycler`, `joiner`, and `namespace`, each of which introduces either
non-determinism or mutable state. Relying on Jinja's defaults and
removing individual filters is a denylist approach: it requires
enumerating every dangerous filter, and it silently regresses whenever a
future Jinja release adds a new one.

The tension is between flexibility for blueprint authors and the three
properties above. A renderer that could do anything is dangerous; a
renderer that could do nothing is useless. The right shape is a narrow,
documented, auditable surface that covers the common scaffolding idioms
(case conversion, identifier casing, JSON serialisation, defaults,
whitespace) and refuses everything else.

## Decision

We will render all blueprint templates through a single class,
`forge.rendering.TemplateRenderer`, backed by a single cached
`ForgeSandboxedEnvironment` (a subclass of Jinja's
`SandboxedEnvironment`), configured as follows:

### Sandbox

The environment is a subclass of `jinja2.sandbox.SandboxedEnvironment`
that adds a name-based deny list of dangerous attributes on top of
Jinja's own type-based checks. The deny list covers object-graph
traversal (`__class__`, `__mro__`, `__subclasses__`, `__globals__`,
`__builtins__`, `__import__`), frame and generator introspection
(`gi_frame`, `f_globals`, `cr_frame`), historical sandbox bypass vectors
(`mro`, `registry`), and direct module names (`os`, `sys`, `subprocess`,
`importlib`). Denial raises `SecurityError`, not `Undefined`, so the
renderer can produce a `sandbox.denied` error with an actionable hint
rather than a misleading "undefined variable" message.

The deny list is **defence in depth, not a replacement for Jinja's
checks**. If a future Jinja release loosens a type-based check, or if a
novel escape is published, the name-based deny list is our independent
line of defence. If a future Jinja release hardens a check we already
cover, we gain nothing but lose nothing. This is deliberate: a
name-based deny list is invariant under Jinja version changes, and the
value of that invariance is exactly the value of a second layer.

### Strict undefined

The environment is configured with `StrictUndefined`. Any reference to a
name that is not in the render context raises `UndefinedError`, which
the renderer translates into a `TemplateRenderError` with
`code="undefined.variable"`, the offending name, the template path, and
a hint suggesting either providing the variable or using
`| default(...)`. The default is **never** an empty string.

### Determinism

The environment is configured with:

- `autoescape=False` (we generate code, not HTML; autoescaping would
  silently corrupt `<`, `>`, `&`, and `"` in generated Python,
  TypeScript, JSON, and YAML),
- `trim_blocks=False`, `lstrip_blocks=False`,
  `keep_trailing_newline=True` (whitespace preserved as authored; any
  post-processing would make golden tests brittle),
- `extensions=[]` (no optional Jinja extensions; extensions add
  capability and therefore surface area),
- empty globals allowlist (see below),
- context keys sorted before rendering (so `{% for k in ctx %}` is
  stable across machines and dict-implementation quirks),
- deterministic `tojson` filter (`sort_keys=True`,
  `ensure_ascii=False`, minimal separators),
- no time, date, random, or environment globals.

The determinism contract is stated as: **same template bytes + same
variables ⇒ byte-identical output**, and it is enforced by an
integration test that renders the same template twice and compares the
SHA-256 of the output.

### Filter allowlist

The environment's filters are cleared and replaced with a hand-curated
allowlist of fifteen filters across four categories:

- **Case:** `upper`, `lower`, `title`, `capitalize`.
- **Whitespace and simple strings:** `trim`, `replace`.
- **Identifier casing:** `snake`, `kebab`, `pascal` (all derived from a
  single normalisation routine so they agree on word boundaries).
- **Defaulting and indentation:** `default` (strict variant; does not
  substitute for `0`, `False`, or empty containers), `indent`.
- **Serialisation and structural:** `tojson`, `length`, `join`, `int`,
  `string`.

The allowlist is the **executable contract**. It is mirrored in
`docs/reference/TEMPLATE_FILTERS.md`, which is the **human contract**.
The two are kept in sync by a doc-sync test
(`tests/unit/rendering/test_filter_docs.py`) that fails the build if
either is updated without the other. The doc-sync extractor strips
fenced code blocks before extracting filter names, so a documentation
example inside a ` ``` ` fence is not mistaken for a real filter.

### Globals allowlist

The environment's globals are cleared and the allowlist is **empty**.
Templates receive everything they need through their context. Adding a
global is a security and determinism decision requiring an ADR
amendment, a doc entry, and a new determinism test — the friction is
deliberate.

### Loader scoping

Each render attaches a `FileSystemLoader` scoped to the template's
parent directory, with `followlinks=False` and `encoding="utf-8"`. This
confines `{% include %}` and `{% extends %}` to the template's own
directory and blocks symlink-based include traversal.

### Error handling

Every failure is a `TemplateRenderError` returned via
`Result[str, TemplateRenderError]`, never an exception. Each error
carries the template path, line and column (where Jinja can locate the
failure), the offending name (when applicable), a machine-readable
`code`, and a mandatory non-empty `hint`. Empty hints are rejected at
construction time — an error without a hint is a bug in the caller, not
a valid error.

### Single boundary

No module outside `forge.rendering` imports Jinja. Consumers use the
public façade (`TemplateRenderer`, `TemplateRenderError`,
`RenderResult`, `Ok`, `Err`) and nothing else. The façade deliberately
does not export the environment, the sandbox class, the filter or global
allowlists, or the loader factory. Reaching into a private module for
any of those is a deliberate act that shows up in code review — the
friction is the point.

## Consequences

### Positive

- **Safety.** Templates cannot execute code, read files outside their
  directory, traverse the object graph, or reference ambient globals.
  The two-layer defence (Jinja's type-based checks plus our name-based
  deny list) means a future Jinja hardening regression does not silently
  become a Forge vulnerability.

- **Determinism.** Byte-identical output for identical input. This
  enables golden-file tests, reproducible builds, meaningful diffs, and
  caching keyed on `(template, variables)`. The contract is tested, not
  merely documented.

- **Loud failure.** Missing variables, unknown filters, syntax errors,
  and sandbox denials all produce structured errors with actionable
  hints. A blueprint author who runs `forge create` gets a diagnostic
  naming the file, line, and fix — not a rendered template with a blank
  line where a class name should be.

- **Auditability.** The allowlists (filters, globals, deny list) are
  three small files that a security reviewer can read in minutes. The
  doc-sync test guarantees the human-readable contract matches the
  executable one.

- **Ergonomics.** The common scaffolding idioms — casing, identifier
  conversion, JSON serialisation, defaults, whitespace — are covered by
  fifteen filters. Blueprint authors do not need to reach for Jinja's
  full set.

- **Future-proofing.** Adding a filter or a global is a documented,
  reviewed change, not a silent side effect of a Jinja upgrade. The
  allowlist principle means the surface does not grow unless someone
  deliberately expands it.

### Negative

- **Narrow surface.** Blueprint authors who want Jinja's full filter set
  — or who port a template from a stock Jinja environment — will find
  some filters missing. The doc's "Filters that are deliberately not
  available" section explains each omission, and the ADR process exists
  to add a filter when a genuine need emerges. This is a real cost, paid
  in exchange for the safety and determinism guarantees.

- **Autoescape off.** Templates that generate HTML must opt into
  escaping explicitly (via a future `|e` filter, which does not yet
  exist) or use a blueprint family with its own environment. In
  practice, Forge generates code, not HTML, so this has been a non-issue
  — but it is a conscious limitation, not an oversight.

- **Deny-list maintenance.** The sandbox's `_DENIED_ATTRS` is a
  curated set that must be extended when new escape vectors are
  published. This is ongoing work, not a one-time setup. It is
  mitigated by the fact that deny-list additions are safe-by-default
  (denying a name that turns out to be legitimate is a bug report;
  allowing a name that turns out to be dangerous is a CVE).

- **Doc-sync burden.** Every filter addition requires updating three
  places (implementation, `ALLOWED_FILTERS`, doc row) and the
  parameterised filter test. This friction is deliberate but real; a
  contributor who adds a filter without updating the doc will see two
  failing tests (the doc-sync test and the parametrised test), which is
  loud enough to be discoverable but quiet enough not to block a
  focused change.

- **`Result` ergonomics.** Callers must handle `Err` explicitly; they
  cannot rely on exceptions bubbling up. This makes call sites more
  verbose than a `try/except`, and a caller who unwraps with
  `.unwrap_or()` loses the error information. The trade is that batch
  renderers can collect every failure in one pass — which the loader
  (ADR-0010) and the validator (ADR-0013) already exploit.

### Follow-up actions

- [ ] Obtain a formal security review of `_sandbox.py` and the deny
      list from a reviewer outside the maintainer group. The review
      should check the deny list against published Jinja escape
      catalogues (e.g. the PayloadsAllTheThings Jinja2 section) and
      confirm each name's inclusion is justified.
- [ ] Add a `|e` (escape) filter, or a documented alternative, for
      blueprint families that need HTML output. This is blocked on the
      first such blueprint existing; it is not speculative work.
- [ ] Add a per-render CPU budget (or an explicit "not supported, rely
      on OS-level limits" note) so a pathological template cannot pin a
      CI runner. Currently out of scope (see ADR's "Non-goals" in
      `docs/security/TEMPLATE_SANDBOX.md`); revisit when a hostile
      template is encountered in practice.
- [ ] Add a determinism property test using Hypothesis: generate
      arbitrary templates from the allowlisted filter set and assert
      that two renders of the same (template, variables) are
      byte-identical. This is a stronger version of the current
      two-render test and catches determinism bugs the fixed example
      might miss.
- [ ] Extend `TEMPLATE_SANDBOX.md` with a concrete threat model for
      include traversal (`../` escapes, symlink escapes, encoded
      escapes) once the symlink-handling code is confirmed to be the
      only loader configuration.
- [ ] Publish a short migration guide for blueprint authors porting
      templates from stock Jinja: what filters to replace with what,
      and why.

## Alternatives Considered

### Alternative 1: Jinja's default environment with a denylist of dangerous filters

Use `jinja2.Environment` (not `SandboxedEnvironment`), configure
`StrictUndefined`, and remove the dangerous filters (`random`, `now`,
`urlize`, `attr`, etc.) via `environment.filters.pop(...)`.

**Rejected because:** a denylist is an ongoing maintenance burden and a
security liability. Every Jinja release can add a filter, and the
denylist would need to enumerate it before it becomes exploitable. The
choice between "silently insecure for one release" and "regularly audit
Jinja's changelog" is not a choice a security-sensitive tool should
have to make. An allowlist is finite and auditable; a denylist is
infinite and reactive.

**Additionally:** without `SandboxedEnvironment`, the `attr`-based
filters and the `map(attribute=...)` filter become immediate sandbox
bypasses, because there is no sandbox to bypass. The sandbox is not an
optional layer.

### Alternative 2: Jinja's `SandboxedEnvironment` with a curated denylist of filters and no globals allowlist

Use `SandboxedEnvironment`, keep most of Jinja's filters, remove only
the ones that are non-deterministic or obviously dangerous, and leave
the globals as Jinja ships them.

**Rejected because:** the same denylist argument applies to filters,
and the globals default includes `lipsum` (non-deterministic) and
`cycler`/`joiner` (stateful), both of which violate the determinism
contract. The difference between "the allowlist has fifteen filters" and
"the denylist has twenty filters removed from forty" is small in code
size but large in guarantees: the former can be reviewed in a single
sitting, and the latter cannot be reviewed at all because it depends on
which version of Jinja is installed.

**Additionally:** the "keep Jinja's filters" default means every new
filter Jinja adds becomes a capability grant by default. That is the
opposite of the allowlist principle this ADR is trying to establish.

### Alternative 3: A custom mini-template engine (no Jinja)

Write a small template engine that supports only `{{ variable }}`,
`{% for %}`, `{% if %}`, and the fifteen filters, with no third-party
dependencies.

**Rejected because:** a custom engine is a multi-month project to
implement, test, and secure at the level Jinja already provides. Jinja's
`SandboxedEnvironment` is actively maintained, audited by a security
community, and hardened against escape vectors that a from-scratch
engine would take years to enumerate. Reimplementing Jinja to avoid
Jinja's defaults is a case of "the cure is worse than the disease."

**Additionally:** Jinja's parser, error locations, `{% include %}`
semantics, and template inheritance are all non-trivial, and re-deriving
them is a poor use of engineering effort for a scaffolding tool.

### Alternative 4: A subprocess sandbox around each render

Run each template render in a subprocess with OS-level isolation
(seccomp, `unshare`, containers) so a code-execution escape is contained
at the OS boundary rather than in Python.

**Rejected because:** it is dramatically more complex to deploy and
test, and it does not address the determinism or strict-undefined
requirements. A subprocess sandbox is a defence for a *known-dangerous*
template; the goal here is to make templates not dangerous in the first
place. The two can be layered (subprocess isolation on top of the
in-process sandbox) if a genuine hostile-template scenario emerges, but
in-process isolation is the correct first line of defence and covers the
realistic threat model (a template from a cloned repository).

**Additionally:** a subprocess per render makes the renderer slow enough
that blueprint authors stop running it in tight loops, which is the
opposite of the ergonomic outcome we want.

### Alternative 5: Return `None` or raise on sandbox denials, not `Result`

Keep the renderer signature as `render(...) -> str` and raise a
`SandboxError` on any denial, letting callers use `try/except`.

**Rejected because:** exceptions force first-failure short-circuiting,
which prevents the batch-render pattern that scaffolding and validation
both rely on. A batch renderer that hits a sandbox denial on the third
of ten templates would stop, losing nine renders' worth of context.
`Result` keeps the caller in control of aggregation, matching the
pattern established by the loader (ADR-0010) and the validator
(ADR-0013).

**Additionally:** a `Result`-returning renderer makes the "never
silently fail" property a type-system guarantee, not a caller
discipline. Callers that ignore the `Err` branch are visible in code
review because `unwrap()` on `Err` raises loudly at runtime and mypy
flags the missing case at static-analysis time.

## References

- `docs/reference/TEMPLATE_FILTERS.md` — the human-readable filter
  contract.
- `docs/security/TEMPLATE_SANDBOX.md` — the threat model and the
  explicit non-goals of the sandbox.
- `src/forge/rendering/filters.py` — the executable filter allowlist.
- `src/forge/rendering/globals.py` — the (empty) globals allowlist.
- `src/forge/rendering/_sandbox.py` — the deny list and the
  `ForgeSandboxedEnvironment` subclass.
- `src/forge/rendering/renderer.py` — the renderer and error
  translation.
- `src/forge/rendering/errors.py` — `TemplateRenderError` and its
  invariants.
- ADR-0010 — `Result`-based error aggregation in the loader (the pattern
  this ADR follows).
- ADR-0013 — Exit codes as a public contract (how template errors map
  to `ExitCode.TEMPLATE_ERROR`).
- `tests/unit/rendering/test_filter_docs.py` — the doc-sync test that
  keeps `TEMPLATE_FILTERS.md` and `filters.py` in agreement.
- `tests/unit/rendering/test_sandbox_escape.py` — the adversarial
  templates that pin the deny list.
- `tests/integration/test_rendering_end_to_end.py` — the composition
  test that proves the modules work together.
- Jinja2 documentation, "Sandbox" — the base behaviour this ADR
  extends.
- Jinja2 documentation, "Undefined Types" — the rationale for
  `StrictUndefined`.
- CommonMark specification, "Fenced code blocks" — the fence-length rule
  the doc-sync extractor follows.
