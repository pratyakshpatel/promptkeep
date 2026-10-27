"""Explicit agent and tool scopes for tracing application work.

An ``agent(...)`` scope is the root of one trace; nested agents and
``tool(...)`` scopes are children in that same trace. A wrapped model call
inside any of them records one ``llm`` span linked to the existing run rows.
The context is a plain ``contextvars`` value, so it follows normal async task
inheritance and never leaks into a thread that did not inherit it.

The scope API is deliberately small: enter it, do the application work, call
``output(value)`` if there is a result, and leave. The context manager never
invokes a tool or decides whether to await anything. Telemetry failures are
shielded; the only exceptions here are explicit usage errors (a bad name, a
tool outside an agent, re-entering a closed handle, or output on a closed
handle).
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from . import checks, controls, storage
from .config import Settings, get_settings
from .conversation import current as current_conversation
from .models import new_run_key

logger = logging.getLogger("promptkeep")

# One serialized payload is capped so a large tool result cannot silently
# dominate a database. The marker is intentionally visible and keeps the
# truncated bytes inspectable as redacted text in history.
_SPAN_PAYLOAD_LIMIT = 64 * 1024

# Distinguishes "output was never supplied" (NULL) from an explicit None
# (the JSON literal null). Input has a documented default of None, so it does
# not need this distinction.
_UNSET = object()


@dataclass
class _Trace:
    """State shared by every span in one trace.

    ``sampled`` is the one successful-span cohort decision; ``problem`` flips
    when a child fails or carries a non-ok verdict; ``recorded`` flips once a
    span is kept despite a sampled-out cohort. Together they explain why a
    trace may contain a problem step without its earlier successful siblings.
    """

    sampled: bool
    conversation_id: int | None
    db_path: str
    pid: int
    problem: bool = False
    recorded: bool = False


@dataclass
class _Frame:
    """The immutable snapshot an active scope pushes into the contextvar."""

    kind: str
    name: str
    span_key: str
    trace_key: str
    parent_span_key: str | None
    started_at: str
    started: float
    input_json: str | None
    trace: _Trace
    tracked: bool
    db_path: str
    pid: int


_current: contextvars.ContextVar[_Frame | None] = contextvars.ContextVar(
    "promptkeep_agent", default=None
)


def utcnow() -> str:
    """Current UTC time in the ISO-8601 form every stored timestamp uses."""
    return datetime.now(UTC).isoformat()


def new_span_key() -> str:
    """Mint a span identity before anything is written.

    A key is an attempt to identify a span, not proof that it persisted: the
    writer may drop it, redaction may reject it, or tracking may be off.
    """
    return new_run_key()


def current() -> _Frame | None:
    """The active scope snapshot, or None outside a valid scope.

    A frame inherited by a forked child is deliberately ignored: its process
    id no longer matches, and a child must start its own explicit scopes
    rather than share the parent's lifecycle.
    """
    frame = _current.get()
    if frame is None or frame.pid != os.getpid():
        return None
    return frame


def agent(name: str, *, input: Any = None) -> _Scope:
    """Start an agent scope: the root of a trace, or a child of the active one.

    Use it as ``with`` or ``async with``. The returned handle exposes
    ``span_key`` immediately and ``trace_key`` after entry; ``output(value)``
    records an optional result when the scope closes.
    """
    _validate_name(name, "agent")
    return _Scope("agent", name, input)


def tool(name: str, *, input: Any = None) -> _Scope:
    """Start an application tool scope inside an active agent.

    A tool records what the application explicitly did around it; it never
    infers tool calls from provider responses. Entering a tool without an
    active agent is a usage error.
    """
    _validate_name(name, "tool")
    return _Scope("tool", name, input)


def finalize_model_span(
    context: _Frame,
    *,
    span_key: str,
    started_at: str,
    duration_ms: int,
    status: str,
    error: str | None,
    sampled: bool,
    checks: list[dict[str, Any]] | None = None,
) -> None:
    """Record the one model span for a physical wrapped call.

    ``context`` is the frame snapshotted when the call started, never the
    ambient context at stream completion. The span is kept when the trace
    cohort was sampled, when a problem makes it worth keeping, or when the
    trace already has a kept span (so a partial trace stays coherent). A
    database change during the call drops only the span association; ordinary
    run recording follows current configuration.
    """
    try:
        if not _attached(context):
            if context.tracked:
                logger.warning(
                    "promptkeep: trace detached by a database or process change; model span not recorded"
                )
            return

        problem = status != "ok" or any(check.get("status") != "ok" for check in (checks or ()))
        if problem:
            context.trace.problem = True
        # A kept trace keeps its shape even when this call was explicitly
        # moved to a sampled-out conversation: the span is structural, while
        # its run row follows that conversation's sampling decision.
        if not (
            sampled
            or context.trace.sampled
            or problem
            or context.trace.problem
            or context.trace.recorded
        ):
            return

        storage.record_span(
            span_key=span_key,
            trace_key=context.trace_key,
            parent_span_key=context.span_key,
            kind="llm",
            name="model",
            started_at=started_at,
            ended_at=utcnow(),
            duration_ms=duration_ms,
            status=status,
            input_json=None,
            output_json=None,
            error=error,
            sampled=sampled,
        )
        context.trace.recorded = True
    except Exception:
        logger.warning("promptkeep: failed to record model span", exc_info=True)


class _Scope:
    """One shared sync/async scope implementation for agent and tool handles."""

    def __init__(self, kind: str, name: str, input: Any) -> None:
        """Prepare a handle; keys are allocated before entry, not at exit."""
        self._kind = kind
        self._name = name
        self._input = input
        self._span_key = new_span_key()
        self._trace_key: str | None = None
        self._frame: _Frame | None = None
        self._token: contextvars.Token | None = None
        self._entered = False
        self._closed = False
        self._output_set = False
        self._output_json: str | None = None

    @property
    def span_key(self) -> str:
        """This scope's identity, available as soon as the handle exists."""
        return self._span_key

    @property
    def trace_key(self) -> str | None:
        """The trace root's identity, known once the scope has been entered."""
        return self._trace_key

    def output(self, value: Any) -> None:
        """Set the optional result recorded when this scope closes.

        The value is serialized immediately, so mutating it afterwards does
        not rewrite history. Repeated calls keep the last value; an explicit
        ``None`` is recorded as JSON null. Omitted output stays absent.
        """
        if not self._entered or self._closed:
            raise RuntimeError("output() must be called on an open agent/tool scope")
        self._output_json = _encode_payload(value)
        self._output_set = True

    def __enter__(self) -> _Scope:
        """Enter the scope, allocate its trace identity, and persist a root start."""
        if self._entered or self._closed:
            raise RuntimeError("agent/tool scopes cannot be re-entered; create a new scope")
        self._entered = True
        try:
            self._start()
        except BaseException:
            if self._token is not None:
                self._restore()
            else:
                self._entered = False
            raise
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        """Finalize the scope without ever replacing the application outcome."""
        try:
            self._finish(exc_type, exc)
        except Exception:
            logger.warning("promptkeep: failed to finalize agent scope", exc_info=True)
        finally:
            self._restore()
        return False

    async def __aenter__(self) -> _Scope:
        """Async twin of ``__enter__``."""
        return self.__enter__()

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        """Async twin of ``__exit__``; cancellation becomes status ``cancelled``."""
        return self.__exit__(exc_type, exc, tb)

    def _start(self) -> None:
        """Build the immutable frame and push it for the duration of the scope."""
        settings = get_settings()
        parent = current()
        if self._kind == "tool" and parent is None:
            raise RuntimeError("tool(...) must be entered inside an agent(...) scope")

        started_at = utcnow()
        started = time.perf_counter()
        db_path = str(settings.db_path)
        pid = os.getpid()
        tracked = settings.enabled and settings.write_mode != "off" and not checks.suppressed()

        # A nested scope inherits the trace and its cohort. A database change
        # mid-scope detaches only telemetry; application code still runs.
        if parent is not None:
            trace = parent.trace
            self._trace_key = parent.trace_key
            parent_span_key = parent.span_key
            if parent.tracked and db_path != trace.db_path:
                logger.warning(
                    "promptkeep: database changed during an active trace; child span not recorded"
                )
            tracked = tracked and parent.tracked and db_path == trace.db_path
        else:
            self._trace_key = self._span_key
            parent_span_key = None
            trace = _new_trace(settings, tracked, db_path, pid)

        # Snapshot the input now; a caller mutating it later must not rewrite
        # history. Serialization failures lose the payload, never the scope.
        input_json = _encode_payload(self._input) if tracked else None
        frame = _Frame(
            kind=self._kind,
            name=self._name,
            span_key=self._span_key,
            trace_key=self._trace_key,
            parent_span_key=parent_span_key,
            started_at=started_at,
            started=started,
            input_json=input_json,
            trace=trace,
            tracked=tracked,
            db_path=db_path,
            pid=pid,
        )
        self._frame = frame
        self._token = _current.set(frame)

        # A sampled-in root gets a running row immediately; a sampled-out one
        # waits for its final row, which can restore a missing start if a
        # problem turns up later.
        if tracked and trace.sampled:
            storage.record_span(
                span_key=frame.span_key,
                trace_key=frame.trace_key,
                parent_span_key=frame.parent_span_key,
                kind=frame.kind,
                name=frame.name,
                started_at=frame.started_at,
                status="running",
                input_json=frame.input_json,
                sampled=trace.sampled,
            )

    def _finish(self, exc_type: Any, exc: Any) -> None:
        """Write the final row when the trace's sampling and problem state allow it."""
        frame = self._frame
        if frame is None:
            return
        status, error = _outcome(exc_type, exc)
        attached = _attached(frame)

        # A suppressed or detached child shares context but cannot affect the
        # parent's sampling state or promote its other spans into storage.
        if status != "ok" and attached:
            frame.trace.problem = True
        keep = frame.trace.sampled or frame.trace.problem or frame.trace.recorded or status != "ok"
        if not keep or not attached:
            return
        storage.record_span(
            span_key=frame.span_key,
            trace_key=frame.trace_key,
            parent_span_key=frame.parent_span_key,
            kind=frame.kind,
            name=frame.name,
            started_at=frame.started_at,
            ended_at=utcnow(),
            duration_ms=int(round((time.perf_counter() - frame.started) * 1000)),
            status=status,
            input_json=frame.input_json,
            output_json=self._output_json if self._output_set else None,
            error=error,
            sampled=frame.trace.sampled,
        )
        frame.trace.recorded = True

    def _restore(self) -> None:
        """Pop the frame and close the handle, even if finalization failed."""
        token = self._token
        self._token = None
        self._entered = False
        self._closed = True
        if token is not None:
            try:
                _current.reset(token)
            except Exception:
                logger.warning("promptkeep: could not restore agent context", exc_info=True)


def _attached(frame: _Frame) -> bool:
    """Whether this frame still belongs to the configured database and process.

    A scope captures its identity at entry. A later configure() or a forked
    child must not attach new telemetry to the old trace; the application work
    itself is unaffected.
    """
    if not frame.tracked or frame.pid != os.getpid():
        return False
    return str(get_settings().db_path) == frame.db_path == frame.trace.db_path


def _new_trace(settings: Settings, tracked: bool, db_path: str, pid: int) -> _Trace:
    """Choose the trace cohort, aligning with a conversation when one is active.

    Resolving the conversation here reuses the existing sampler's conversation
    id. The row is created by the same helper a model call would use; a trace
    with no model call still has a trace record, which is its own work.
    """
    conversation_id = None
    sampled = False
    if tracked:
        try:
            active = current_conversation()
            if active is not None:
                conversation_id = storage.get_or_create_conversation(
                    active.external_id, active.title, active.metadata
                )
            sampled = controls.keep_run(settings, {"status": "ok"}, conversation_id)
        except Exception:
            logger.warning("promptkeep: failed to prepare trace sampling", exc_info=True)
            sampled = False
    return _Trace(
        sampled=sampled,
        conversation_id=conversation_id,
        db_path=db_path,
        pid=pid,
    )


def _validate_name(name: Any, kind: str) -> None:
    """Reject a missing or blank explicit name before any scope state changes."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{kind}() requires a non-empty string name")


def _encode_payload(value: Any) -> str | None:
    """Serialize one span payload, capped with a visible byte-count marker.

    The existing JSON conventions are reused: values are JSON, unsupported
    values fall back to ``repr``, and a custom serializer/repr failure loses
    only the payload. The cap is applied after serialization, so it bounds
    stored bytes rather than the cost of serializing an arbitrary object.
    """
    try:
        encoded = json.dumps(value, ensure_ascii=False, default=repr).encode("utf-8")
    except Exception:
        logger.warning("promptkeep: failed to serialize span payload", exc_info=True)
        return None
    if len(encoded) <= _SPAN_PAYLOAD_LIMIT:
        return encoded.decode("utf-8")
    preview = encoded[:_SPAN_PAYLOAD_LIMIT].decode("utf-8", errors="ignore")
    return f"{preview}\n…[promptkeep truncated: {len(encoded)} bytes]"


def _outcome(exc_type: Any, exc: Any) -> tuple[str, str | None]:
    """Map a scope exit to a status and optional error text.

    Cancellation is inspected before ordinary exceptions because
    ``asyncio.CancelledError`` is a ``BaseException``. The original exception
    is never wrapped; the context manager returns false and lets it continue.
    """
    if exc_type is None:
        return "ok", None
    if isinstance(exc, asyncio.CancelledError) or (
        isinstance(exc_type, type) and issubclass(exc_type, asyncio.CancelledError)
    ):
        return "cancelled", "cancelled"
    if exc is not None:
        return "error", repr(exc)
    return "error", getattr(exc_type, "__name__", "error")


__all__ = ["agent", "tool"]
