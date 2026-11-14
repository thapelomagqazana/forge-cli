"""Typed progress events — the staging engine's only channel to the UI.

Why a typed event instead of printing from the engine?
-------------------------------------------------------
Three concerns get tangled the moment an engine writes to a stream:

1. **Testability.** Capturing stdout/stderr to assert on progress is
   fragile: a library upgrade that changes line buffering, a colour
   helper that decides to wrap, a future `--json` mode that needs clean
   stdout — all of these force tests to know things about rendering.

2. **Separation of audience.** The engine's audience is "somebody who
   wants to know what it's doing." That somebody might be a human at a
   TTY (progress bar), a CI log (one line per file, no colours), a JSON
   consumer (events embedded in an envelope), or a metrics collector
   (counters keyed by stage). All four want the *same* information in
   different shapes. A typed event is the shape-neutral middle.

3. **Extensibility without churn.** Adding a field to a `ProgressEvent`
   (say, `bytes_written`) is additive — existing sinks ignore it.
   Adding a field to a printed line is a text-formatting exercise that
   inevitably breaks some consumer somewhere. Data is easy to extend;
   prose is not.

Why a `Protocol` and not an abstract base class?
------------------------------------------------
Structural typing. Any callable with the right signature is a valid
sink — a bound method, a lambda, a `functools.partial`, a class with
`__call__`. No import required, no inheritance ceremony, no "you must
subclass `ProgressSink`" friction. Python's type checkers accept it;
runtime does not care.

Why `null_sink` as a default?
-----------------------------
The alternative is `progress=None` and `if progress is not None` at
every call site. That works, but it forces every sink consumer to
handle two cases (something and nothing), and it makes the engine's
control flow branch on a plumbing detail. A real function that does
nothing is simpler and cannot be forgotten: callers just call it.

Concurrency note
----------------
`ProgressSink` implementations may be invoked from multiple staging
calls running concurrently (each call owns its own sink, but a caller
could supply a shared sink for multi-project staging). The protocol
makes no ordering guarantee across calls and no thread-safety promise.
Implementations that collect events should either be lock-protected
internally or supplied once per call. The default sink and the
`list.append` used in tests are both thread-safe for this use.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final, Protocol

# Stages are a closed set. Downstream UI typically switches on them
# (progress bar for "render"/"copy", spinner for "start", summary line
# for "finish"). Keeping them as a documented constant tuple means:
#   * typos are caught by tests that exhaust the set,
#   * adding a stage is a visible change (update here + tests + docs),
#   * the JSON-serialised `stage` field has a stable vocabulary.
STAGES: Final[tuple[str, ...]] = ("start", "render", "copy", "finish")


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One observable step of the staging phase.

    Attributes
    ----------
    stage
        One of `STAGES`. Machine-readable so a UI can filter without
        parsing the path or the message.
    index
        1-based position of the file being processed. `0` on the
        `start` event; equals `total` on the `finish` event, so a
        progress bar can be advanced purely from `index / total` without
        a separate counter.
    total
        Total number of files in the plan. Stable across all events of
        a single `stage()` call, so a UI can calculate percentages.
    path
        Destination-relative path of the file this event refers to.
        Empty for `start`/`finish`, where no file is implicated. Never
        the absolute staging path: the staging directory is an internal
        implementation detail whose name may change between releases
        and must not leak into user logs or automation.
    elapsed_ms
        Milliseconds since the start of the phase (for `start`/`finish`)
        or since the file began processing (for `render`/`copy`).
        Wall-clock timings are informational; they are not stable across
        runs and must not be asserted on in golden tests.
    """

    stage: str
    index: int
    total: int
    path: str = ""
    elapsed_ms: float = 0.0

    def is_file_event(self) -> bool:
        """True for per-file events; false for start/finish.

        Exposed as a method (not a property) so a future variant of the
        event — say, "retry" — can be added without silently changing
        the semantics of existing callers that use `is_file_event`.
        """
        return self.stage in {"render", "copy"}


class ProgressSink(Protocol):
    """Consumer of progress events.

    Implementations must:
        * not raise (the engine treats sinks as advisory; a throwing
          sink would turn a working generation into a failed one),
        * not mutate the event (it is frozen; mutation is impossible,
          but a sink should not rely on side-effecting the shared
          object's identity either),
        * not block indefinitely (a slow sink slows the engine).

    Return value is ignored: a sink either consumes the event or does
    not. Returning `None` is conventional but not required.
    """

    def __call__(self, event: ProgressEvent) -> None:  # pragma: no cover - protocol
        ...


def null_sink(_event: ProgressEvent) -> None:
    """Default sink: drop every event.

    The name is `null_sink` (not `noop` or `silent`) to make it obvious
    at call sites that a *sink* is being supplied — `progress=null_sink`
    reads as "there is a sink and it does nothing," whereas
    `progress=None` reads as "there is no sink and control flow must
    branch to handle that." The first is simpler to reason about.
    """
    return None


# A tiny convenience for the very common "make me a sink that records
# into a list" case in tests. Kept here (rather than in test utilities)
# because it is trivial, has no dependencies, and lets any future
# consumer — a CLI debug mode, a metrics collector during development —
# benefit from the same helper without duplicating it.
class ListSink:
    """`ProgressSink` implementation that appends events to a list.

    Not thread-safe by itself; callers running concurrent `stage()`
    calls against one instance should add their own lock or use one
    sink per call. The engine does not call sinks concurrently within a
    single `stage()` call, so a single-threaded staging pipeline needs
    no synchronisation.
    """

    __slots__ = ("events",)

    def __init__(self) -> None:
        self.events: list[ProgressEvent] = []

    def __call__(self, event: ProgressEvent) -> None:
        self.events.append(event)

    def __len__(self) -> int:
        return len(self.events)

    def __iter__(self) -> Iterator[ProgressEvent]:
        return iter(self.events)
