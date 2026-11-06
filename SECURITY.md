# Security Policy

Forge CLI is a **developer infrastructure tool**. It writes to your filesystem,
executes verification commands, and will eventually manage shared engineering
standards across an organization. Security is not a feature — it is a
precondition for trust.

This document describes:

1. [Our security commitment](#1-security-commitment).
2. [How to report a vulnerability](#2-reporting-a-vulnerability).
3. [Supported versions](#3-supported-versions).
4. [The Forge CLI threat model](#4-threat-model).
5. [Security controls we implement](#5-security-controls).
6. [What Forge CLI does *not* protect against](#6-non-goals).
7. [Security design of generated projects](#7-generated-project-security).
8. [Dependency and supply-chain security](#8-supply-chain-security).
9. [Security advisories](#9-security-advisories).
10. [Security review process](#10-security-review-process).
11. [Acknowledgments](#11-acknowledgments).

---

## 1. Security Commitment

We take security seriously. Our commitments:

- **No silent data loss.** Forge CLI will never overwrite or delete your files
  without explicit consent.
- **No arbitrary code execution.** Downloading and rendering a blueprint will
  never execute code from that blueprint.
- **No hidden network calls.** Core generation and verification work offline;
  any network access is explicit and documented.
- **Transparency.** Every security-relevant behavior is documented, tested, and
  reproducible.
- **Timely response.** We acknowledge security reports within 48 hours and
  provide a remediation timeline within 5 business days.

If you find a vulnerability, please **do not open a public issue**. Follow the
[reporting process](#2-reporting-a-vulnerability) below.

---

## 2. Reporting a Vulnerability

### Private Disclosure

**Email:** [security@forge-cli.dev](mailto:security@forge-cli.dev)

Please include:

| Field | Description |
| :--- | :--- |
| **Summary** | A one-line description of the vulnerability. |
| **Affected version(s)** | The Forge CLI version(s) where you observed the issue. |
| **Severity** | Your assessment: critical / high / medium / low. |
| **Reproduction steps** | Exact commands, configuration, and environment. |
| **Impact** | What an attacker could achieve, and under what conditions. |
| **Suggested mitigation** | If you have one (optional, but appreciated). |
| **Your contact info** | For follow-up questions. |

**Do not** include:
- Real credentials, tokens, or private keys.
- Data from production systems.
- Personal information about third parties.

### What Happens Next

| Timeline | Action |
| :--- | :--- |
| **Within 48 hours** | We acknowledge receipt and assign a tracking ID. |
| **Within 5 business days** | We provide an initial assessment and a remediation timeline. |
| **Ongoing** | We keep you informed of progress and coordinate disclosure. |
| **At fix time** | We publish a security advisory and credit you (unless you prefer anonymity). |

### Coordinated Disclosure

We follow a **90-day coordinated disclosure** policy:

- We aim to release a fix within 90 days of confirmed vulnerability.
- If a fix requires more time, we will agree on an extended disclosure window
  with you.
- You are free to disclose the vulnerability publicly after 90 days, or after
  a fix is released, whichever comes first.

### Safe Harbor

We will not pursue legal action against researchers who:

- Follow this disclosure policy.
- Do not access, modify, or exfiltrate data beyond what is necessary to
  demonstrate the vulnerability.
- Do not disrupt our services or those of other users.
- Do not publicly disclose the vulnerability before the coordinated disclosure
  window ends.

---

## 3. Supported Versions

| Version | Status | Security Support |
| :--- | :--- | :--- |
| `0.1.x` | Alpha | ✅ Best effort (see [Status](../README.md#status)) |
| `1.0.x` and later | Stable | ✅ Full support |
| Older versions | End-of-life | ❌ No security fixes |

**Recommendation:** Always use the latest release. Subscribe to
[security advisories](#9-security-advisories) to be notified of updates.

---

## 4. Threat Model

Forge CLI operates in a trust boundary that includes:

1. **The user** (trusted).
2. **The Forge CLI binary** (trusted, but must be distributed securely).
3. **The blueprint** (untrusted by default, unless explicitly trusted).
4. **The project workspace** (trusted, but must be protected from accidental damage).
5. **The network** (untrusted; not required for core functionality).

### 4.1. Assets

| Asset | Description | Impact if Compromised |
| :--- | :--- | :--- |
| **User's project files** | Source code, configuration, data in the target directory. | Data loss, corrupted projects, source-code exfiltration. |
| **User's filesystem** | Files outside the target directory. | Data loss, privilege escalation. |
| **User's credentials** | Git tokens, SSH keys, cloud credentials in the environment. | Account compromise, supply-chain attack. |
| **Forge CLI integrity** | The installed binary and its bundled blueprints. | Malicious project generation, backdoored foundations. |
| **Generated project integrity** | The output of `forge create`. | Insecure defaults, backdoored dependencies. |
| **System integrity** | The host machine running Forge CLI. | Arbitrary code execution, full compromise. |

### 4.2. Adversaries

| Adversary | Capability | Motivation |
| :--- | :--- | :--- |
| **Malicious blueprint author** | Crafts a blueprint with malicious templates, variables, or verification commands. | Backdoor generated projects, exfiltrate data, execute code. |
| **Compromised blueprint registry** | Serves tampered blueprint packages. | Supply-chain attack on downstream projects. |
| **Malicious dependency** | A package in a blueprint's dependency tree is compromised. | Supply-chain attack on generated projects. |
| **Local attacker** | Has limited access to the user's machine (e.g., another user account). | Read sensitive files, escalate privileges. |
| **Network attacker** | Intercepts or modifies network traffic. | Tamper with blueprint downloads, inject commands. |
| **Malicious CI configuration** | Injects commands into the verification pipeline. | Execute arbitrary code in CI, exfiltrate secrets. |

### 4.3. Attack Vectors

| Attack | Description | Mitigation |
| :--- | :--- | :--- |
| **Path traversal** | A blueprint declares a destination path like `../../etc/passwd`. | Canonicalize all paths and reject any path outside the target root. |
| **Absolute path injection** | A blueprint declares an absolute path like `/etc/cron.d/backdoor`. | Reject absolute paths that are not within the target root. |
| **Symlink escape** | A blueprint creates a symlink pointing outside the target directory. | Detect and reject symlinks that escape the root. |
| **File overwrite** | A blueprint generates files that clobber existing user files. | Fail on collision unless `--overwrite` is explicitly passed. |
| **Arbitrary code execution** | A blueprint includes a `post_generate` hook that runs arbitrary code. | Disable hooks by default; require explicit consent to run them. |
| **Command injection** | A verification check's command is constructed from untrusted input. | Execute commands with `shell=False`; pass arguments as a list. |
| **Command hijacking** | A malicious blueprint declares a verification command like `rm -rf ~`. | Require explicit user consent before executing any command. |
| **Dependency confusion** | A generated project's dependency is replaced by a malicious package. | Generate dependency manifests with lockfiles; document the risk. |
| **Secret leakage** | A blueprint generates a project that hardcodes secrets. | Never generate real secrets; provide `.env.example` files only. |
| **Diagnostic leakage** | Error messages or logs expose secrets or credentials. | Redact known secret patterns before logging or outputting. |
| **Supply-chain tampering** | A blueprint package is modified in transit. | Verify integrity hashes; support signed blueprints (future). |
| **Registry compromise** | A registry serves a malicious blueprint version. | Pin exact versions and verify content hashes. |

---

## 5. Security Controls

Forge CLI implements the following controls. Each control is tested in the
`tests/security/` suite.

### 5.1. Filesystem Safety

| Control | Implementation | Test |
| :--- | :--- | :--- |
| **Path canonicalization** | `pathlib.Path.resolve()` + root containment check. | `test_path_traversal.py` |
| **Absolute path rejection** | Reject paths that are not relative to the destination root. | `test_path_traversal.py` |
| **Symlink detection** | Reject symlinks that escape the destination root. | `test_symlinks.py` |
| **Collision detection** | Fail if a destination file already exists (unless `--overwrite`). | `test_collision_detection.py` |
| **Staging-first generation** | Render all files into a temporary directory, validate, then commit. | `test_rollback.py` |
| **Transactional commit** | Move files atomically; roll back on failure. | `test_rollback.py` |
| **No silent deletion** | Never delete files outside the current transaction's staging area. | `test_rollback.py` |

### 5.2. Template Rendering Safety

| Control | Implementation | Test |
| :--- | :--- | :--- |
| **Sandboxed Jinja2** | `jinja2.sandbox.SandboxedEnvironment`. | `test_renderer.py` |
| **Strict undefined variables** | `jinja2.StrictUndefined`. | `test_renderer.py` |
| **Restricted filter allowlist** | Only safe filters (`upper`, `lower`, `default`, `replace`, `tojson`). | `test_renderer.py` |
| **No arbitrary Python access** | Templates cannot import modules or call `eval`. | `test_renderer.py` |
| **Deterministic rendering** | No `now()`, no random values, stable ordering. | `test_renderer.py` |

### 5.3. Command Execution Safety

| Control | Implementation | Test |
| :--- | :--- | :--- |
| **No shell interpolation** | `subprocess.run(..., shell=False)`. | `test_command_runner.py` |
| **Argument list, not string** | Commands are executed as a list of arguments, not a shell string. | `test_command_runner.py` |
| **Working directory restriction** | Commands must run with `cwd` inside the project root. | `test_command_runner.py` |
| **Environment allowlist** | Only `PATH`, `HOME`, `LANG`, `PYTHONPATH` are passed. | `test_command_runner.py` |
| **Timeout enforcement** | Commands are killed after a configurable timeout (default 60s). | `test_command_runner.py` |
| **Output truncation** | Output is truncated to 10KB to prevent memory exhaustion. | `test_command_runner.py` |
| **Explicit consent for commands** | Blueprint verification commands require user consent in interactive mode. | `test_verify.py` |

### 5.4. Output and Diagnostic Safety

| Control | Implementation | Test |
| :--- | :--- | :--- |
| **Secret redaction** | Known secret patterns (tokens, passwords, keys) are redacted from logs and output. | `test_redaction.py` |
| **No stack traces for expected errors** | Expected errors produce actionable messages, not raw tracebacks. | `test_cli_errors.py` |
| **Structured error output** | Errors are machine-readable (`{"code": "...", "message": "...", "hint": "..."}`). | `test_json_output.py` |
| **No secrets in manifests** | The `.forge/manifest.json` never contains credentials, tokens, or secret values. | `test_manifest.py` |

### 5.5. CI and Automation Safety

| Control | Implementation | Test |
| :--- | :--- | :--- |
| **Non-interactive mode is explicit** | `--no-interactive` must be passed; missing required inputs fail with a clear error. | `test_ci_integration.py` |
| **Stable exit codes** | Exit codes are documented and stable across versions. | `test_exit_codes.py` |
| **No interactive prompts in JSON mode** | `--json` disables all prompts. | `test_json_output.py` |
| **Timeout for interactive prompts** | Interactive prompts time out after 5 minutes. | `test_prompts.py` |

### 5.6. Supply-Chain Security (Forge CLI Itself)

| Control | Implementation | Test |
| :--- | :--- | :--- |
| **Trusted publishing** | PyPI releases use GitHub Actions OIDC Trusted Publishing (no long-lived API tokens). | CI workflow |
| **Lockfile integrity** | `uv.lock` is committed and verified in CI. | CI workflow |
| **Dependency auditing** | `pip-audit` runs in CI and blocks releases with known vulnerabilities. | CI workflow |
| **Static analysis** | `bandit` runs in CI and blocks high-severity findings. | CI workflow |
| **Reproducible builds** | Release builds are reproducible from the tagged commit. | Release process |
| **Artifact verification** | Release artifacts are published with SHA-256 checksums. | Release process |

---

## 6. Non-Goals

Forge CLI is **not** a security boundary for your code. It does not protect
against:

| Threat | Why Forge CLI Cannot Prevent It |
| :--- | :--- |
| **Malicious dependencies** | Forge generates a dependency manifest; it does not audit every transitive dependency. Use `pip-audit`, `npm audit`, or Dependabot. |
| **Compromised registries** | If a blueprint registry is compromised, Forge can only verify the content it receives. Use pinned versions and content hashes. |
| **Vulnerabilities in generated code** | Forge generates a *foundation*, not a security audit. Your application's code is your responsibility. |
| **Runtime vulnerabilities** | Forge does not scan for CVEs in your running application. Use a runtime security tool. |
| **Secrets in your environment** | Forge avoids capturing secrets, but it cannot prevent you from storing them insecurely. |
| **Malicious Git hooks** | Forge does not install or run Git hooks. |
| **Compromised CI runners** | Forge's CI integration runs whatever commands your CI configuration specifies. The CI runner's security is your responsibility. |
| **Physical access** | If an attacker has physical access to your machine, no local tool can protect you. |

**The core principle:** Forge CLI gives you a **safe, well-structured, and
reproducible foundation**. It does not replace your application's security
practices, dependency management, or runtime monitoring.

---

## 7. Generated Project Security

Forge CLI's official blueprints are designed with security in mind.

### 7.1. Python API Blueprint (`python.fastapi.api`)

| Control | Implementation |
| :--- | :--- |
| **No hardcoded secrets** | `.env.example` is provided; no real secrets are generated. |
| **Structured logging** | Uses JSON logging; no secrets are logged. |
| **Dependency pinning** | `uv.lock` is included for reproducible installs. |
| **Docker non-root user** | The `Dockerfile` runs as a non-root user. |
| **Security headers** | CORS middleware is configured safely. |
| **No debug mode in production** | `DEBUG=False` is the default in `.env.example`. |
| **Health check separation** | The health endpoint does not expose sensitive system information. |

### 7.2. TypeScript Frontend Blueprint (`typescript.react.app`)

| Control | Implementation |
| :--- | :--- |
| **No hardcoded secrets** | `.env.example` is provided; only `VITE_API_URL` is required. |
| **Content Security Policy** | A strict CSP is documented in the README. |
| **No `eval`** | ESLint rule `no-eval` is enabled. |
| **Dependency pinning** | `package-lock.json` is included for reproducible installs. |
| **Accessibility baseline** | `eslint-plugin-jsx-a11y` is enabled. |
| **No inline scripts** | The `index.html` contains no inline JavaScript. |

### 7.3. Python Test Automation Blueprint (`python.pytest.automation`)

| Control | Implementation |
| :--- | :--- |
| **No hardcoded credentials** | Test data uses Faker; `.env.example` provides configuration. |
| **Isolated test data** | Test fixtures are isolated per test; no shared state. |
| **No secrets in logs** | The test reporter redacts known secret patterns. |
| **Dependency pinning** | `uv.lock` is included. |
| **Browser sandbox** | Playwright runs browsers in headless mode by default. |

### 7.4. Verification of Generated Projects

Forge CLI's verification engine (`forge verify`) runs the blueprint's declared
checks against the generated project. This includes:

- **Required files:** Ensures the generated project contains the files the
  blueprint promised.
- **Dependency manifests:** Ensures `pyproject.toml` / `package.json` and
  lockfiles are present and valid.
- **Lint and type-check:** Ensures the code meets the blueprint's quality
  standards.
- **Tests:** Ensures the generated project's test suite passes.
- **Build:** Ensures the project builds successfully.
- **Docker:** Ensures the Docker image builds (if Docker is available).

If a verification check fails, the project is **not** reported as "verified."
The `forge verify` command exits with code `4` and provides an actionable
diagnostic.

---

## 8. Supply-Chain Security

### 8.1. Forge CLI Itself

| Control | Description |
| :--- | :--- |
| **PyPI Trusted Publishing** | Releases are published via GitHub Actions OIDC. No long-lived PyPI API tokens are used. |
| **Lockfile** | `uv.lock` is committed and verified in CI. |
| **Dependency auditing** | `pip-audit` runs in CI and blocks releases with known vulnerabilities. |
| **Static analysis** | `bandit` runs in CI and blocks high-severity findings. |
| **Reproducible builds** | `uv build` produces byte-reproducible artifacts from the tagged commit. |
| **Signed releases** | Release artifacts include SHA-256 checksums in the GitHub release. |
| **Provenance** | GitHub Actions publishes build provenance via OIDC (Sigstore-compatible). |

### 8.2. Official Blueprints

| Control | Description |
| :--- | :--- |
| **Bundled, not downloaded** | Official blueprints ship with the Forge CLI package. They are not downloaded at runtime. |
| **Versioned** | Each blueprint has a semantic version and a changelog. |
| **Verified in CI** | Each blueprint is generated and verified in CI before release. |
| **Reviewed** | Blueprint changes require review by a maintainer. |
| **No hooks** | Official blueprints do not execute hooks. |

### 8.3. Third-Party Blueprints (Future)

When Forge CLI supports a public blueprint registry, the following controls will
apply:

| Control | Description |
| :--- | :--- |
| **Content hashing** | Blueprint packages are hashed (SHA-256) and verified on download. |
| **Version pinning** | Users can pin exact blueprint versions and content hashes. |
| **Signature verification** | Blueprint packages can be signed (Sigstore, minisign). |
| **Trust levels** | Blueprints are classified as `official`, `verified`, or `community`. |
| **Sandboxing** | Blueprints are validated and rendered in a restricted environment. |

These controls are **not yet implemented** in the alpha. See the
[roadmap](../docs/architecture/roadmap.md) for timelines.

---

## 9. Security Advisories

We publish security advisories for confirmed vulnerabilities in Forge CLI
itself, its official blueprints, and its dependencies (when relevant).

### How to Subscribe

- **GitHub Security Advisories:** Watch the repository's security advisories
  page: `https://github.com/your-org/forge-cli/security/advisories`.
- **RSS/Atom:** GitHub provides a feed for advisories.
- **Release notes:** Every release includes a security section if applicable.

### Advisory Format

Each advisory includes:

| Field | Description |
| :--- | :--- |
| **ID** | `GHSA-xxxx-xxxx-xxxx` (GitHub Security Advisory ID). |
| **Title** | A short description of the vulnerability. |
| **Severity** | CVSS score and severity (critical, high, medium, low). |
| **Affected versions** | Which Forge CLI versions are affected. |
| **Patched versions** | Which versions contain the fix. |
| **Description** | What the vulnerability is and how it can be exploited. |
| **Mitigation** | What users should do to protect themselves. |
| **Credit** | Who reported the vulnerability (with permission). |

### Example Advisory

> **GHSA-xxxx-xxxx-xxxx: Path traversal in `forge create` destination path**
>
> **Severity:** High (CVSS 7.5)
>
> **Affected versions:** `0.1.0a0` – `0.1.2`
>
> **Patched versions:** `0.1.3`
>
> **Description:** A malicious blueprint could declare a destination path
> containing `../` that escapes the target directory. An attacker who controls
> a blueprint could overwrite arbitrary files on the user's filesystem.
>
> **Mitigation:** Upgrade to `0.1.3` or later. If you cannot upgrade, avoid
> using untrusted blueprints.
>
> **Credit:** Reported by [Researcher Name] (@handle).

---

## 10. Security Review Process

Every release candidate goes through the following security review:

### 10.1. Pre-Release Security Checklist

- [ ] **Threat model reviewed.** Has the threat model changed? Are there new
      attack vectors?
- [ ] **Security tests pass.** All tests in `tests/security/` pass on Linux,
      macOS, and Windows.
- [ ] **Dependency audit passes.** `pip-audit` reports no critical or high
      vulnerabilities in Forge CLI's dependencies.
- [ ] **Static analysis passes.** `bandit` reports no high-severity findings.
- [ ] **Secret scan passes.** No secrets are committed to the repository.
- [ ] **Blueprint review complete.** All official blueprint changes are
      reviewed for security implications.
- [ ] **Redaction tests pass.** Known secret patterns are correctly redacted
      from logs, output, and reports.
- [ ] **Path safety tests pass.** Path traversal and symlink escape tests pass.
- [ ] **Command runner tests pass.** Timeout, injection, and output truncation
      tests pass.
- [ ] **No P0/P1 security issues open.** All critical and high-severity issues
      are resolved or have an approved mitigation.

### 10.2. Post-Release Monitoring

After a release:

- Monitor GitHub Security Advisories for new issues.
- Monitor `pip-audit` results for new CVEs in dependencies.
- Respond to new security reports within the documented SLA.
- Release a patch within 7 days of a confirmed critical/high vulnerability.

### 10.3. Security Reviewers

| Role | Responsibility |
| :--- | :--- |
| **Security Lead** | Owns the threat model, reviews security-sensitive changes, signs off on releases. |
| **Maintainer** | Reviews security-sensitive PRs, triages reports. |
| **Contributors** | Follow the security design; flag potential issues during review. |

---

## 11. Acknowledgments

We thank the following researchers for responsibly disclosing vulnerabilities
in Forge CLI. (This list will be populated as reports are received.)

| Researcher | Vulnerability | Advisory |
| :--- | :--- | :--- |
| *(awaiting first report)* | — | — |

If you report a vulnerability and wish to be credited, let us know in your
report. If you prefer to remain anonymous, we will respect that.

---

## 12. Contact

| Purpose | Contact |
| :--- | :--- |
| **Security vulnerabilities** | [security@forge-cli.dev](mailto:security@forge-cli.dev) |
| **General questions** | [GitHub Discussions](https://github.com/your-org/forge-cli/discussions) |
| **Bug reports** | [GitHub Issues](https://github.com/your-org/forge-cli/issues/new?template=bug_report.yml) |
| **Code of Conduct** | [conduct@forge-cli.dev](mailto:conduct@forge-cli.dev) |

---

<div align="center">

**Security is a shared responsibility.**

If you find something, tell us. If you fix something, tell us. If you're
unsure, ask. We would rather hear from you than not.

[Back to top](#security-policy)

</div>
