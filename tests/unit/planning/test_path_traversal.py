"""Unit tests for path-traversal rejection across the planner pipeline.

Path traversal is the sharpest edge in the whole generation pipeline.
Every other planner failure is a *user error* — a collision is a
blueprint authoring mistake, an empty substitution is a templating
mistake. Traversal is different: it is the boundary between "this
blueprint is buggy" and "this blueprint is *hostile*". A blueprint that
escapes its own directory can:

* read a file the user did not intend to share (a source pointing at
  ``../../.ssh/id_rsa``),
* write a file outside the target project (a destination pointing at
  ``../../.bashrc``),
* in combination with a symlink, reach anywhere the CLI process can
  reach.

Three checkpoints, by design
----------------------------
Traversal is rejected at **three distinct points** in the pipeline. The
redundancy is deliberate — "defence in depth" is the whole design here:

1. **``FileEntry`` (Pydantic, at construction).** The model's ``source``
   and ``destination`` fields are constrained by a pattern that rejects
   leading ``/`` and any ``..`` segment. This is the *earliest* possible
   checkpoint: a blueprint whose ``files[]`` entries contain raw
   traversal cannot even be parsed into a model. **Most of this file
   exercises this checkpoint** — it is where raw traversal dies.

2. **``planning.destinations.resolve`` (at rendering time).** After
   substitution, the *rendered* destination is passed through
   ``planning.paths.is_safe_relative``. This catches what the model
   pattern cannot: traversal introduced by *variable values* (a
   ``{{ name }}`` that renders to ``".."``), empty segments produced by
   empty substitutions, NUL bytes, and over-length strings.

3. **``loading.fs_checks`` (at load time).** The *resolved* path is
   checked against the blueprint root, which catches symlink-based
   escapes that the lexical checks cannot see (``templates/evil.j2``
   where ``templates`` is a symlink to ``/etc``). Tested in
   ``test_loading_fs_checks.py`` — a different file, because it needs
   a real filesystem.

Why three checkpoints?
----------------------
Each one catches a *different way* the rule can be violated, against a
*different representation* of the path:

* The model checkpoint catches raw strings the author typed.
* The resolver checkpoint catches strings that *become* paths after
  substitution — invisible to the model, because at model-construction
  time the value is a template, not a path.
* The loader checkpoint catches paths that *become* escapes after
  filesystem resolution (symlinks) — invisible to the string checks.

Removing any one of them leaves a real gap. The tests in this file are
the executable specification of checkpoints 1 and 2 — the two that run
without a filesystem.

Test structure
--------------
* ``TestModelRejectsRawTraversal`` — asserts that ``FileEntry``
  construction raises for the shapes the schema pattern catches
  (traversal, absolute paths, empty). This is the checkpoint that fires
  *before* the planner ever runs, and the most important one to pin
  because it is the earliest.
* ``TestPlannerRejectsSubstitutedTraversal`` — the cases the schema
  pattern cannot catch, because they only exist after substitution.
* ``TestRuleAndResolverAgreement`` — the equivalence between
  ``is_safe_relative`` and ``resolve`` on strings the model does *not*
  reject (NUL, over-length, empty-segment-after-substitution).
* ``TestNoPartialAcceptance`` — a plan with any traversal (or any
  traversal-adjacent failure) is rejected entirely.

The tests are grouped by *checkpoint*, not by *field* (source vs.
destination), because a future contributor adding a new kind of path
(a ``post_create`` script path, say) needs to know which checkpoint
to hook into, not which attribute the path ends up on.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from forge.loading.result import Err, Result
from forge.models import (
    Blueprint,
    Category,
    Compatibility,
    FileEntry,
)
from forge.planning import FileMapper
from forge.planning.destinations import resolve as resolve_destination
from forge.planning.paths import is_safe_relative


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _blueprint_with_files(files: list[FileEntry]) -> Blueprint:
    """Construct a minimal valid ``Blueprint`` declaring ``files``.

    A real ``Blueprint`` instance, not a fake, so the planner validates
    against the model's actual contract. The helper exists so the tests
    in this file construct blueprints in exactly one place — if
    ``Blueprint`` gains a required field, the tests all fail together
    with one fix, rather than each test failing for its own irrelevant
    reason.
    """
    return Blueprint(
        schema_version="1.0.0",
        id="sample.traversal",
        name="Path Traversal Demo",
        version="1.0.0",
        description="Fixture for traversal-rejection tests.",
        category=Category.api,
        files=files,
        verification=[
            {"id": "ok", "type": "required_file", "path": "README.md"},
        ],
        compatibility=Compatibility(min_forge_version="1.0.0"),
    )


def _map(*files: FileEntry, variables: dict | None = None) -> Result:
    """Run ``files`` through the planner with ``variables`` (default empty).

    Does *not* assert on success — many tests in this file assert on
    rejection. The ``variables`` parameter exists because the most
    interesting cases (substituted traversal, empty substitution) require
    a *specific* value to be passed, and defaulting to ``{}`` would force
    every caller to pass a dict even when they do not need one.
    """
    return FileMapper().map(
        _blueprint_with_files(list(files)),
        variables or {},
    )


def _assert_rejected(result: Result, *, code: str) -> None:
    """Assert ``result`` is an ``Err`` whose findings include ``code``.

    Uniform assertion shape across every rejection test, so a future
    contributor adding a new traversal shape writes a one-line assertion
    instead of re-deriving the shape from an example. The message names
    the *expected* code (the contract) and the *found* codes (the
    diagnostic), which is what a reviewer needs to triage a failure.
    """
    assert isinstance(result, Err), f"expected the planner to reject this input, got Ok: {result!r}"
    codes = {f.code for f in result.error.findings}
    assert code in codes, f"expected code {code!r}, got {codes!r}"


# ---------------------------------------------------------------------------
# Checkpoint 1: the model rejects raw traversal at construction
# ---------------------------------------------------------------------------
class TestModelRejectsRawTraversal:
    """``FileEntry`` rejects raw traversal, absolute paths, and empties.

    The ``FileEntry`` model has a pattern on both ``source`` and
    ``destination``:

        ^(?!/)(?!.*\\.\\.(?:/|$)).+$

    which rejects a leading ``/``, any ``..`` segment, and the empty
    string. This is the *earliest* checkpoint in the pipeline: a
    blueprint whose ``files[]`` entries contain these shapes cannot even
    be constructed as a model, let alone planned.

    These tests assert that ``FileEntry`` construction *raises*, rather
    than that the planner rejects. The distinction matters: a future
    change that loosened the model pattern (perhaps to allow a
    ``./foo`` form) would pass planner-level tests but fail these,
    which is the correct signal — the earliest checkpoint should not be
    weakened just because a later checkpoint would still catch the
    shape.
    """

    @pytest.mark.parametrize(
        "source",
        [
            "../escape.j2",
            "../../escape.j2",
            "../../../etc/passwd",
            "src/../../escape.j2",
            "src/../../../escape.j2",
            "templates/../../outside.j2",
            "/etc/passwd",
            # An absolute path on POSIX. Previously ``/tmp/template.j2``
            # was used here, but Ruff's S108 flags the literal ``/tmp``
            # prefix as a probable-insecure-temp-file pattern (it cannot
            # tell this test only *names* the path). ``/etc`` serves the
            # same role — obviously absolute, obviously out-of-bounds —
            # without tripping the heuristic. The test contract
            # ("absolute source is rejected") is unchanged.
            "/etc/template.j2",
            "\\Windows\\system32\\config",
            "C:\\Users\\template.j2",
        ],
    )
    def test_source_construction_rejects_traversal(self, source: str) -> None:
        """``FileEntry(source=…)`` raises for a traversal-shaped source."""
        with pytest.raises(ValidationError) as exc_info:
            FileEntry(source=source, destination="out/x.py")
        # The failure must be on the `source` field, not on some other
        # field the constructor tripped over.
        errors = exc_info.value.errors()
        assert any(
            err["loc"] == ("source",) for err in errors
        ), f"expected a `source` validation error, got: {errors!r}"

    @pytest.mark.parametrize(
        "destination",
        [
            "../escape.py",
            "../../escape.py",
            "../../../etc/evil",
            "src/../../escape.py",
            "out/../escape.py",
            "/etc/evil",
            "\\Windows\\system32\\evil.dll",
            "C:\\Users\\evil.exe",
        ],
    )
    def test_destination_construction_rejects_traversal(self, destination: str) -> None:
        """``FileEntry(destination=…)`` raises for a traversal-shaped destination."""
        with pytest.raises(ValidationError) as exc_info:
            FileEntry(source="a.j2", destination=destination)
        errors = exc_info.value.errors()
        assert any(
            err["loc"] == ("destination",) for err in errors
        ), f"expected a `destination` field error, got: {errors!r}"

    def test_empty_source_is_rejected(self) -> None:
        """The pattern requires ``.+`` — empty strings do not construct."""
        with pytest.raises(ValidationError):
            FileEntry(source="", destination="out/x.py")

    def test_empty_destination_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            FileEntry(source="a.j2", destination="")


# ---------------------------------------------------------------------------
# Checkpoint 2: the resolver rejects traversal introduced by substitution
# ---------------------------------------------------------------------------
class TestPlannerRejectsSubstitutedTraversal:
    """Traversal introduced by *variable values* is rejected at resolve time.

    The model pattern cannot catch this case: at ``FileEntry``
    construction time, the destination is the *template*
    ``"out/{{ name }}/x.py"`` — a string with no ``..`` segment, no
    leading ``/``, and non-empty. The pattern passes. Only after
    substitution does the traversal appear, and the only checkpoint
    that sees it is ``destinations.resolve``, which runs the *rendered*
    string through ``is_safe_relative``.

    This is the case a naive "check the template, not the result"
    implementation would miss, and it is the *easiest* escape to
    construct accidentally: a user typing ``--name ".."`` while
    debugging, or a hostile blueprint exploiting a variable the user
    is expected to fill in without thinking.
    """

    def test_dot_dot_from_variable_is_rejected(self) -> None:
        """``{{ name }}`` = ``".."`` → ``out/../x.py`` — rejected."""
        result = _map(
            FileEntry(source="a.j2", destination="out/{{ name }}/x.py"),
            variables={"name": ".."},
        )
        _assert_rejected(result, code="destination.traversal")

    def test_dot_dot_with_suffix_from_variable_is_rejected(self) -> None:
        """``{{ name }}`` = ``"../.."`` escapes further — still rejected."""
        result = _map(
            FileEntry(source="a.j2", destination="out/{{ name }}/x.py"),
            variables={"name": "../.."},
        )
        _assert_rejected(result, code="destination.traversal")

    def test_absolute_path_from_variable_is_rejected(self) -> None:
        """``{{ path }}`` = ``"/etc"`` → an absolute destination — rejected."""
        result = _map(
            FileEntry(source="a.j2", destination="{{ path }}/x.py"),
            variables={"path": "/etc"},
        )
        _assert_rejected(result, code="destination.absolute")

    def test_traversal_in_the_middle_of_a_variable_is_rejected(self) -> None:
        """``{{ name }}`` = ``"a/../../b"`` — traversal not just at the edges."""
        result = _map(
            FileEntry(source="a.j2", destination="out/{{ name }}/x.py"),
            variables={"name": "a/../../b"},
        )
        _assert_rejected(result, code="destination.traversal")

    def test_empty_variable_producing_empty_segment_is_rejected(self) -> None:
        """``{{ name }}`` = ``""`` → ``out//x.py`` — empty segment rejected.

        Not "traversal" in the ``..`` sense, but the same class of bug:
        a variable that was expected to name a path component expanded
        to nothing. The code is ``destination.empty_segment`` (not
        ``destination.traversal``), and the test asserts the specific
        code because the *fix* differs: "supply a value" vs. "remove
        the ``..``".
        """
        result = _map(
            FileEntry(source="a.j2", destination="out/{{ name }}/x.py"),
            variables={"name": ""},
        )
        _assert_rejected(result, code="destination.empty_segment")


# ---------------------------------------------------------------------------
# Checkpoint 2 (continued): the rule and the resolver agree on strings
#            the model *does not* reject
# ---------------------------------------------------------------------------
class TestRuleAndResolverAgreement:
    """``is_safe_relative`` and ``resolve`` reject the same strings.

    These are the shapes the model pattern does *not* catch — NUL
    bytes, over-length strings, empty-segment shapes with no ``..`` or
    leading ``/``. The model passes them; the resolver's safety check
    must reject them.

    The equivalence test is the trip wire for divergence between the
    two entry points into the same rule (``is_safe_relative`` called
    directly, and the call inside ``resolve`` after rendering).
    """

    @pytest.mark.parametrize(
        "path",
        [
            "\x00null",  # NUL byte — not caught by the model pattern
            "a" * 500,  # over the length limit
            "src//double",  # empty segment (no traversal)
        ],
    )
    def test_rule_and_resolver_reject_same_unsafe_paths(self, path: str) -> None:
        """Both entry points reject the shapes the model pattern misses."""
        assert is_safe_relative(path) is False, (
            f"{path!r} is expected to be unsafe, but is_safe_relative "
            f"accepted it — a rule regression."
        )

        destination, finding = resolve_destination(path, {})
        assert destination == ""
        assert finding is not None


class TestResolverAcceptsTemplateRenderOutput:
    """``resolve`` accepts what a *successful* render produces.

    This is deliberately *not* an equivalence with ``is_safe_relative``:
    a template that resolves to a safe path passes ``resolve``; a
    template that resolves to nothing or to an undefined variable
    correctly produces a finding. The distinction is what this class
    pins — a well-meaning change that made ``resolve`` accept *any*
    string would blur the line between "the path is safe" and "the
    template rendered successfully", which are two different questions.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "README.md",
            "src/main.py",
            "src/nested/deep/file.py",
            "my file.py",
            "my..file.py",  # `..` as a substring is not a traversal segment
        ],
    )
    def test_plain_strings_are_accepted(self, path: str) -> None:
        """A safe plain string is accepted unchanged."""
        destination, finding = resolve_destination(path, {})
        assert finding is None, f"{path!r}: unexpected finding {finding!r}"
        assert destination == path

    def test_template_with_supplied_variable_renders_and_is_accepted(self) -> None:
        """``src/{{ name }}.py`` with ``name="app"`` renders to ``src/app.py``."""
        destination, finding = resolve_destination("src/{{ name }}.py", {"name": "app"})
        assert finding is None
        assert destination == "src/app.py"

    def test_template_with_undefined_variable_produces_finding(self) -> None:
        """The path *template* is safe, but the *render* fails — a finding.

        An earlier version of this test asserted that ``resolve`` returns
        no finding for ``"src/{{name}}.py"`` with no ``name`` value.
        That assertion was wrong: the template is safe, but rendering it
        requires a variable that was not supplied, and ``resolve``'s job
        includes propagating that failure as a finding. The failure
        code is ``destination.render_failed`` (the renderer's
        undefined-variable error, translated at the resolver boundary).
        """
        destination, finding = resolve_destination("src/{{name}}.py", {})
        assert destination == ""
        assert finding is not None
        assert finding.code == "destination.render_failed"


# ---------------------------------------------------------------------------
# The plan is all-or-nothing
# ---------------------------------------------------------------------------
class TestNoPartialAcceptance:
    """A plan with any traversal is rejected entirely, not partially.

    The writer must never see a plan that contains a traversal. A
    "mostly safe" plan whose traversal is *skipped* is worse than a
    rejected one: it quietly produces a different project than the
    blueprint described, with no signal to the user.
    """

    def test_one_substituted_traversal_rejects_many_valid_files(self) -> None:
        """Ten valid files plus one substituted traversal → whole plan fails."""
        valid = [FileEntry(source=f"f{i}.j2", destination=f"out/f{i}.py") for i in range(10)]
        result = _map(
            *valid,
            FileEntry(source="a.j2", destination="out/{{ name }}/x.py"),
            variables={"name": ".."},
        )
        _assert_rejected(result, code="destination.traversal")

    def test_multiple_substituted_traversals_all_reported(self) -> None:
        """Every substituted traversal is reported — none is silently skipped."""
        result = _map(
            FileEntry(source="a.j2", destination="out/{{ a }}/x.py"),
            FileEntry(source="b.j2", destination="out/{{ b }}/y.py"),
            variables={"a": "..", "b": "../.."},
        )
        assert isinstance(result, Err)
        traversal_findings = [f for f in result.error.findings if f.code == "destination.traversal"]
        assert len(traversal_findings) == 2
