import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Self


def _plain_params(fn: Callable[..., object]) -> dict[str, type]:
    """fn's parameters as {name: class}. Each must be required and annotated with a plain class.
    Shared by Stage.from_callable and Guard.from_callable."""
    params: dict[str, type] = {}
    for pname, p in inspect.signature(fn).parameters.items():
        if p.kind not in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY) or p.default is not p.empty:
            raise TypeError(f"{fn.__name__}.{pname}: must be a plain required parameter")
        if not isinstance(p.annotation, type):
            raise TypeError(f"{fn.__name__}.{pname}: must be annotated with a plain class")
        params[pname] = p.annotation
    return params


@dataclass(frozen=True, slots=True)
class Stage:
    """What a step is. Build it with from_callable; requires/produces are derived."""
    name: str
    run: Callable[[Mapping[str, object]], Awaitable[dict[str, object]]]
    requires: dict[str, type]
    produces: dict[str, type]

    @classmethod
    def from_callable(
        cls,
        fn: Callable[..., object],
        *,
        fixed: dict[str, object] | None = None,
        produces: str | None = None,
    ) -> Self:
        """Build a Stage that calls `fn`, using fn's signature to work out what the stage
        reads from the pipeline's context (the dict of values passed from step to step)
        and what it adds to it.

        Reading: each parameter name is a context key, and its annotation is the type that
        key must hold. When the stage runs, fn is called with those values from the context.

        Constants: `fixed` gives some parameters the same value on every call, for example
        fixed={"mode": "fast"}. Those parameters are not read from the context.

        Writing: if `produces` is given, fn's return value is added to the context under
        that name, and the return annotation is its type. Without it, the return value is
        thrown away.

        Example: `def start() -> int` with produces="log_offset" reads nothing and adds
        "log_offset" (an int), which `def wait_for_log(log_offset: int)` then reads.

        Name: the stage's name, used in the step log and failure messages, is fn's name.
        If `fixed` is given, the constants are added to it, so two stages made from the
        same function can be told apart: "f(mode='fast')" and "f(mode='slow')".

        Types are checked with isinstance, so every parameter must be required (no default,
        no *args or **kwargs) and annotated with a plain class (not `str | None` or
        `list[int]`). Anything else raises TypeError here, when the stage is built.
        """
        fixed = fixed or {}
        sig = inspect.signature(fn)

        unknown = fixed.keys() - sig.parameters.keys()
        if unknown:
            raise TypeError(f"{fn.__name__}: fixed values for unknown parameters {sorted(unknown)}")

        requires: dict[str, type] = {}
        for pname, annotation in _plain_params(fn).items():
            if pname in fixed:
                if not isinstance(fixed[pname], annotation):
                    raise TypeError(f"{fn.__name__}.{pname}: fixed value is not {annotation.__name__}")
                continue
            requires[pname] = annotation

        produced: dict[str, type] = {}
        if produces is not None:
            if not isinstance(sig.return_annotation, type):
                raise TypeError(f"{fn.__name__}: return must be a plain class to produce {produces!r}")
            produced[produces] = sig.return_annotation

        name = fn.__name__
        if fixed:
            name += "(" + ", ".join(f"{k}={v!r}" for k, v in fixed.items()) + ")"

        # Calls fn with the fixed values plus its required keys from the context (sync or async),
        # and returns the produced value as a one-key dict after checking its type.
        async def run(ctx: Mapping[str, object]) -> dict[str, object]:
            result = fn(**fixed, **{k: ctx[k] for k in requires})
            if inspect.isawaitable(result):
                result = await result
            if produces is None:
                return {}
            if not isinstance(result, produced[produces]):
                raise TypeError(
                    f"{name} produced {type(result).__name__} for {produces!r}, "
                    f"expected {produced[produces].__name__}"
                )
            return {produces: result}

        return cls(name, run, requires, produced)


@dataclass(frozen=True, slots=True)
class Guard:
    """Decides whether a step runs. Parameter names are context keys, annotations their types."""
    name: str
    test: Callable[[Mapping[str, object]], bool]
    requires: dict[str, type]

    @classmethod
    def from_callable(cls, fn: Callable[..., bool]) -> Self:
        """Build a Guard from a plain, synchronous function annotated `-> bool`.

        Same parameter rules as Stage.from_callable (required, annotated with a plain class),
        but no `fixed` and no `produces`: a guard only reads the context. An async function
        or a missing `-> bool` annotation raises TypeError here, when the guard is built.
        The returned value is checked to be a bool when the guard runs.
        """
        if inspect.iscoroutinefunction(fn):
            raise TypeError(f"{fn.__name__}: a guard must be a plain function, not async")
        if inspect.signature(fn).return_annotation is not bool:
            raise TypeError(f"{fn.__name__}: a guard must be annotated to return bool")
        requires = _plain_params(fn)

        def test(ctx: Mapping[str, object]) -> bool:
            result = fn(**{k: ctx[k] for k in requires})
            if not isinstance(result, bool):
                raise TypeError(f"{fn.__name__} returned {type(result).__name__}, expected bool")
            return result

        return cls(fn.__name__, test, requires)


@dataclass(frozen=True, slots=True)
class PipelineStep:
    """How a step is used."""
    stage: Stage
    critical: bool = True
    retries: int = 3                 # retries after the first try; at most retries + 1 tries
    when: Guard | None = None        # None = always runs; False = skipped, logged with attempts=0

    def __post_init__(self) -> None:
        if self.retries < 0:
            raise ValueError(f"{self.stage.name}: retries must not be negative, got {self.retries}")


@dataclass(frozen=True, slots=True)
class StepRecord:
    name: str
    ok: bool
    attempts: int                    # tries; 0 = skipped by its guard (ok=True) or the guard failed (ok=False)
    error: str | None


class Pipeline:
    """Runs steps in order over a shared context, stopping at the first critical failure.
    Finally steps always run, including on cancellation."""

    def __init__(self, steps: list[PipelineStep], finally_steps: list[PipelineStep]) -> None:
        self.steps = steps
        self.finally_steps = finally_steps
        self.step_log: list[StepRecord] = []
        self.failure: str | None = None
        self._check_structure()

    # --- checks ---

    def _produced(self) -> dict[str, type]:
        """Every key some step produces, with its type."""
        return {k: t for s in self.steps + self.finally_steps for k, t in s.stage.produces.items()}

    def _needs(self, step: PipelineStep) -> dict[str, type]:
        """Everything the step reads: its stage's keys plus its guard's keys."""
        guard = step.when.requires if step.when is not None else {}
        return step.stage.requires | guard

    def _check_structure(self) -> None:
        """Reject steps that can't fit together, before anything runs.

        Each key has one type and at most one producer. A normal step (or its guard) may
        require a produced key only after an earlier critical, unconditional step produced
        it, since a non-critical or skipped producer may not have run. Finally steps and
        their guards can't require produced keys at all, because they run even when the
        producer never did.
        """
        types: dict[str, type] = {}
        producers: dict[str, str] = {}
        for step in self.steps + self.finally_steps:
            sources = [step.stage.requires, step.stage.produces]       # checked separately, so no merge hides a conflict
            if step.when is not None:
                sources.append(step.when.requires)
            for source in sources:
                for k, t in source.items():
                    if types.setdefault(k, t) is not t:
                        raise TypeError(f"key {k!r} used as both {types[k].__name__} and {t.__name__}")
            for k in step.stage.produces:
                if k in producers:
                    raise ValueError(f"key {k!r} produced by both {producers[k]} and {step.stage.name}")
                producers[k] = step.stage.name

        available: set[str] = set()
        for step in self.steps:
            for k in self._needs(step):
                if k in producers and k not in available:
                    raise ValueError(
                        f"{step.stage.name} (or its guard) requires {k!r} before a critical step produces it"
                    )
            if step.critical and step.when is None:                    # a skipped or non-critical producer may not have run
                available |= step.stage.produces.keys()
        for step in self.finally_steps:
            for k in self._needs(step):
                if k in producers:
                    raise ValueError(
                        f"finally step {step.stage.name} (or its guard) requires produced key {k!r}"
                    )

    def initial_requires(self) -> dict[str, type]:
        """The keys the caller must supply: everything some step (or guard) requires that no step produces."""
        produced = self._produced()
        return {
            k: t
            for step in self.steps + self.finally_steps
            for k, t in self._needs(step).items()
            if k not in produced
        }

    def _check_context(self, ctx: Mapping[str, object]) -> None:
        """Check the caller's context before any side effect: every initial key is present
        with the right type, and none of the keys a step is meant to produce is supplied."""
        supplied_produced = ctx.keys() & self._produced().keys()
        if supplied_produced:
            raise ValueError(f"context supplies keys a step produces: {sorted(supplied_produced)}")
        for k, t in self.initial_requires().items():
            if k not in ctx:
                raise KeyError(f"context is missing {k!r}")
            if not isinstance(ctx[k], t):
                raise TypeError(f"context {k!r} is {type(ctx[k]).__name__}, expected {t.__name__}")

    # --- execution ---

    async def run(self, ctx: Mapping[str, object]) -> None:
        self._check_context(ctx)                      # before any side effect
        data = dict(ctx)
        try:
            for step in self.steps:
                ok = await self._attempt(step, data)
                if not ok and step.critical:
                    last = self.step_log[-1]
                    self.failure = f"{last.name}: {last.error}"
                    break
        finally:
            for step in self.finally_steps:           # success, failure, or cancellation
                await self._attempt(step, data)

    async def _attempt(self, step: PipelineStep, data: dict[str, object]) -> bool:
        if step.when is not None:
            try:
                should_run = step.when.test(data)
            except Exception as e:
                self.step_log.append(StepRecord(
                    name=step.stage.name, ok=False, attempts=0,
                    error=f"guard {step.when.name}: {type(e).__name__}: {e}",
                ))
                return False
            if not should_run:
                self.step_log.append(StepRecord(name=step.stage.name, ok=True, attempts=0, error=None))
                return True

        tries = step.retries + 1
        error: str | None = None
        for attempt in range(1, tries + 1):
            try:
                produced = await step.stage.run(data)
            except asyncio.CancelledError:
                self.step_log.append(
                    StepRecord(name=step.stage.name, ok=False, attempts=attempt, error="cancelled")
                )
                raise
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                continue
            data.update(produced)
            self.step_log.append(
                StepRecord(name=step.stage.name, ok=True, attempts=attempt, error=None)
            )
            return True
        self.step_log.append(
            StepRecord(name=step.stage.name, ok=False, attempts=tries, error=error)
        )
        return False
