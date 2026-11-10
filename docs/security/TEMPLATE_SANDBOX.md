# Template Sandbox — Threat Model and Guarantees

This page documents the security properties of Forge's template
rendering layer: what the sandbox protects against, how it does so,
what it explicitly does *not* protect against, and what an operator
should do if they suspect a bypass.

It is written for three audiences:

- **Blueprint authors** who want to know what a template can and cannot
  do, so they can write templates with confidence.
- **Security reviewers** who need to assess the sandbox's design before
  trusting Forge in a sensitive environment.
- **Forge contributors** who are considering a change to
  `src/forge/rendering/_sandbox.py` or `environment.py` and need to
  understand the invariants they might break.

The page is a *contract*, not a tutorial. It describes what is true
today and why. If a future change weakens one of the guarantees below,
that is a security review item, not a refactoring detail.

---

## The threat model in one paragraph

Forge renders templates that come from **blueprints**. A blueprint is a
directory of files — a YAML manifest and a set of Jinja templates —
that a user may have written themselves, cloned from a repository, or
received from a colleague. **The blueprint is untrusted input.** The
template author may be malicious, careless, or simply mistaken; the
sandbox's job is to ensure that rendering a template cannot:

- **Execute arbitrary code** on the host (shell commands, Python
  imports, `os.system`, `subprocess.run`, `eval`, `exec`).
- **Read or write files** outside the blueprint's own directory (or
  read files at all, beyond the template and its includes).
- **Access or mutate process state** — environment variables, loaded
  modules, class hierarchies, `sys.modules`, global config.
- **Escape into Python's object graph** via attribute chains like
  `__class__.__bases__[0].__subclasses__()`.
- **Exfiltrate secrets** present in the process's memory or on disk.

The sandbox does not attempt to prevent a *buggy* template from
producing *wrong output* — that is `forge validate`'s job. It
prevents a *hostile* template from doing something harmful while
producing output.

---

## Assets protected

| Asset | Why it matters | How it is protected |
|-------|----------------|---------------------|
| **Host process** | A successful RCE compromises the user's machine, and any credentials in their environment (cloud tokens, git keys, SSH agent) | `ForgeSandboxedEnvironment` denies attribute/call access to dangerous objects |
| **Filesystem** | Reading `/etc/passwd` or `~/.ssh/id_rsa` leaks credentials; writing anywhere corrupts the user's home | Loader is scoped to the template's directory; no file I/O filters exist |
| **Filesystem scope** | Even within the blueprint, a symlink could escape to `/etc` | Loader sets `followlinks=False`; symlink traversal is refused |
| **Process memory** | Env vars, loaded modules, in-memory secrets | No access via the sandbox; object-graph traversal denied |
| **Determinism** | A non-deterministic render produces output that fails CI or misleads review | No time/random filters or globals; sorted context |
| **Supply-chain integrity** | A malicious blueprint could exfiltrate the user's credentials via a rendered file that gets committed | All of the above; plus, rendered output is plain data, not executable content |

The last row is worth emphasising: a successful attack does not need to
crash the process. A template that quietly renders `{{ my_secret }}`
into a generated `config.yaml` that gets committed to a public
repository is an *exfiltration*, not an *exploit* — and the sandbox's
no-attribute-access policy is what prevents reaching `my_secret` in the
first place.

---

## Defense layers

The sandbox is built in layers. Each layer can be defeated in
isolation; the property that matters is that *all* of them must be
defeated simultaneously for an escape to succeed. This is the
defense-in-depth principle applied to template security.

| Layer | What it does | Where it lives |
|-------|--------------|----------------|
| **1. Jinja's base sandbox** | `SandboxedEnvironment` blocks calls and attribute accesses on objects Jinja considers dangerous (modules, classes, functions with suspicious attributes) | `jinja2.sandbox.SandboxedEnvironment` |
| **2. Name-based deny list** | A curated list of attribute names that are denied unconditionally, regardless of the object they appear on | `_DENIED_ATTRS` in `src/forge/rendering/_sandbox.py` |
| **3. `SecurityError` on denial** | Denials raise `SecurityError` so the renderer produces a structured `sandbox.denied` error rather than an `Undefined` fallback | `ForgeSandboxedEnvironment.is_safe_attribute` |
| **4. Filter allowlist** | Only the filters documented in `TEMPLATE_FILTERS.md` are installed; Jinja's default filters are cleared | `filters.install()` in `src/forge/rendering/filters.py` |
| **5. Globals allowlist** | Only the globals documented in `TEMPLATE_FILTERS.md` are installed; Jinja's default globals are cleared | `globals.install()` in `src/forge/rendering/globals.py` |
| **6. Loader confinement** | `{% include %}` and `{% extends %}` are restricted to the template's own directory; symlinks are not followed | `make_loader()` in `src/forge/rendering/environment.py` |
| **7. Strict undefined** | Any name not in the context raises `UndefinedError` instead of silently rendering as empty | `StrictUndefined` in `environment.py` |
| **8. No autoescape** | Autoescape is deliberately off; templates cannot use HTML encoding tricks to smuggle data through attribute access | `environment.get_environment()` |

The layers are ordered roughly by *specificity*, not by importance.
Layer 2 (name-based deny list) is what catches novel escape routes
that layer 1 might miss; layer 4 and 5 are what keep the attack
surface small so layers 1–3 have less to defend.

---

## What the sandbox blocks

Each of the following vectors is *tested* by
`tests/unit/rendering/test_sandbox_escape.py`. The list is not
exhaustive — it is the set of *published* escape chains the sandbox is
known to close.

### The `__class__.__globals__` import chain

The canonical Jinja escape, published in Jinja's own security
advisories and reproduced in every "template engine exploitation"
write-up:

```jinja
{{ ''.__class__.__init__.__globals__['__builtins__']['__import__']('os').system('id') }}
```

Every attribute in the chain — `__class__`, `__globals__`,
`__builtins__`, `__import__` — is on `_DENIED_ATTRS`. The sandbox
raises `SecurityError` at the first one, which the renderer reports as
`sandbox.denied`.

### The `__subclasses__` enumeration chain

The other canonical vector — walk from any object's class to `object`
via `__bases__`, then enumerate every loaded class and find one whose
methods reach a dangerous module:

```jinja
{{ ().__class__.__bases__[0].__subclasses__() }}
```

`__class__`, `__bases__`, and `__subclasses__` are all denied by name.
The chain is closed at the first attribute access.

### The `__mro__` shortcut

A shorter route to the same enumeration via the method resolution
order:

```jinja
{{ ().__class__.__mro__ }}
```

`__mro__` is on the deny list.

### Generator and coroutine frame introspection

Generators carry a `gi_frame` attribute pointing at their execution
frame, from which `f_globals` reaches the module's namespace.
Coroutines have `cr_frame`. Both are closed:

```jinja
{{ (x for x in [1]).__class__ }}
{{ gen.gi_frame.f_globals }}
{{ coro.cr_frame.f_locals }}
```

`gi_frame`, `gi_code`, `cr_frame`, `cr_code`, `f_globals`, and
`f_locals` are all on the deny list. These are less well-known than the
`__class__` chain, but equally effective — a template author who knows
them can reach arbitrary module state.

### Attribute access on context-provided classes

A template author can pass a class through the context (Forge's
resolver does not restrict types — see `test_subclasses_on_context_class_is_denied`):

```jinja
{{ cls.__subclasses__() }}
```

`__subclasses__` is denied by name, so the enumeration route is closed
regardless of how the class arrived.

### Attribute access on context-provided functions

A function passed through the context has `__globals__` pointing at its
defining module's namespace (see
`test_globals_on_context_function_is_denied`):

```jinja
{{ fn.__globals__ }}
```

`__globals__` is denied by name, so the module-namespace route is
closed regardless of which function was passed.

### Plain `__class__` access on any object

Even without a follow-up chain, `{{ obj.__class__ }}` is denied. This
matters because Jinja's base sandbox considers `__class__` on an
arbitrary object "safe" (it is a normal attribute of a normal object);
Forge's deny list closes it unconditionally, because
`__class__` is a known escape primitive regardless of the object it
appears on.

### Non-deterministic filters and globals

Stock-Jinja filters and globals that read the clock (`now`), draw from
random sources (`random`, `lipsum`), or hold state across calls
(`cycler`, `joiner`, `namespace`) are all removed by the filter and
global allowlists. See `TEMPLATE_FILTERS.md` for the full list of what
is excluded and why.

### Non-deterministic iteration

The context is sorted *recursively* before being passed to Jinja (see
`_sort_mapping` in `renderer.py`), so `{% for k in ctx %}` iterates
keys in a stable, sorted order regardless of how the caller built the
dict. Without this, the same template with the same logical variables
could produce different output on different runs.

### File I/O via `{% include %}`

The loader is scoped to the template's own directory with
`followlinks=False`:

```jinja
{% include "../secrets.json" %}  {# denied: escapes the blueprint directory #}
{% include "sibling.j2" %}       {# allowed: within the same directory #}
```

The denial surfaces as `include.not_found` (Jinja's `TemplateNotFound`),
which the renderer reports with a hint pointing at the blueprint
directory.

---

## What the sandbox does NOT block

A threat model that only describes what is protected is incomplete. The
following are **explicit non-goals** — the sandbox does not defend
against them, and operators relying on Forge in a sensitive environment
should address them at a different layer.

### Resource exhaustion

A template can consume unbounded CPU, memory, or disk *within the
sensitivity of the filters*. Examples:

```jinja
{% for i in range(10000000) %}{{ i }}{% endfor %}
{{ payload | tojson }}  {# where payload is a huge nested dict #}
```

The sandbox does not enforce a time or memory budget per render.
Operators should apply OS-level resource limits (cgroups, `ulimit`,
container quotas) or run Forge in a dedicated sandbox (a subprocess
with a timeout, a container with a memory cap).

A future ADR may add per-render budgets; they are not part of the
current contract, and any blueprint that relies on resource limits
being enforced should document that requirement in its own manifest.

### Rendering wrong output

The sandbox prevents a template from doing something dangerous. It
does not prevent it from doing something *wrong*. A template that
renders `class {{ name }}` when `name` is undefined is a *rendering
correctness* problem, caught by `StrictUndefined` (as an
`undefined.variable` error), not by the sandbox.

A template that renders syntactically valid but semantically wrong
Python (e.g. `def name()` when `name` is a keyword) is not caught at
render time; it will fail when the generated code is imported, which is
downstream of Forge. This is why `forge create` runs the blueprint's
verification contract after scaffolding — but verification is a
blueprint-author responsibility, not a sandbox guarantee.

### Side channels

The sandbox does not prevent a template from *observing* information
through timing or output:

- The time taken to render `{% for x in big_list %}` reveals something
  about `big_list`'s length.
- The output of `{{ payload | length }}` reveals the payload's length.
- The output of `{{ items | join }}` reveals `items`' contents.

These are legitimate template operations. The sandbox's threat model
does not include "a template author should not be able to observe the
size of the data they were given"; it includes "a template author
should not be able to *escape into Python*." The distinction matters
for threat model clarity: sandboxes that try to prevent observation
tend to be overly restrictive and produce confusing failures.

If you have a threat model where even the *size* of the data must not
leak, do not pass that data to a template — Forge's templates operate
on whatever context the caller provides, and context inspection is
part of the contract.

### Attack on the CLI itself

The sandbox protects the *template rendering*, not the *CLI process*.
Attacks on the CLI (malicious YAML in the blueprint manifest, malicious
arguments) are handled at a different layer:

- YAML is parsed with `yaml.safe_load`, not `yaml.load`.
- Path arguments are validated by the loader.
- The CLI does not execute arbitrary code from the blueprint.

A security review of the whole Forge CLI should consider both layers;
this document is specifically about the template sandbox.

### Non-Jinja template languages

Forge renders Jinja templates. If a blueprint uses a *different*
template language (some blueprints may support multiple frontends),
that language's security is that language's responsibility. The
sandbox described here does not apply.

### Physical access and privilege escalation

If an attacker has shell access to the user's machine, the sandbox is
irrelevant — they can read whatever they want with the OS. The sandbox
assumes the template *file* is untrusted, but the *user's environment*
is trusted. If your threat model includes a compromised host, address
that at the OS layer.

---

## Known escape routes and their status

This table documents every *publicly known* Jinja escape route and
Forge's response. It is the reference for security reviewers assessing
the sandbox.

| Escape route | Status | Mitigation |
|--------------|--------|------------|
| `__class__` chain to `__globals__` | **Blocked** | Name-based deny list (`__class__`, `__globals__`) |
| `__subclasses__` enumeration | **Blocked** | Name-based deny list (`__subclasses__`) |
| `__mro__` traversal | **Blocked** | Name-based deny list (`__mro__`) |
| `gi_frame.f_globals` (generators) | **Blocked** | Name-based deny list (`gi_frame`, `f_globals`) |
| `cr_frame.f_locals` (coroutines) | **Blocked** | Name-based deny list (`cr_frame`, `f_locals`) |
| `__builtins__['__import__']` | **Blocked** | Name-based deny list (`__builtins__`, `__import__`) |
| `lipsum` resource exhaustion | **Partial** | Global removed; operators should also apply OS limits |
| `attr` filter | **Blocked** | Filter removed from allowlist |
| `map(attribute=...)` filter | **Blocked** | Filter removed from allowlist |
| `urlize` (URL parsing) | **Blocked** | Filter removed from allowlist |
| `{{ obj.__dict__ }}` | **Blocked** | Name-based deny list (`__dict__`) |
| `{{ getattr(obj, 'x') }}` (if `getattr` were a global) | **Blocked** | Globals allowlist is empty; `getattr` is not available |
| Jinja extension `{% debug %}` | **Blocked** | Extensions disabled |
| Autoescape bypass | **N/A** | Autoescape is off by design (Forge renders code, not HTML) |
| Custom filter with side effects | **Prevented** | Filter allowlist is fixed; no user-supplied filters |
| Custom template class | **Prevented** | Not configurable; environment is fixed |
| Loader override | **Prevented** | Loader is scoped per-render; not configurable |
| Environment subclass with a relaxed check | **Prevented** | `ForgeSandboxedEnvironment.overlay` asserts the class |

"Blocked" means the sandbox raises `SecurityError` before the template
executes. "Prevented" means the configuration makes the vector
impossible, not just denied. "Partial" means the route is closed but
the general risk (resource exhaustion) is not addressed by the
sandbox — it requires a different mitigation.

### What is *not* on this list, and why

The table does not include hypothetical future Jinja bugs, CVEs not yet
published, or escape vectors that require compromising the process
outside the template. Those are handled by:

- **Dependency pinning**: `pyproject.toml` pins Jinja to a version
  range that receives security updates.
- **Security advisories**: subscriptions to Jinja's release notes for
  any new CVEs.
- **Defense in depth**: layers 1–8 above mean a single Jinja bug does
  not suffice; the name-based deny list and filter allowlist also need
  to fail.

A successful escape requires defeating *all* these layers, which is a
meaningfully harder bar than defeating any single one.

---

## Operational guidance

### If you suspect the sandbox has been bypassed

1. **Do not report it in a public issue.** See `SECURITY.md` for the
   private disclosure process.
2. **Preserve the blueprint** (templates and manifest) and any
   environment details (Python version, Jinja version, Forge version).
3. **Document what you observed**: the template, the variables, the
   expected vs. actual output, and whether the escape succeeded or was
   blocked with an unexpected code.

The maintainers will reproduce and assess.

### If you are deploying Forge in a sensitive environment

The sandbox is *one* layer of defence. Consider:

- **Container isolation**: run Forge in a container with a
  read-only filesystem and no network access, unless the command needs
  either.
- **Resource limits**: use cgroups, `ulimit`, or container quotas to
  bound CPU and memory per invocation. The sandbox does not enforce
  limits.
- **Least privilege**: run Forge as a dedicated user with only the
  permissions it needs. Do not run as root.
- **Network isolation**: rendering does not require network access;
  block it unless explicitly needed (some future filters may fetch
  URLs; today they do not).
- **Audit logging**: log every blueprint rendered, its source, and the
  variables passed, for post-incident analysis.
- **Version pinning**: pin Forge, Jinja, and their dependencies to
  specific versions, and update on a schedule.

These measures do not replace the sandbox — they complement it.
Defense in depth means an escape from the template layer does not
immediately compromise the host.

### If you are contributing to the sandbox

See `docs/adr/0014-sandboxed-deterministic-templates.md` for the design
rationale, and `src/forge/rendering/_sandbox.py` for the layer-by-layer
implementation. Any change to `_DENIED_ATTRS`, `ForgeSandboxedEnvironment`,
or the environment configuration requires:

1. A new adversarial template in `tests/unit/rendering/test_sandbox_escape.py`
   demonstrating the vector the change addresses (if adding) or proving
   the vector is still closed (if refactoring).
2. An ADR amendment explaining the change and its effect on the threat
   model.
3. A security review note in the PR explaining why the change does not
   weaken any existing layer.

The sandbox is security-critical code. Changes to it are reviewed with
the same rigour as changes to authentication or cryptographic code —
because a sandbox bypass is a security incident, not a bug.

---

## See also

- `src/forge/rendering/_sandbox.py` — the deny list and
  `ForgeSandboxedEnvironment`.
- `src/forge/rendering/environment.py` — the environment configuration.
- `src/forge/rendering/filters.py`, `globals.py` — the filter and global
  allowlists.
- `tests/unit/rendering/test_sandbox_escape.py` — the adversarial test
  suite.
- `docs/adr/0014-sandboxed-deterministic-templates.md` — the design
  decisions.
- `docs/reference/TEMPLATE_FILTERS.md` — the allowlist reference for
  blueprint authors.
- `SECURITY.md` — the vulnerability disclosure process.
