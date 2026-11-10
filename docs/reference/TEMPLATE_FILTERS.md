# Template Filters Reference

Templates in Forge blueprints may use **only** the filters listed on this
page. The list is an *allowlist*: any filter not documented here is
rejected at render time with a `syntax.error`, and the template author
sees a structured error naming the unknown filter.

This page is the **human-readable contract** for what a template can do.
It is kept in sync with the executable allowlist
(`src/forge/rendering/filters.py`) by a doc-sync test — see
[Keeping this page in sync](#keeping-this-page-in-sync) at the bottom.
If you are adding a filter to Forge, that section tells you what to
update; if you are writing a blueprint, this page tells you what is
available.

---

## Why an allowlist, not a denylist

Jinja ships with a large filter set. Forge denies almost all of it,
for three reasons worth understanding before reading the table:

1. **Determinism.** A rendering pipeline must produce the *same bytes*
   for the same template and the same variables. Filters that call the
   clock (`|now`), draw from a random source (`|random`), or depend on
   iteration order would break that guarantee. Forge's determinism
   contract lives in `docs/adr/0014-sandboxed-deterministic-templates.md`.

2. **Security.** Templates are untrusted input — a blueprint may come
   from a repository you cloned, a colleague's PR, or a marketplace.
   Filters that parse untrusted content (`|urlize`, `|xmlattr`) or that
   expose attribute access (`|attr`, `|map(attribute=...)`,
   `|selectattr`) widen the attack surface the sandbox has to defend.
   Forge's sandbox lives in `src/forge/rendering/_sandbox.py`.

3. **Predictable behaviour.** Every filter on this page is *total*: it
   produces a defined result for every plausible input, or raises a
   clear error. Filters that silently reinterpret input (e.g. Jinja's
   stock `|default(x, true)`, which substitutes for `0`, `False`, and
   empty containers as well as missing names) are excluded because
   their behaviour surprises blueprint authors.

An allowlist is stronger than a denylist for the same reason a
whitelist of allowed callers is stronger than a blacklist: the set is
finite and auditable, and it does not silently grow when Jinja adds a
filter in a future release.

---

## Reading the tables

Each row lists:

- **Filter** — the name as it appears in a template, in backticks. Use
  it as `{{ value | filter }}` or `{{ value | filter(arg1, arg2) }}`.
- **Behaviour** — what it does, in one line.
- **Example** — a one-line input/output demonstration.

Filters are grouped by category. Groups are semantic (casing,
whitespace, serialisation) rather than alphabetical so that related
filters are easy to scan.

---

## Case conversion

| Filter | Behaviour | Example |
|--------|-----------|---------|
| `upper` | Uppercase the string. | `{{ "abc" \| upper }}` → `ABC` |
| `lower` | Lowercase the string. | `{{ "ABC" \| lower }}` → `abc` |
| `title` | Title-case each word. | `{{ "hello world" \| title }}` → `Hello World` |
| `capitalize` | Capitalise the first character, lowercase the rest. | `{{ "hello" \| capitalize }}` → `Hello` |

The four casing filters operate on any value by first converting it to
`str`, so they are safe to apply to integers and other non-string
values. They are the foundation of the `snake`/`kebab`/`pascal`
identifier trio below.

---

## Whitespace and simple string operations

| Filter | Behaviour | Example |
|--------|-----------|---------|
| `trim` | Strip leading and trailing whitespace (or specific characters, if given). | `{{ "  hi  " \| trim }}` → `hi` |
| `replace` | Replace occurrences of a substring. | `{{ "a-b-c" \| replace("-", "_") }}` → `a_b_c` |

`trim` strips only *whitespace* by default (spaces, tabs, newlines);
pass a string argument to strip specific characters instead:
`{{ "xxhiixx" | trim("x") }}` → `hii`. This matches Python's
`str.strip`.

`replace` takes three arguments: the value, the old substring, and the
new substring. An optional fourth argument limits the number of
replacements (matching Python's `str.replace`). Omitting the new
substring is a *template error* — Forge reports it as
`filter.invalid_arguments` with a hint to check the filter's signature.

---

## Identifier casing

| Filter | Behaviour | Example |
|--------|-----------|---------|
| `snake` | `CamelCase` → `snake_case`. | `{{ "MyThing" \| snake }}` → `my_thing` |
| `kebab` | `snake_case` → `kebab-case`. | `{{ "my_thing" \| kebab }}` → `my-thing` |
| `pascal` | `snake_case` → `PascalCase`. | `{{ "my_thing" \| pascal }}` → `MyThing` |

The three identifier filters share a single normalisation routine, so
they never disagree about word boundaries. `HTTPServer` becomes
`http_server` (not `h_t_t_p_server`); `myVariable` becomes
`my_variable`; `my_thing` round-trips through `kebab` and `pascal` back
to `my_thing` if passed through `snake`.

Use `snake` for Python module and variable names, `kebab` for npm
package names and CLI command slugs, and `pascal` for class names in
Python and TypeScript.

---

## Defaulting and indentation

| Filter | Behaviour | Example |
|--------|-----------|---------|
| `default` | Substitute a fallback for *missing*, `None`, or `""`. | `{{ missing \| default("fallback") }}` → `fallback` |
| `indent` | Indent every line after the first by *width* spaces. | `{{ "a\nb" \| indent(2) }}` → `a\n  b` |

### `default`: the strict variant

Forge's `default` substitutes a fallback **only** when the value is:

- **Undefined** — the name is not in the render context.
- **`None`** — the caller explicitly passed `None`.
- **`""`** — the empty string.

It deliberately **does not** substitute for `0`, `False`, or empty
containers. Those are legitimate values: a blueprint variable set to
`0` should render as `"0"`, not as the fallback. Jinja's stock
`default(x, true)` mode substitutes for every falsy value, which is a
silent footgun for exactly these cases.

Because of the strict behaviour, `default` is *safe to leave on every
optional variable*: if the blueprint author sets the value to `0`, they
get `0`; if they set it to `""`, they get the fallback; if they omit it
entirely, they get the fallback. No hidden boolean-mode surprises.

### `indent`: first-line control

`indent` takes two arguments beyond the value: the width (default `4`)
and a boolean `first` flag (default `False`).

With `first=False` (the default), only lines after the first are
indented, which is what you want when embedding a block into an
already-indented context. The template:

```jinja
    {{ some_block | indent(4) }}
```

With `first=True`, the first line is indented too, which is what you
want when the block should be indented uniformly. The template:

```jinja
{{ some_block | indent(4, true) }}
```

Line endings inside the block are preserved; only whitespace at the
start of each line is added.

---

## Serialisation and structural operations

| Filter | Behaviour | Example |
|--------|-----------|---------|
| `tojson` | Serialise the value as deterministic JSON. | `{{ {"b": 2, "a": 1} \| tojson }}` → `{"a":1,"b":2}` |
| `length` | Number of items in a sequence or mapping. | `{{ "hello" \| length }}` → `5` |
| `join` | Concatenate sequence items with a separator. | `{{ ["a", "b"] \| join("-") }}` → `a-b` |
| `int` | Convert a value to an integer. | `{{ "42" \| int }}` → `42` |
| `string` | Convert a value to its string form. | `{{ 42 \| string }}` → `42` |

### `tojson`: deterministic by design

Forge's `tojson` differs from Jinja's stock `tojson` in one crucial
way: **keys are sorted**. The same Python dict produces byte-identical
JSON regardless of how the caller inserted the keys, which is what
makes generated JSON files reproducible.

Consequences worth knowing:

- Nested dicts are sorted at every level (the sort is recursive).
- Whitespace is minimal — no spaces after `:` or `,`. A golden snapshot
  of a generated `package.json` or `pyproject.toml` matches across
  platforms.
- Non-ASCII characters are preserved (`ensure_ascii=False`); use
  explicit `\uXXXX` escapes in the source data if ASCII-only output is
  required.
- Numeric formatting follows Python's `json.dumps`, matching
  JavaScript for integers and the RFC 8259 double-precision form for
  floats.

### `length`, `join`, `int`, `string`

These four filters exist because the stock Jinja versions surprise
blueprint authors:

- `length` calls `len()` directly. Jinja's stock `length` falls back to
  `__iter__`, which for some custom objects returns a consumed
  generator's length. Forge's version is stricter: a `TypeError`
  propagates as `filter.invalid_arguments` with a clear message.
- `join` converts every item to `str` before joining. Jinja's stock
  `join` relies on its internal `soft_str`, which behaves differently
  from `str()` for a few edge-case types. Forge's version is
  predictable: `{{ [1, 2] | join(",") }}` is always `"1,2"`.
- `int` matches Python's `int()`; a non-numeric string raises
  `filter.invalid_arguments` rather than silently returning `0`.
- `string` matches Python's `str()`; use it when you need to force a
  value's textual form before applying another filter.

---

## Filters that are deliberately *not* available

For completeness, this section lists the Jinja default filters Forge
does not expose, and why. If you are porting a template from a stock
Jinja environment, this is the list to consult when a filter
"mysteriously" fails.

### Non-deterministic filters (would break byte-identity)

- `random` — different output on every render.
- `now` (as a global function) — different output every second.
- `lipsum` — random text generation.
- `cycler`, `joiner` — stateful across calls; the state is
  non-deterministic when the template is reused.
- `namespace` — creates a mutable object whose attribute probing
  expands the sandbox's threat model.

### Content-parsing filters (attack surface)

- `urlize` — parses arbitrary strings as URLs, with I/O risk.
- `xmlattr` — parses strings as XML attributes.
- `format` — opens `str.format` attribute-access paths that the
  sandbox closes elsewhere.

### Attribute-exposing filters (contradict the sandbox's policy)

- `attr` — explicit attribute access by name.
- `map(attribute=...)` — implicit attribute access via a mapping.
- `selectattr`, `rejectattr` — filter sequences by attribute value.

The sandbox denies attribute access on objects by name (see
`_DENIED_ATTRS` in `src/forge/rendering/_sandbox.py`); these filters
would defeat that policy by giving templates another way to reach
attributes.

### Cosmetic filters (no scaffolding value)

- `wordwrap`, `truncate`, `center`, `format`, `filesizeformat` — the
  visual-editing family. Generated code does not benefit from word
  wrapping or centering; a blueprint template that needs to truncate a
  value can use `string` + slicing, or declare the length rule in its
  variable validation.

### Framework-bound filters

- `url_for`, `tojson` with a `script` argument, `escape` — bound to
  Flask's request context, which Forge does not have.

If a filter you need is missing from this page, the correct response is
to open an ADR proposing its addition — not to write a template that
expects it. Adding a filter is a security and determinism decision, and
the ADR process exists to make that decision explicit and reviewable.

---

## Error behaviour

When a template uses a filter that is not on this page, the renderer
returns a structured error:

- **Code:** `syntax.error` — Jinja's parser rejects the unknown filter
  before rendering begins, because filter names are resolved at
  *compile* time (not render time).
- **Message:** names the unknown filter (e.g. `No filter named
  'nonexistent_filter'`).
- **Hint:** points at this page.
- **Line/column:** populated with the location of the filter
  reference, so you can jump straight to the offending line.

When a template uses a known filter with invalid arguments, the
renderer returns a different error:

- **Code:** `filter.invalid_arguments` — the filter exists but the
  call is malformed.
- **Message:** names the underlying exception (e.g. `_replace()
  missing 1 required positional argument: 'new'`).
- **Hint:** points at this page and suggests checking the filter's
  signature.
- **Line/column:** not populated, because the failure happens at
  render time (after the lexer has discarded line positions).

The distinction matters for diagnosis: `syntax.error` means "you used
the wrong filter name; check the table above," while
`filter.invalid_arguments` means "you used the right filter, but with
the wrong arguments; check its signature."

---

## Keeping this page in sync

This page is checked against the executable allowlist by a doc-sync
test (`tests/unit/rendering/test_filter_docs.py`). The test fails in
three situations:

1. **A filter is in `ALLOWED_FILTERS` but not on this page.** The
   failure message names the missing filter. To fix: add a row for it
   under the appropriate category, following the table format used
   above.

2. **A filter is on this page but not in `ALLOWED_FILTERS`.** The
   failure message names the ghost. To fix: either remove the row (if
   the filter was intentionally removed from code) or restore the
   filter to `filters.py` (if the removal was accidental).

3. **The page's structure has drifted** — for instance, if a section
   was rewritten as prose rather than a table. The failure message
   says the extractor found no parsable rows. To fix: either restore
   the table format, or update the extractor in the test file to match
   the new format (and do both in the same commit — changing one
   without the other is a drift bug by definition).

### Table row format

Every filter is documented as a markdown table row whose first cell is
the filter name in backticks, followed by a pipe, the behaviour, a
pipe, and an example. The extractor scans **table rows that are not
inside fenced code blocks**; a row used as a demonstration inside a
` ``` ` fence is ignored by the extractor and does not count as a
documented filter. That distinction is what allows this page to
demonstrate the format without polluting the extracted set.

The pattern the extractor matches is, informally:

    | `filter_name` | Behaviour | Example |

with the first cell being a backtick-quoted lowercase identifier. Rows
inside fenced code blocks are stripped before matching, so a
demonstration like:

````
```markdown
| `example_name` | Example behaviour. | `{{ example }}` → `output` |
```
````

is safe: `example_name` will **not** be treated as a documented filter,
because the row is inside a fence.

The same rule applies to the "Filters that are deliberately *not*
available" section above: filter names mentioned in prose (like
`random`, `now`, `lipsum`) are not written as table rows, so the
extractor ignores them. That constraint — *prose mentions are
documentation; table rows are contract* — is what makes the extractor
reliable. Only deliberately structured documentation counts as a
documented filter, which means the doc can discuss forbidden filters
freely without confusing the sync test.

If you are demonstrating row format in prose (as this section does),
put the example inside a fenced code block. If you are documenting a
real filter, put it in an unfenced table row. The extractor makes the
distinction by structure, not by convention, so the two uses cannot
collide.

### Adding a new filter

To add a filter to Forge:

1. Implement it in `src/forge/rendering/filters.py`. The function
   must be pure, total, and deterministic — see that module's
   docstring for the contract.
2. Add it to `ALLOWED_FILTERS` in the same file.
3. Add a row for it under the appropriate category in this page.
4. Update the example in the parameterised table in
   `tests/unit/rendering/test_filter_allowlist.py`
   (`test_allowed_filter_renders_correctly`) with a template snippet
   and expected output. This proves the filter works end-to-end, not
   just that it is documented.
5. If the filter has non-trivial behaviour (interacts with
   `StrictUndefined`, has an unusual signature, uses non-obvious
   defaults), add a dedicated behaviour test in the same file, with a
   docstring explaining what could go wrong if it broke.

The doc-sync test will fail if you skip step 3, so it is not
forgettable in the same way that an "update the docs" reminder is. The
build fails with a specific message naming the filter you forgot to
document, and the fix is a two-line edit to this page.

---

## See also

- `src/forge/rendering/filters.py` — the executable allowlist.
- `src/forge/rendering/errors.py` — the error codes referenced above.
- `docs/adr/0014-sandboxed-deterministic-templates.md` — the design
  decisions behind the allowlist, sandbox, and determinism contract.
- `docs/reference/VALIDATION_ERRORS.md` — the error-message reference
  for template failures.
