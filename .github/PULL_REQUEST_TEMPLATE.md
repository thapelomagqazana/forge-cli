<!--
  Forge CLI — Pull Request Template
  =================================

  Thanks for contributing. Please fill in every section below.
  If a section is genuinely not applicable, keep the heading and write "N/A"
  plus a one-line reason. Do not delete headings — reviewers scan for them.

  Before requesting review:
    1. Run `./scripts/check_quality.sh` locally. All gates must pass.
    2. Rebase on `main`. The branch must be up to date.
    3. Keep the PR focused on one logical change.
-->

## Summary

<!--
  WHAT does this PR do and WHY does it matter?
  Lead with the change, then the rationale. Two to five sentences is ideal.
  Use bullets when the change spans multiple concerns.
-->

_Describe the change and its rationale here._

**What it does:**

- _Concise bullet describing change #1_
- _Concise bullet describing change #2_
- _Concise bullet describing change #3_

**Why it matters:**

_One paragraph on the impact. What breaks if this is not merged? What becomes
possible once it is? Who benefits?_

---

## Related Issues

<!--
  Use GitHub's closing keywords so issues auto-close on merge.
    Closes #123      → auto-closes the issue
    Fixes #456       → auto-closes
    Part of #789     → does not close
    Depends on #101  → does not close
    Unblocks #102    → does not close
    Refs #103        → does not close
-->

- Closes #
- Part of Epic #
- Depends on #
- Unblocks #
- Refs #

---

## Type of Change

<!--
  Check every box that applies. A PR can span multiple types.
  If "Breaking change" is checked, the "Migration Notes" section is mandatory.
-->

- [ ] Bug fix
- [ ] New feature
- [ ] Breaking change
- [ ] Documentation update
- [ ] Refactor
- [ ] Performance improvement
- [ ] Test improvement
- [ ] CI / build / tooling change
- [ ] Dependency update

> **Note:** _If this PR is not a breaking change, say so here in one line so
> reviewers do not have to guess._

---

## Checklist

<!--
  Every box must be checked before review. If a box cannot be checked,
  explain why in the "Reviewer Notes" section instead of leaving it blank.
-->

- [ ] **I have added tests that prove my fix or feature works.**
  - _List the test files added or modified._
  - _Describe the scenario(s) covered._

- [ ] **All tests pass locally (`pytest`).**
  - Verified via `uv run pytest`.
  - Verified via `./scripts/check_quality.sh`.

- [ ] **Linting passes (`ruff check .`).**
  - `ruff format --check` also passes.
  - Rule sets enforced: `E`, `W`, `F`, `I`, `N`, `UP`, `B`, `C4`, `SIM`, `RUF`, `S`, `T20`, `PT`.

- [ ] **Type checking passes (`mypy src`).**
  - Strict mode: `disallow_untyped_defs`, `disallow_any_generics`,
    `disallow_subclassing_any`, `no_implicit_optional`, `no_implicit_reexport`,
    `warn_unreachable`.

- [ ] **Coverage is ≥ 90%.**
  - `--cov-fail-under=90` enforced in `pyproject.toml`.
  - `branch = true` — untested `else` branches fail the gate.
  - `coverage.xml` generated and uploaded as a CI artifact.

- [ ] **Documentation is updated (if user-facing).**
  - _List the docs changed: `README.md`, `CONTRIBUTING.md`, `CHANGELOG.md`,
    `docs/*`._
  - _If none: state why, for example "internal refactor, no user-facing change"._

- [ ] **This change is backward-compatible (or a migration is documented).**
  - _If breaking: link to the migration note in `CHANGELOG.md`._
  - _If backward-compatible: state what was checked (CLI behaviour, public API,
    schemas, dependency versions)._

---

## Reviewer Notes

<!--
  Direct the review. What should reviewers focus on? What trade-offs were
  made? What design decisions deserve scrutiny? Be specific — "please review"
  wastes reviewers' time.
-->

**What to focus on during review:**

1. _Specific file or decision #1 — why it matters._
2. _Specific file or decision #2 — the trade-off made._
3. _Specific file or decision #3 — the assumption made._

---

## Test Plan

<!--
  Enumerate every test level affected by this PR. Use "N/A" for levels that
  are genuinely not applicable — never leave a row blank.
-->

---

## Screenshots / Evidence

<!--
  Paste terminal output, CI status badges, coverage snippets, or demo GIFs.
  For CLI changes, show before and after.
  For UI changes, include a screenshot or short GIF.
  For CI changes, include a screenshot of the workflow run.
-->

---

## Deployment Notes

<!--
  What happens when this PR merges? If the answer is "nothing", say so
  explicitly. Reviewers should never have to guess.
-->

- **User-facing changes:** _Yes or No, then describe._
- **Release required:** _Yes or No. If yes, mention version or tag._
- **PyPI:** _Will this trigger a publish? If yes, describe the workflow._
- **Docs site:** _Will this deploy docs? If yes, describe the workflow._
- **Lockfile:** _Did `uv.lock` change? If yes, note that contributors must run
  `uv sync --all-extras`._
- **Post-merge:** _Follow-up actions required, for example enable rule, rotate
  secret, notify team._

---

## What I Learned (Feynman Summary)

<!--
  Explain the key insight of this PR to a 12-year-old. If you cannot explain
  it simply, the change may be too complex. This section doubles as a
  teaching note for future contributors.
-->

> _One or two sentences in plain English. Use an analogy if helpful._
>
> _Example: "`uv.lock` is the recipe photo — everyone bakes the same cake."_

---

## Migration Notes

<!--
  MANDATORY if "Breaking change" is checked. Delete this section entirely if
  the PR is backward-compatible.
-->

**What changed:**

- _Describe the breaking change._

**Who is affected:**

- _Users of `<command>`, `<API>`, `<schema>`, or `<config>`._

**Migration steps:**

```bash
# Step-by-step migration commands
```

**Rollback steps (if applicable):**

```bash
# Step-by-step rollback commands
```

---

<!--
  Thank you for contributing to Forge CLI.
  A maintainer will review this PR within two business days.
  If you have not heard back in that window, feel free to ping the thread.
-->
