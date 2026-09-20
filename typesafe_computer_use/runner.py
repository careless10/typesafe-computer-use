"""The step loop and the run folder."""

from __future__ import annotations

import contextlib
import json
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace

import anthropic
from typesafe_sdk import TypeSafeClient

from . import config as cfg_module
from . import learning, macos
from .actions import Context, is_noop, perform
from .config import DEFAULT_DELAY, DEFAULT_MIN_CONFIDENCE, DEFAULT_STEPS, MAX_OPTIONS
from .decide import LAST_USAGE as decide_usage
from .decide import Decision, decide, offscreen_records
from .models import Abort, Item, Screen
from .perception import OcrCache, capture, perceive
from .report import Log, annotate, ax_count, render_payload, top
from .timing import format_timing, phase, summarize
from .writer import Answer, compose_answer

MAX_CONSECUTIVE_NOOPS = 2

# The outcomes that end with an answer, each in words the writer can pass on. A dry run took no
# action and an abort is the user's own stop, so neither has anything to report.
STOPPED = {
    "done": "the classifier judged the goal already achieved on this screen",
    "nothing helps": "the classifier found nothing on this screen that helps with the goal",
    "low confidence": "the classifier was not confident enough in any next action",
    "stalled": "the last actions changed nothing",
    "step limit": "the run used every step it was allowed",
}


@dataclass
class RunConfig:
    goal: str
    out: Path
    act: bool = False
    steps: int = DEFAULT_STEPS
    min_confidence: float = DEFAULT_MIN_CONFIDENCE
    delay: float = DEFAULT_DELAY
    image: Path | None = None  # replay a saved capture (never acts)
    app: str | None = None  # frontmost app to report during replay
    url: str | None = None  # browser URL to report during replay
    context: list[str] = field(default_factory=list)  # what happened before this run started
    answer: bool = True  # the closing summary; a vision call worth several seconds
    note: str = ""  # something true about the goal itself, e.g. that speech produced it

    @property
    def replay(self) -> bool:
        return self.image is not None


@dataclass
class RunState:
    history: list[str] = field(default_factory=list)
    timings: list[dict[str, float]] = field(default_factory=list)
    consecutive_noops: int = 0
    last_url: str | None = None
    outcome: str = "crashed"  # every way out of the loop names its own; only an exception leaves this
    ocr_cache: OcrCache = field(default_factory=OcrCache)  # carries one step's OCR into the next
    view: tuple[Screen, list[Item]] | None = None  # the latest capture, until an action makes it stale
    answer: Answer | None = None


def run(cfg: RunConfig, ctx_factory, client: TypeSafeClient | None = None, ocr_cache: OcrCache | None = None) -> RunState:
    """Drive the loop. ctx_factory(typesafe, history) builds the action Context.

    A caller that runs many goals in one process can pass its own client and OCR cache.
    That keeps the TLS connection open — worth about 0.7s on the first decision of every
    run — and carries the cached screen regions across commands, so the first step no
    longer has to read the whole display.
    """
    cfg.out.mkdir(parents=True, exist_ok=True)
    log = Log(cfg.out / "run.log")
    log(f"run folder: {cfg.out}")
    if cfg.act:
        log("driving the machine. abort: Ctrl-C, or slam the mouse into the top-left corner.")

    state = RunState()
    if ocr_cache is not None:
        state.ocr_cache = ocr_cache
    # Each spoken command is its own process, so without this the model sees an empty
    # history and cannot resolve a follow-up like "go to the main page".
    state.history.extend(cfg.context)
    started = time.time()
    try:
        with contextlib.ExitStack() as stack:
            typesafe = client if client is not None else stack.enter_context(TypeSafeClient())
            ctx = ctx_factory(typesafe, state.history)
            for step in range(1, cfg.steps + 1):
                if not run_step(cfg, ctx, state, step, log):
                    break
            else:
                log(f"\nstopped after {cfg.steps} steps")
                state.outcome = "step limit"
            conclude(cfg, ctx, state, log)
    except (KeyboardInterrupt, Abort) as e:
        state.outcome = f"aborted ({e or 'Ctrl-C'})"
        log(f"\n{state.outcome} after {len(state.history)} actions")
    finally:
        # Learn only from runs that got somewhere: a wrong turn should not be
        # remembered as if it were the right one.
        if cfg.act and state.outcome == "done" and (state.answer is None or state.answer.achieved):
            landed = macos.browser_url(cfg_module.browser())
            if landed:
                learning.remember(cfg.goal, landed, state.view[0].app if state.view else "")
        summary = {
            "goal": cfg.goal,
            "act": cfg.act,
            "steps_taken": len(state.history),
            "outcome": state.outcome,
            "answer": state.answer.text if state.answer else None,
            "goal_achieved": state.answer.achieved if state.answer else None,
            "seconds": round(time.time() - started, 1),
            "timing": summarize(state.timings),
            "history": state.history,
            "config": {k: str(v) for k, v in asdict(cfg).items()},
        }
        (cfg.out / "run.json").write_text(json.dumps(summary, indent=2))
        log(f"run folder: {cfg.out}")
    return state


def conclude(cfg: RunConfig, ctx: Context, state: RunState, log: Log) -> None:
    """Hand the screen the run ended on to the writer, for the answer the classifier cannot put into words.

    The last step's capture serves when nothing acted after it. An action makes it stale, so the
    screen is captured again, and saved so the answer can be checked against what it was read from.
    """
    stopped = STOPPED.get(state.outcome)
    if stopped is None:
        return
    if not cfg.answer:
        return
    if ctx.writer is None:
        log("\nno answer: the writer is disabled (set ANTHROPIC_API_KEY)")
        return
    started = time.perf_counter()
    if state.view is None:
        macos.check_abort()
        screen = capture(cfg.image, cfg.app, cfg.url, ctx.browser)
        screen.image.save(cfg.out / "answer-raw.png")
        state.view = (screen, perceive(screen, MAX_OPTIONS, cfg.goal))
    screen, items = state.view
    try:
        state.answer = compose_answer(ctx.writer, cfg.goal, screen, items, state.history, stopped)
    except anthropic.APIError as e:
        log(f"\nno answer: the writer failed ({e})")
        return
    verdict = "goal achieved" if state.answer.achieved else "goal not achieved"
    log(f"\nanswer ({verdict}, {time.perf_counter() - started:.1f}s):\n  {state.answer.text}")


def run_step(cfg: RunConfig, ctx: Context, state: RunState, step: int, log: Log) -> bool:
    macos.check_abort()
    timing: dict[str, float] = {}
    started = time.perf_counter()
    with phase(timing, "capture"):
        screen = capture(cfg.image, cfg.app, cfg.url, ctx.browser, timing)
    items = perceive(screen, MAX_OPTIONS, cfg.goal, timing, None if cfg.replay else state.ocr_cache)
    state.view = (screen, items)
    prefix = cfg.out / f"step-{step:03d}"  # three digits, so a run of 100 steps still lists in order
    screen.image.save(prefix.with_name(prefix.name + "-raw.png"))
    prefix.with_name(prefix.name + "-payload.txt").write_text(
        render_payload(cfg.goal, screen, items, state.history, ctx.browser, ctx.email)
    )

    with phase(timing, "decide"):
        decision = decide(ctx.typesafe, cfg.goal, screen, items, state.history, ctx.browser, ctx.email, cfg.note)
    tokens = decide_usage["input_tokens"]
    by_index = {str(it.index): it for it in items}
    annotate(screen, items, decision.chosen, prefix.with_suffix(".png"))

    field_desc = f" field={screen.field.role}:{screen.field.label!r}" if screen.field else ""
    log(
        f"\nstep {step}: app={screen.app!r}{field_desc} url={screen.url!r} items={len(items)} ax={ax_count(items)} "
        f"offscreen={len(screen.offscreen)} kind={decision.kind.choice} ({decision.kind.confidence:.2f}) "
        f"site={decision.site.choice}"
    )
    for key, p in top(decision.kind, 4):
        log(f"  {p:5.2f}  {key}")
    if decision.item is not None:
        log(f"  item ({decision.item.confidence:.2f}):")
        for key, p in top(decision.item, 4):
            log(f"  {p:5.2f}  [{key}] {by_index[key].text!r}")
    if decision.offscreen is not None:
        log(f"  offscreen ({decision.offscreen.confidence:.2f}):")
        for key, p in top(decision.offscreen, 3):
            log(f"  {p:5.2f}  [{key}] {screen.offscreen[int(key)].label!r}")

    keep_going = resolve(cfg, ctx, state, screen, items, decision, timing, log)
    timing.setdefault("act", 0.0)
    timing["total"] = round(time.perf_counter() - started, 3)
    state.timings.append(timing)

    prefix.with_name(prefix.name + "-answers.json").write_text(json.dumps(answers(decision, screen, items, timing), indent=2))
    log(f"  files: {prefix.name}-raw.png, {prefix.name}.png, {prefix.name}-payload.txt, {prefix.name}-answers.json")
    log(format_timing(timing) + (f"  tokens {tokens}" if tokens else ""))

    if state.view is None:  # an action ran: let the screen settle before the next step, or the answer, reads it
        macos.sleep_watching(cfg.delay)
    return keep_going


def resolve(
    cfg: RunConfig,
    ctx: Context,
    state: RunState,
    screen: Screen,
    items: list[Item],
    decision: Decision,
    timing: dict[str, float],
    log: Log,
) -> bool:
    """Apply the stop rules, then the action. True to keep looping."""
    if decision.stops:
        log(f"  model says {decision.kind.choice!r}; stopping")
        state.outcome = "done" if decision.kind.choice == "done" else "nothing helps"
        return False
    if decision.confidence < cfg.min_confidence:
        log(f"  confidence {decision.confidence:.2f} below {cfg.min_confidence}; stopping")
        state.outcome = "low confidence"
        return False
    if not cfg.act or cfg.replay:
        log(f"  would do: {decision.chosen}. dry run (pass --act without --image to drive the machine)")
        state.outcome = "dry run"
        return False

    with phase(timing, "act"):
        what = perform(decision, screen, items, ctx)
        # The questions are answered independently and in parallel, so they can
        # disagree: "open an app" with no app named, "use the browser" with no site.
        # That refusal is not a wrong judgement about the screen, it is an internally
        # inconsistent plan, so try the next most likely action rather than burn a step.
        if is_noop(what) and "refused: no" in what:
            second = runner_up(decision)
            if second:
                log(f"  {what}; falling back to {second!r}")
                # The answer objects come from the SDK and are not dataclasses, so the
                # substitute kind is a plain stand-in carrying the same fields.
                instead = SimpleNamespace(
                    choice=second,
                    confidence=decision.kind.confidence,
                    probabilities=decision.kind.probabilities,
                )
                what = perform(replace(decision, kind=instead), screen, items, ctx)
    state.view = None
    repeated = bool(state.history) and state.history[-1] == what and screen.url == state.last_url
    state.last_url = screen.url
    state.history.append(what)
    log(f"  did: {what}")
    if is_noop(what) or repeated:
        state.consecutive_noops += 1
        if state.consecutive_noops >= MAX_CONSECUTIVE_NOOPS:
            log(f"  {MAX_CONSECUTIVE_NOOPS} consecutive no-ops; stopping")
            state.outcome = "stalled"
            return False
    else:
        state.consecutive_noops = 0
    return True


# What a refused action may fall back to. Deliberately tiny, and deliberately does not
# include anything that writes: a fallback ran once with "open notion", chose type_text,
# and the writer — seeing a terminal in front of it — composed `open -a Notion`, which
# the next step submitted. A recovery must never be able to author a command.
SAFE_FALLBACKS = ("use_browser", "click_item", "scroll_down", "scroll_up")


def runner_up(decision: Decision) -> str:
    """A safe alternative when the chosen action turned out to be impossible.

    Only reached when an action's parameter question named nothing, which is an
    internally inconsistent plan rather than a misread screen. There is no probability
    floor — a confident wrong plan leaves its alternatives almost nothing, which is
    exactly when recovery is needed — but the alternative must be one that cannot type,
    press keys, quit an app or submit a form.
    """
    if decision.kind.choice == "open_app" and getattr(decision, "site", None) is not None:
        site = getattr(decision.site, "choice", "none")
        if site not in ("none", "other"):
            return "use_browser"  # it named where to go; go there in the browser
    ranked = sorted(decision.kind.probabilities.items(), key=lambda kv: kv[1], reverse=True)
    for name, _probability in ranked[1:]:
        if name in SAFE_FALLBACKS and name != decision.kind.choice:
            return name
    return ""


def answers(decision: Decision, screen: Screen, items: list[Item], timing: dict[str, float]) -> dict:
    """What the classifier returned for this step, plus what it cost."""
    return {
        "kind": decision.kind.choice,
        "kind_confidence": decision.kind.confidence,
        "kind_probabilities": decision.kind.probabilities,
        "item": decision.item.choice if decision.item else None,
        "item_confidence": decision.item.confidence if decision.item else None,
        "item_probabilities": decision.item.probabilities if decision.item else None,
        "site": decision.site.choice,
        "site_probabilities": decision.site.probabilities,
        "offscreen": decision.offscreen.choice if decision.offscreen else None,
        "offscreen_probabilities": decision.offscreen.probabilities if decision.offscreen else None,
        "offscreen_controls": offscreen_records(screen.offscreen),
        "chosen": decision.chosen,
        "confidence": decision.confidence,
        "timing": timing,
        "items": [asdict(it) for it in items],
        "field": screen.field.record() if screen.field else None,
        "app": screen.app,
        "url": screen.url,
    }
