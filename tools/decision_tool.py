#!/usr/bin/env python3
"""decision_evaluate — advisory second-model evaluation (F5 foundation).

Runs one auxiliary-model call carrying ONLY the state and questions the caller
supplies: no conversation history, no files, no memory, no ambient context.
Mirrors the external agent's ``decision_evaluate`` contract v2 (boolean / choice / score)
and its rule that results never authorize actions — this tool is advisory
only. It is not wired into any approval gate, and nothing in Hermes may treat
its output as permission to act.

Configuration (per profile — ``load_config`` reads the active profile home):
top-level ``decisionModel: "provider/model"`` in ``config.yaml``
(e.g. ``"openrouter/google/gemini-3-flash-preview"``). Per-task routing knobs
(timeout, extra_body, reasoning effort) live under ``auxiliary.decision.*``,
the same shape as the other auxiliary tasks; explicit ``decisionModel``
provider/model win over ``auxiliary.decision`` provider/model, per
``_resolve_task_provider_model`` (explicit args > auxiliary.<task>.* > auto).

When ``decisionModel`` is unset the tool is unavailable (its ``check_fn``
answers False, like the external agent returning no tool) and a direct call returns a
structured ``unavailable`` error naming the key to set.
"""

import json
import logging
from typing import Any, Optional

from tools.registry import no_cache_check_fn, registry, tool_error, tool_result

logger = logging.getLogger(__name__)

_MAX_QUESTIONS = 10
_MAX_STATE_CHARS = 65536
_MAX_QUESTION_CHARS = 4096
_DEFAULT_TIMEOUT = 30
_DEFAULT_MAX_TOKENS = 2048

_DECISION_SYSTEM_PROMPT = (
    "You are a decision evaluator. You see ONLY the state and questions in the "
    "user message — nothing else. Answer each question strictly from that "
    "supplied state, and reply with a single JSON object of this shape:\n"
    '{"answers": {"<question id>": {...}, ...}}\n'
    "Per question id, return:\n"
    '- boolean: {"answer": true | false | null, "probability_true": 0.0-1.0 | null, '
    '"reason": "<one sentence>"}\n'
    '- choice: {"choice": "<exactly one of the listed options>" | "abstain", '
    '"confidence": 0.0-1.0 | null, "reason": "<one sentence>"}\n'
    '- score: {"score": 0.0-1.0 | null, "reason": "<one sentence>"}\n'
    "Rules: answer null / \"abstain\" (and probability/score null) whenever the "
    "supplied state does not contain enough evidence to decide — say so in the "
    "reason. Never guess or infer a missing probability, confidence, or score; "
    "a missing number is null, not 0.5. Never add keys, commentary, or text "
    "outside the JSON object."
)

_QUESTION_TYPES = ("boolean", "choice", "score")


def _load_config_dict() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly()
    except Exception:
        logger.debug("decision_evaluate: config read failed", exc_info=True)
        return {}
    return config if isinstance(config, dict) else {}


def resolve_decision_model(config: Optional[dict[str, Any]] = None) -> tuple[Optional[str], Optional[str]]:
    """Parse top-level ``decisionModel`` (``"provider/model"``) into (provider, model).

    The model id may itself contain slashes (e.g. OpenRouter
    ``google/gemini-3-flash-preview``), so only the first slash splits.
    Returns (None, None) when unset, non-string, or unparseable.
    """
    raw = (config if config is not None else _load_config_dict()).get("decisionModel")
    if not isinstance(raw, str):
        return None, None
    raw = raw.strip()
    if "/" not in raw:
        return None, None
    provider, model = raw.split("/", 1)
    provider, model = provider.strip(), model.strip()
    if not provider or not model:
        return None, None
    return provider, model


def _decision_timeout() -> float:
    try:
        timeout = float(
            (_load_config_dict().get("auxiliary") or {}).get("decision", {}).get("timeout", _DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        return float(_DEFAULT_TIMEOUT)
    return timeout if timeout > 0 else float(_DEFAULT_TIMEOUT)


@no_cache_check_fn
def check_decision_requirements() -> bool:
    """Available only when a decision model is configured (the external agent: no tool otherwise)."""
    provider, model = resolve_decision_model()
    return provider is not None and model is not None


def _validate_questions(questions: Any) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Normalize the caller-supplied questions, or return (None, error)."""
    if not isinstance(questions, list) or not questions:
        return [], "questions must be a non-empty list"
    if len(questions) > _MAX_QUESTIONS:
        return [], f"at most {_MAX_QUESTIONS} questions per call"
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, question in enumerate(questions):
        if not isinstance(question, dict):
            return [], f"question {index} must be an object"
        qid = question.get("id")
        if not isinstance(qid, str) or not qid.strip():
            return [], f"question {index} needs a non-empty string id"
        if qid in seen:
            return [], f"duplicate question id {qid!r}"
        seen.add(qid)
        qtype = question.get("type")
        if qtype not in _QUESTION_TYPES:
            return [], f"question {qid!r}: type must be one of {', '.join(_QUESTION_TYPES)}"
        text = question.get("question")
        if not isinstance(text, str) or not text.strip():
            return [], f"question {qid!r} needs a non-empty question"
        if len(text) > _MAX_QUESTION_CHARS:
            return [], f"question {qid!r} exceeds {_MAX_QUESTION_CHARS} characters"
        item: dict[str, Any] = {"id": qid, "type": qtype, "question": text}
        if qtype == "choice":
            options = question.get("options")
            if (not isinstance(options, list) or len(options) < 2
                    or any(not isinstance(opt, str) or not opt.strip() for opt in options)):
                return [], f"question {qid!r}: choice needs options (at least 2 non-empty strings)"
            item["options"] = options
        criteria = question.get("criteria")
        if criteria is not None:
            item["criteria"] = criteria
        normalized.append(item)
    return normalized, None


def _extract_json(text: str) -> Any:
    """Parse the model's reply as JSON, tolerating a surrounding code fence."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped[3:]
        if stripped.lstrip().startswith(("json", "JSON")):
            stripped = stripped.lstrip()[4:]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    try:
        return json.loads(stripped)
    except ValueError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start >= 0 and end > start:
            return json.loads(stripped[start:end + 1])
        raise


def _number_in_range(value: Any) -> Optional[float]:
    """A probability/confidence/score, or None — never inferred, never clamped."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return float(value) if 0.0 <= float(value) <= 1.0 else None


def _reason(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _parse_answers(questions: list[dict[str, Any]], data: Any) -> tuple[dict[str, Any], list[str]]:
    """Validate the model's per-question answers; never infer missing numbers."""
    raw: dict[str, Any] = {}
    if isinstance(data, dict):
        if isinstance(data.get("answers"), dict):
            raw = data["answers"]
        else:
            raw = {key: value for key, value in data.items()
                   if key in {question["id"] for question in questions}}
    answers: dict[str, Any] = {}
    errors: list[str] = []
    for question in questions:
        qid, qtype = question["id"], question["type"]
        entry = raw.get(qid)
        if not isinstance(entry, dict):
            answers[qid] = {"type": qtype, "error": "no answer returned for this question"}
            errors.append(qid)
            continue
        if qtype == "boolean":
            answer = entry.get("answer")
            if answer is not None and not isinstance(answer, bool):
                answers[qid] = {"type": qtype, "error": "boolean answer must be true, false, or null"}
                errors.append(qid)
                continue
            answers[qid] = {
                "type": qtype, "answer": answer,
                "probability_true": _number_in_range(entry.get("probability_true")),
                "abstained": answer is None, "reason": _reason(entry.get("reason"))}
        elif qtype == "choice":
            choice = entry.get("choice")
            if choice is None or choice == "abstain":
                answers[qid] = {
                    "type": qtype, "choice": None,
                    "confidence": _number_in_range(entry.get("confidence")),
                    "abstained": True, "reason": _reason(entry.get("reason"))}
            elif choice in question["options"]:
                answers[qid] = {
                    "type": qtype, "choice": choice,
                    "confidence": _number_in_range(entry.get("confidence")),
                    "abstained": False, "reason": _reason(entry.get("reason"))}
            else:
                answers[qid] = {"type": qtype, "error": "choice is not one of the listed options"}
                errors.append(qid)
        else:  # score
            score = _number_in_range(entry.get("score"))
            answers[qid] = {
                "type": qtype, "score": score,
                "abstained": score is None, "reason": _reason(entry.get("reason"))}
    return answers, errors


def decision_evaluate(state: Any = None, questions: Any = None) -> str:
    """Evaluate only the supplied state with the configured decision model.

    Advisory only: the returned verdicts never authorize any action.
    """
    provider, model = resolve_decision_model()
    if provider is None or model is None:
        return tool_error(
            "decision_evaluate is not configured: set decisionModel: \"provider/model\" "
            "in config.yaml (e.g. decisionModel: \"openrouter/google/gemini-3-flash-preview\")",
            status="unavailable")
    normalized, error = _validate_questions(questions)
    if error is not None:
        return tool_error(error)
    try:
        state_text = json.dumps(state, ensure_ascii=False)
    except (TypeError, ValueError):
        return tool_error("state must be JSON-serializable")
    if len(state_text) > _MAX_STATE_CHARS:
        return tool_error(f"state exceeds {_MAX_STATE_CHARS} characters; supply a smaller excerpt")
    # Only the supplied state and questions are sent — no conversation, files, or memory.
    messages = [
        {"role": "system", "content": _DECISION_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(
            {"state": state, "questions": normalized}, ensure_ascii=False)},
    ]
    try:
        from agent.auxiliary_client import call_llm
        from agent.auxiliary_unavailable import AuxiliaryClientUnavailable
    except Exception as exc:  # pragma: no cover - import failure
        logger.debug("decision_evaluate: auxiliary client import failed", exc_info=True)
        return tool_error(f"decision model client unavailable: {exc}", status="unavailable")
    try:
        resp = call_llm(
            task="decision", provider=provider, model=model, messages=messages,
            temperature=0, max_tokens=_DEFAULT_MAX_TOKENS, timeout=_decision_timeout())
    except AuxiliaryClientUnavailable as exc:
        return tool_error(f"decision model unavailable: {exc}", status="unavailable")
    except Exception as exc:
        logger.info("decision_evaluate call failed: %s", exc, exc_info=True)
        return tool_error(f"decision model call failed: {type(exc).__name__}: {exc}",
                          status="unavailable")
    try:
        text = resp.choices[0].message.content or ""
    except Exception:
        logger.debug("decision_evaluate: unreadable model response", exc_info=True)
        text = ""
    if not text.strip():
        return tool_error("decision model returned no text", status="unavailable")
    try:
        data = _extract_json(text)
    except ValueError:
        return tool_error("decision model returned unparseable output", status="unavailable")
    answers, errors = _parse_answers(normalized, data)
    logger.info("decision_evaluate: model=%s/%s questions=%s errors=%s",
                provider, model, [question["id"] for question in normalized], errors)
    result: dict[str, Any] = {
        "status": "ok", "model": f"{provider}/{model}",
        "advisory_only": True, "answers": answers}
    if errors:
        result["errors"] = errors
    return tool_result(result)


DECISION_EVALUATE_SCHEMA = {
    "name": "decision_evaluate",
    "description": (
        "Evaluate ONLY the supplied state with a separately configured decision model. "
        "Ask boolean (explicit answer and/or probability_true), choice (competing "
        "alternatives), or score (0-1 rubric position) questions. The decision model "
        "sees nothing but the state and questions you pass — no conversation, files, "
        "or memory. It abstains where the evidence is missing, and a missing "
        "probability is never inferred. Advisory only: results never authorize "
        "actions. Requires decisionModel in config.yaml."),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["state", "questions"],
        "properties": {
            "state": {
                "description": (
                    "The only evidence the decision model sees: a claim, a JSON object, "
                    "or a list. Keep it to the evidence the questions need.")},
            "questions": {
                "type": "array",
                "minItems": 1,
                "maxItems": _MAX_QUESTIONS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["id", "type", "question"],
                    "properties": {
                        "id": {"type": "string",
                               "description": "Stable id for this question, echoed in the answers."},
                        "type": {"type": "string", "enum": ["boolean", "choice", "score"],
                                 "description": "boolean: true/false + probability_true. "
                                 "choice: pick one listed option. score: 0-1 rubric position."},
                        "question": {"type": "string",
                                     "description": "The question, judged only against state."},
                        "options": {"type": "array", "minItems": 2,
                                    "items": {"type": "string"},
                                    "description": "Required for choice: the competing alternatives."},
                        "criteria": {
                            "description": (
                                "Optional judging criteria: for boolean, {\"true\": ..., \"false\": ...}; "
                                "for choice, one entry per option; for score, an ordered rubric list.")},
                    },
                },
            },
        },
    },
}


registry.register(
    name="decision_evaluate",
    toolset="decision",
    schema=DECISION_EVALUATE_SCHEMA,
    handler=lambda args, **kw: decision_evaluate(
        state=args.get("state"), questions=args.get("questions")),
    check_fn=check_decision_requirements,
    emoji="⚖️")
