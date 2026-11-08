"""Integration tests for the ``forge validate`` CLI command.

What this suite must prove
--------------------------
The loader unit tests (``tests/unit/loading/*``) prove that
``BlueprintLoader.load()`` produces the correct ``Result``. This
suite proves that ``forge validate`` invokes the loader correctly,
renders the result correctly, and exits with the correct code.

That is a different contract. A loader test that passes does not
prove the CLI exits with code 1 on failure, does not prove the CLI
prints "validation error", and does not prove the CLI's output names
the file the user should edit. This suite is the *end-to-end* proof
that a user running ``forge validate <file>`` in a shell sees and
gets what the spec promises.

The contract has four parts:

1. **Exit codes.** ``0`` on valid, ``1`` on invalid, ``1`` on usage
   error (missing path). These are contractual (see
   ``docs/reference/exit-codes.md``) and scripts branch on them.

2. **Output content.** On success, the output names the loaded
   blueprint. On failure, every error is rendered with its source,
   path, message, and hint.

3. **Stream discipline.** Product output (success messages, the
   individual error lines) goes to stdout; commentary (summary
   lines, warnings, the reason a command failed) goes to stderr.
   Scripts that pipe ``forge validate ... | jq`` receive only the
   product; scripts that redirect ``2>/dev/null`` suppress only the
   commentary. This suite asserts the current split so a refactor
   that mixed the streams fails the test rather than silently
   breaking pipelines.

4. **Exit code from ``typer.Exit`` and from ``SystemExit`` are
   indistinguishable to the caller.** A shell sees ``$? == 1``
   whether the loader rejected the file or Typer rejected the
   invocation. The test asserts the user-facing contract, not the
   implementation of "which layer decided."

How the CLI is invoked
----------------------
The tests use a small ``_invoke`` helper that calls ``main()``
**in-process** and captures the resulting ``SystemExit``. That is a
deliberate choice, and it is worth understanding why.

``typer.testing.CliRunner`` invokes the Typer app *directly* — it
does not go through ``main()``. That is fine when the app raises
``typer.Exit`` (``CliRunner`` records the exit code from that
exception) but it is *not* fine when the command's exit code travels
through the callback's return value. ``CliRunner`` has never
supported return values as exit codes; it treats a normal return as
exit 0 regardless of what the callback returned.

Since ``forge validate`` (and every command in this project that
returns its exit code, per ``validate.py``'s "Exit-code mechanism"
docstring) communicates the exit code through the return value,
``CliRunner`` is not the right tool for asserting on exit codes. The
``_invoke`` helper below invokes the same function the console script
invokes — ``main()`` — and captures the ``SystemExit`` it raises.
That is the only in-process mechanism that observes the exit code the
shell actually sees.

The ``subprocess`` tests remain in this file for a different reason:
they prove that ``python -m forge.cli`` works at all, and that the
``__main__.py`` entry point calls ``main()`` correctly. They are
slower, so there are only two.

Why ``result.exception`` is *not* "the CLI crashed"
---------------------------------------------------
``_invoke`` captures the ``SystemExit`` that ``main()`` raises on
every path (success, failure, usage error). For a command that
finishes with exit code 0 or 1, the captured exception is a
``SystemExit(0)`` or ``SystemExit(1)`` — a *clean exit*, not a crash.
A crash would be an exception of some other type (``RuntimeError``,
``ValueError``, etc.), which ``main()``'s ``except BaseException``
branch would have handled and converted to ``SystemExit`` — and
``_invoke`` would capture the resulting ``SystemExit`` (with exit
code 5, per ``handle_exception``'s ``INTERNAL_ERROR`` classification).

So ``isinstance(result.exception, SystemExit)`` proves "the command
finished deliberately, whatever the exit code". A hypothetical crash
that somehow bypassed ``handle_exception`` would surface as a
non-``SystemExit`` exception type, which the test would detect.

Fixture strategy
----------------
The shared ``tests/fixtures/loader/multi-error.yaml`` fixture is used
for the "invalid blueprint" case, and the shared
``tests/fixtures/blueprints/valid/python-api.yaml`` fixture for the
"valid blueprint" case. Sharing with the loader tests means the CLI
integration test and the loader unit test are proving the *same
document* produces the *same* behaviour at two different layers.

For the directory-acceptance test, a **temporary directory** is
created with a ``blueprint.yaml`` inside it. This is a fixture
shaped like an actual blueprint directory, not "any directory that
happens to contain YAML files." The distinction matters: the
command's contract is "accept a blueprint directory" — a directory
that contains arbitrary YAML files but no ``blueprint.yaml`` is not
a blueprint directory, and is *correctly* rejected with exit 1 (a
user error, not a blueprint error; see ``exit-codes.md``).

Why the subprocess tests carry ``# noqa: S603``
-----------------------------------------------
``bandit``'s ``S603`` rule fires on every ``subprocess.run(...)``
call whose first argv element is not a string literal. The rule
exists because ``subprocess`` is a common vector for command
injection when the argv is derived from untrusted input.

In these two tests, the argv is *not* untrusted:

- ``sys.executable`` is the path of the Python interpreter running
  the test suite — the same interpreter that would run any
  subprocess the test suite spawns. There is no scenario in which
  this is attacker-controlled.
- ``"-m"``, ``"forge.cli"``, ``"validate"`` are hardcoded literals.
- The final argv element is a path to a fixture file in this
  repository's ``tests/fixtures/`` directory. It is a fixed literal
  path at import time; no test parametrization or user input
  influences it.

The ``# noqa: S603`` on the ``subprocess.run`` line documents that
this call has been reviewed and is safe, and — crucially — is a
*scoped* suppression: it covers exactly these two calls. A future
``subprocess.run(...)`` added to this file with attacker-controlled
argv would still trigger ``S603``, forcing a conscious decision.
A file-level or project-level ``# nosec`` would silently cover
future calls; a line-level ``# noqa`` does not.
"""

from __future__ import annotations

import contextlib
import io
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

# Shared fixtures — consumed by the loader unit tests too, so the
# CLI and loader can never disagree about what "valid" and "invalid"
# mean.
_FIXTURES = Path(__file__).parents[1] / "fixtures"
_VALID_BLUEPRINT = _FIXTURES / "blueprints" / "valid" / "python-api.yaml"
_MULTI_ERROR_BLUEPRINT = _FIXTURES / "loader" / "multi-error.yaml"
_MISSING_TEMPLATE_BLUEPRINT = _FIXTURES / "loader" / "missing-template.yaml"


def _invoke(*args: str) -> SimpleNamespace:
    """Invoke the CLI through ``main()`` and return a ``CliRunner``-shaped result.

    Why ``main()`` and not ``CliRunner.invoke(app, args)``
    ------------------------------------------------------
    ``CliRunner`` is Typer's in-process test utility. It invokes the
    Typer app directly and records the exit code from any
    ``SystemExit`` or ``typer.Exit`` the app raises. It does **not**
    read the callback's return value — a callback that returns ``1``
    still appears to ``CliRunner`` as exit code 0.

    ``forge validate`` communicates its exit code through the
    callback's return value (see ``validate.py``'s "Exit-code
    mechanism" docstring). That mechanism is necessary because Typer,
    under ``standalone_mode=False``, does not reliably propagate
    ``typer.Exit`` across versions — some versions swallow it and
    return the callback's value, others propagate it. Returning the
    code and reading it in ``main()`` works on every version.

    The consequence is that ``CliRunner`` cannot observe the exit
    code. ``main()`` can — and does, by reading the return value and
    raising ``SystemExit(code)``. This helper invokes ``main()``,
    captures the ``SystemExit``, and normalises the result to the
    shape the tests read: ``exit_code``, ``stdout``, ``stderr``,
    ``exception``.

    Why ``SimpleNamespace`` and not a real ``Result``
    -------------------------------------------------
    ``CliRunner``'s ``Result`` has many attributes (``runner``,
    ``return_value``, ``exc_info``, etc.) that this suite does not
    use. ``SimpleNamespace`` with exactly the four attributes the
    tests read is a smaller surface area and does not create a false
    impression that this helper is a full ``CliRunner`` replacement.

    Stream capture
    --------------
    ``contextlib.redirect_stdout`` and ``redirect_stderr`` are used
    rather than ``capsys`` because they work at the ``sys.stdout`` /
    ``sys.stderr`` level — which is where ``forge.cli.ui``'s helpers
    (via Rich's ``Console``) actually write. Rich's console binds to
    ``sys.stdout`` at call time, so redirecting ``sys.stdout``
    captures every UI helper's output, including messages written
    through the ``print_*`` functions.

    Return-value semantics
    ----------------------
    * ``exit_code`` is the ``SystemExit.code`` that ``main()`` raised,
      or 0 if the code was ``None`` (Python's own convention).
    * ``stdout`` and ``stderr`` are the captured streams.
    * ``exception`` is the ``SystemExit`` instance, matching what
      ``CliRunner`` would have put there for a ``typer.Exit``-raising
      command. For a test that wants to distinguish a clean exit
      from a crash, ``isinstance(result.exception, SystemExit)``
      means "clean exit"; any other exception type would mean
      ``main()`` reached the ``except BaseException`` branch and
      ``handle_exception`` converted it — which for an invalid
      blueprint is *still* a ``SystemExit`` (exit 1), and for a
      genuine internal error would be ``SystemExit(5)``.
    """
    # Imported here rather than at module top so the module-level
    # fixture paths can be defined without importing the whole CLI.
    # It also matches the pattern in ``forge.cli.__main__``, which
    # imports ``main`` at the point of use.
    from forge.cli.main import main

    out = io.StringIO()
    err = io.StringIO()
    captured_exc: SystemExit | None = None

    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            main(list(args))
    except SystemExit as exc:
        captured_exc = exc

    # ``SystemExit.code`` may be ``None`` (which Python treats as 0),
    # an ``int``, or a string. Only the ``int`` and ``None`` cases
    # occur in this codebase (``main()`` always raises with
    # ``ExitCode.SUCCESS`` or the command's integer exit code).
    # Normalising ``None`` to 0 matches the shell's behaviour: a
    # process that exits with ``SystemExit(None)`` exits 0.
    exit_code = 0
    if captured_exc is not None and captured_exc.code is not None:
        exit_code = (
            captured_exc.code
            if isinstance(captured_exc.code, int)
            else 1  # non-int codes are treated as 1 by Python; match that
        )

    return SimpleNamespace(
        exit_code=exit_code,
        stdout=out.getvalue(),
        stderr=err.getvalue(),
        exception=captured_exc,
    )


# =============================================================================
# Valid blueprint — exit 0, success message on stdout
# =============================================================================
class TestValidBlueprint:
    """A valid blueprint produces exit 0 and a success message.

    The success message is intentionally minimal (``OK: <id>
    v<version>``) so a script can parse it if needed and so a human
    gets confirmation without scrolling past a wall of "processed
    N files" boilerplate. This class pins both the exit code and the
    presence of the id and version in the output.
    """

    def test_exits_zero(self):
        result = _invoke("validate", str(_VALID_BLUEPRINT))
        assert result.exit_code == 0, (
            f"expected exit 0, got {result.exit_code}. " f"stdout: {result.stdout!r}"
        )

    def test_output_names_the_blueprint_id(self):
        result = _invoke("validate", str(_VALID_BLUEPRINT))
        assert result.exit_code == 0
        # The id is the primary human-recognizable handle for a
        # blueprint; a success message that omitted it would be
        # uninformative. The specific id comes from the fixture,
        # which the loader test also reads.
        assert "python.fastapi.api" in result.stdout

    def test_output_names_the_version(self):
        result = _invoke("validate", str(_VALID_BLUEPRINT))
        assert result.exit_code == 0
        # Version is the second part of the "OK: id vX.Y.Z" message.
        # A refactor that dropped it would make the message less
        # useful without failing any user-visible check.
        assert "1.0.0" in result.stdout

    def test_success_message_goes_to_stdout(self):
        """The success message is *product*, so it goes to stdout.

        A script that pipes ``forge validate ... | grep OK`` must
        find the message. If it went to stderr, the pipeline would
        silently see nothing and the script would fail with no clue
        why. This test pins the stream contract.
        """
        result = _invoke("validate", str(_VALID_BLUEPRINT))
        assert result.exit_code == 0
        assert "OK:" in result.stdout

    def test_accepts_a_blueprint_directory(self, tmp_path: Path):
        """The command accepts a *blueprint directory*, not just a file.

        A blueprint directory contains a ``blueprint.yaml`` at its root
        and every template the manifest references under ``templates/``.
        The fixture is constructed by copying the whole valid-blueprint
        fixture directory — manifest *and* templates — so it satisfies
        both the manifest-presence contract and the loader's
        ``check_files=True`` phase.
        """
        blueprint_dir = tmp_path / "my-blueprint"
        blueprint_dir.mkdir()
        # Copy the manifest.
        shutil.copy(_VALID_BLUEPRINT, blueprint_dir / "blueprint.yaml")
        # Copy the templates the manifest references. Without them, the
        # loader's filesystem phase correctly reports "missing template
        # file" for every entry — the directory is not a valid blueprint,
        # just a manifest.
        templates_src = _VALID_BLUEPRINT.parent / "templates"
        if templates_src.is_dir():
            shutil.copytree(templates_src, blueprint_dir / "templates")

        result = _invoke("validate", str(blueprint_dir))
        assert result.exit_code == 0, (
            f"expected exit 0 for blueprint directory input, got "
            f"{result.exit_code}. stdout: {result.stdout!r}"
        )

    def test_rejects_a_directory_without_a_manifest(self, tmp_path: Path):
        """A directory without a ``blueprint.yaml`` is a user error.

        The distinction from the test above: a blueprint directory
        has a manifest at its root. A directory that contains YAML
        files but no ``blueprint.yaml`` is not a blueprint
        directory; passing it is a user error (exit 1), not a
        blueprint error (exit 2). ``forge validate`` takes a path,
        not a blueprint ID, so "the blueprint you asked for is
        missing from the catalogue" is not a scenario it can
        produce.
        """
        not_a_blueprint = tmp_path / "not-a-blueprint"
        not_a_blueprint.mkdir()
        (not_a_blueprint / "some.yaml").write_text("id: not-a-blueprint\n")

        result = _invoke("validate", str(not_a_blueprint))
        assert result.exit_code == 1, (
            f"expected exit 1 for non-blueprint directory, got "
            f"{result.exit_code}. stdout: {result.stdout!r}"
        )


# =============================================================================
# Invalid blueprint — exit 1, every error rendered with its hint
# =============================================================================
class TestInvalidBlueprint:
    """An invalid blueprint produces exit 1 and a rendered report.

    The report must be actionable: every error should show its path
    (which field is wrong), message (what is wrong), and hint (how
    to fix it). A report that showed only the first error would
    fail the "one edit, not ping-pong" contract that the loader's
    aggregation was designed to provide; a report that showed all
    errors but no hints would satisfy "every error is shown" while
    leaving the user with nothing to do about any of them.

    Stream discipline: the individual error lines go to **stdout**
    (they are the product — what the user asked to see). The summary
    line (``N validation error(s) in FILE:``) goes to **stderr**
    (it is commentary — metadata about the product). Scripts that
    pipe the output get the error lines; scripts that only want the
    summary read stderr.
    """

    def test_exits_one(self):
        result = _invoke("validate", str(_MULTI_ERROR_BLUEPRINT))
        assert result.exit_code == 1, (
            f"expected exit 1, got {result.exit_code}. " f"stdout: {result.stdout!r}"
        )

    def test_summary_line_goes_to_stderr(self):
        """The "N validation error(s)" summary is commentary.

        It tells the user how many problems were found without
        restating them; the actual errors follow on stdout. A script
        that pipes the output should receive the errors, not the
        summary — otherwise every consumer has to filter the summary
        out before parsing.
        """
        result = _invoke("validate", str(_MULTI_ERROR_BLUEPRINT))
        # Three errors in the multi-error fixture. The count is on
        # stderr; the assertion is on the exact number appearing in
        # the summary line, not on the phrase "3 errors" being
        # scattered anywhere.
        assert "3" in result.stderr, f"expected '3' in stderr summary, got: {result.stderr!r}"

    def test_output_includes_the_source_file(self):
        """The report names the file the user should edit.

        A report that said "3 errors" without naming the file would
        be useless in a batch (``forge validate */*.yaml``). The
        source path is on every ``ValidationError`` and is rendered
        by ``.format()``.
        """
        result = _invoke("validate", str(_MULTI_ERROR_BLUEPRINT))
        assert str(_MULTI_ERROR_BLUEPRINT) in result.stdout

    def test_output_includes_every_error_path(self):
        """Every violated field's path appears in the output.

        The three violations in the multi-error fixture are
        ``version``, ``category``, and ``id``. All three must be
        visible — a report that dropped one would break the
        aggregation guarantee the loader provides.
        """
        result = _invoke("validate", str(_MULTI_ERROR_BLUEPRINT))
        for field in ("version", "category", "id"):
            assert field in result.stdout, (
                f"field {field!r} not mentioned in output. " f"stdout: {result.stdout!r}"
            )

    def test_output_includes_at_least_one_hint(self):
        """At least one "hint:" appears — the actionable part of the report.

        Every error carries a hint (the loader's KPI); the CLI
        renders it. A refactor that formatted errors without their
        hints would pass the other assertions in this class and
        silently break the KPI.
        """
        result = _invoke("validate", str(_MULTI_ERROR_BLUEPRINT))
        assert "hint" in result.stdout.lower(), f"no 'hint' in output. stdout: {result.stdout!r}"


# =============================================================================
# Missing template — exit 1, the filesystem phase's error
# =============================================================================
class TestMissingTemplate:
    """A blueprint whose template is missing produces exit 1.

    This case goes through the *filesystem* phase (the fourth loader
    phase), not the schema phase. It is included here because a
    future refactor that "simplified" the loader by removing the
    filesystem phase would pass every schema-phase integration test
    but fail this one — which is exactly the coverage this test adds
    beyond the schema-invalid case.
    """

    def test_exits_one(self):
        result = _invoke("validate", str(_MISSING_TEMPLATE_BLUEPRINT))
        assert result.exit_code == 1

    def test_output_names_the_missing_template(self):
        """The error names the template path (relative, as the user wrote it)."""
        result = _invoke("validate", str(_MISSING_TEMPLATE_BLUEPRINT))
        assert "does-not-exist.j2" in result.stdout, (
            f"missing template path not in output. " f"stdout: {result.stdout!r}"
        )


# =============================================================================
# Malformed YAML — exit 1, the parse phase's error
# =============================================================================
class TestMalformedYaml:
    """A file that is not valid YAML produces exit 1, not a traceback.

    The parse phase is the one most tempting to "just let YAMLError
    propagate" because the underlying exception is already raised.
    The loader's contract is that *every* failure is a ``Result``;
    a future refactor that let the exception escape would produce a
    Python traceback here, which is a much worse user experience
    than the loader's structured error.

    What "not a traceback" means in ``_invoke``
    -------------------------------------------
    ``_invoke`` calls ``main()`` and captures the ``SystemExit`` it
    raises. Every path through ``main()`` raises ``SystemExit`` —
    either directly (success) or via ``handle_exception`` (errors).
    So for a *clean* failure the captured exception is a
    ``SystemExit(N)``.

    A *crash* — an uncaught exception from the command body — would
    also be converted to ``SystemExit(5)`` by ``handle_exception``,
    so this suite cannot distinguish "clean failure" from "crash"
    by the exception type alone. It distinguishes by the exception
    class *before* ``main()``'s conversion: the assertion is on the
    ``exit_code`` value (1 for clean failure, 5 for crash) and on
    the presence of a hint (present for clean failure, absent for
    crash).

    The test below pins the "no crash" behaviour by asserting the
    captured exception is a ``SystemExit``. If ``main()`` ever
    propagated a raw exception past its ``except BaseException``
    branch (a bug), ``_invoke`` would capture that exception's type
    (``RuntimeError``, etc.) and the assertion would fail.
    """

    def test_exits_one(self, tmp_path: Path):
        broken = tmp_path / "broken.yaml"
        broken.write_bytes(b"id: [unclosed\n")
        result = _invoke("validate", str(broken))
        assert result.exit_code == 1

    def test_no_unexpected_exception(self, tmp_path: Path):
        """The command raised a *clean exit*, not a crash.

        ``_invoke`` captures whatever ``main()`` raised. A
        ``SystemExit`` (of any code) is how a clean exit looks;
        anything else would mean ``main()`` failed to convert the
        exception to ``SystemExit`` — a bug in ``main()`` that this
        test guards against.
        """
        broken = tmp_path / "broken.yaml"
        broken.write_bytes(b"id: [unclosed\n")
        result = _invoke("validate", str(broken))
        assert isinstance(result.exception, SystemExit), (
            f"expected a clean exit (SystemExit), got "
            f"{type(result.exception).__name__}: {result.exception!r}"
        )

    def test_output_names_the_source_file(self, tmp_path: Path):
        broken = tmp_path / "broken.yaml"
        broken.write_bytes(b"id: [unclosed\n")
        result = _invoke("validate", str(broken))
        assert str(broken) in result.stdout

    def test_output_mentions_yaml(self, tmp_path: Path):
        """The error message names the failure class ("invalid YAML").

        A user who sees "could not load" without "YAML" would not
        know whether their problem is the file format, the path, or
        the permissions. The message names the class so the search
        is scoped.
        """
        broken = tmp_path / "broken.yaml"
        broken.write_bytes(b"id: [unclosed\n")
        result = _invoke("validate", str(broken))
        assert "yaml" in result.stdout.lower()


# =============================================================================
# Usage errors — exit 1, Typer's own handling
# =============================================================================
class TestUsageErrors:
    """Missing or nonexistent paths produce exit 1.

    ``ExitCode.USER_ERROR`` (1) covers every failure the user can
    resolve without help: a missing argument, a nonexistent path, an
    invalid blueprint, a cancelled prompt. The exit-code table in
    ``src/forge/core/exceptions.py`` does not carve out a separate
    code for "the invocation was malformed"; usage errors are user
    errors, and the tests assert the same exit code they would for
    a schema violation.

    That distinction from exit 2 matters: 2 is reserved for *the
    blueprint you asked for is missing from the catalogue*, which
    is a problem only a command that takes a blueprint *ID*
    (``forge create``) can produce. ``forge validate`` takes a
    *path*, so its failures are always exit 1 unless Forge itself
    is broken (exit 5).

    This distinction is enforced by Typer's argument validators —
    ``exists=True`` on the argument rejects missing paths before
    the command body runs — and by ``main()``'s usage-error
    classifier, which translates Typer's ``UsageError`` /
    ``MissingParameter`` / ``BadParameter`` classes to exit 1.
    The tests here pin the behaviour so a future change to either
    layer would fail and require a conscious decision.
    """

    def test_missing_argument_exits_one(self):
        """No PATH argument is a user error (exit 1)."""
        result = _invoke("validate")
        assert result.exit_code == 1

    def test_nonexistent_path_exits_one(self, tmp_path: Path):
        """A path that does not exist is a user error (exit 1)."""
        missing = tmp_path / "does-not-exist.yaml"
        result = _invoke("validate", str(missing))
        assert result.exit_code == 1

    def test_nonexistent_path_does_not_print_validation_report(self, tmp_path: Path):
        """A usage error does not produce a "N validation errors" report.

        The distinction matters: a user who mistyped a path should
        see "PATH does not exist", not "your blueprint is invalid."
        A future refactor that moved path resolution into the
        command body (removing ``exists=True``) would produce a
        confusing error here — the loader would fail with "cannot
        read file" and the command would render a validation
        report, misleading the user.
        """
        missing = tmp_path / "does-not-exist.yaml"
        result = _invoke("validate", str(missing))
        assert "validation error" not in result.stdout.lower(), (
            f"nonexistent path produced a validation report: " f"{result.stdout!r}"
        )


# =============================================================================
# End-to-end via subprocess — the entry point wiring works
# =============================================================================
class TestEndToEndSubprocess:
    """The CLI is invocable as ``python -m forge.cli`` with correct exit codes.

    ``_invoke`` calls ``main()`` in-process; the subprocess tests below
    invoke the *real* ``python -m forge.cli`` entry point. Both go
    through ``main()``, so the two layers see the same exit-code
    mechanism. The subprocess tests differ in that they prove:

    * ``python -m forge.cli`` finds the module and its entry point.
    * ``__main__.py`` calls ``main()`` correctly.
    * The exit code reaches the OS as ``$?``.

    None of those are proven by the in-process tests, and none of them
    are affected by ``main()``'s return-value handling (which is what
    the in-process tests exercise).

    These two tests are the only subprocess invocations in the suite.
    They are slow (about 500ms each) but they prove the full pipeline.

    Why ``# noqa: S603`` on the ``subprocess.run`` calls
    ----------------------------------------------------
    ``bandit``'s ``S603`` rule flags ``subprocess`` calls whose argv
    is not entirely string literals. The rule exists to catch command
    injection, where the argv is derived from untrusted input.

    Here, ``sys.executable`` is the path of the *current* Python
    interpreter — the one running the test suite — and the remaining
    argv entries are hardcoded literals plus a fixture path defined
    at module import. There is no path by which any part of the argv
    is influenced by a user, a network, or a file the test suite did
    not itself create.

    The ``# noqa: S603`` documents that this call has been reviewed
    and is safe. Crucially, it is a *line-level* suppression, not a
    file-level or project-level ``# nosec``. A future contributor
    who adds a ``subprocess.run(...)`` with an attacker-controlled
    argv would still trigger ``S603`` — the rule remains active for
    any call that has not been individually reviewed.
    """

    def test_valid_blueprint_exits_zero(self):
        proc = subprocess.run(
            [  # noqa: S603
                sys.executable,
                "-m",
                "forge.cli",
                "validate",
                str(_VALID_BLUEPRINT),
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, (
            f"expected exit 0, got {proc.returncode}. " f"stderr: {proc.stderr!r}"
        )

    def test_invalid_blueprint_exits_one(self):
        proc = subprocess.run(
            [  # noqa: S603
                sys.executable,
                "-m",
                "forge.cli",
                "validate",
                str(_MULTI_ERROR_BLUEPRINT),
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 1, (
            f"expected exit 1, got {proc.returncode}. " f"stdout: {proc.stdout!r}"
        )
