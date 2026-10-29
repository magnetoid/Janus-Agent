#!/usr/bin/env python3
"""
Clarify Tool Module - Interactive Clarifying Questions

Allows the agent to present structured multiple-choice questions or open-ended
prompts to the user. In CLI mode, choices are navigable with arrow keys. On
messaging platforms, choices are rendered as a numbered list.

The actual user-interaction logic lives in the platform layer (cli.py for CLI,
gateway/run.py for messaging). This module defines the schema, validation, and
a thin dispatcher that delegates to a platform-provided callback.
"""

import json
from typing import Any, Callable, Dict, List, Optional, Tuple


# Maximum number of predefined choices the agent can offer.
# A 5th "Other (type your answer)" option is always appended by the UI.
MAX_CHOICES = 4
MAX_QUESTIONS = 4


def _clean_choices(choices: Any) -> Tuple[Optional[List[str]], Optional[str]]:
    if choices is None:
        return None, None
    if not isinstance(choices, list):
        return None, "choices must be a list of strings."
    cleaned = [str(c).strip() for c in choices if str(c).strip()][:MAX_CHOICES]
    return (cleaned or None), None


def _ask_many(questions: Any, callback: Optional[Callable]) -> str:
    """Ask up to MAX_QUESTIONS independent questions, one after another."""
    if not isinstance(questions, list) or not questions:
        return tool_error("questions must be a non-empty list of {question, choices?} objects.")
    items = []
    for i, q in enumerate(questions[:MAX_QUESTIONS]):
        if not isinstance(q, dict) or not str(q.get("question") or "").strip():
            return tool_error(f"questions[{i}] needs a non-empty 'question'.")
        choices, err = _clean_choices(q.get("choices"))
        if err:
            return tool_error(f"questions[{i}]: {err}")
        items.append((str(q["question"]).strip(), choices))
    if callback is None:
        return json.dumps({"error": "Clarify tool is not available in this execution context."},
                          ensure_ascii=False)
    answers = []
    for question, choices in items:
        try:
            response = callback(question, choices)
        except Exception as exc:
            return json.dumps({"error": f"Failed to get user input: {exc}",
                               "answers": answers}, ensure_ascii=False)
        answers.append({"question": question, "choices_offered": choices,
                        "user_response": str(response).strip()})
    return json.dumps({"answers": answers}, ensure_ascii=False)


def clarify_tool(
    question: Optional[str] = None,
    choices: Optional[List[str]] = None,
    callback: Optional[Callable] = None,
    questions: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """
    Ask the user a question, optionally with multiple-choice options.

    Args:
        question: The question text to present.
        choices:  Up to 4 predefined answer choices. When omitted the
                  question is purely open-ended.
        callback: Platform-provided function that handles the actual UI
                  interaction. Signature: callback(question, choices) -> str.
                  Injected by the agent runner (cli.py / gateway).
        questions: Several independent ``{question, choices?}`` items (up to
                  MAX_QUESTIONS), asked one after another through the same
                  callback. Returns ``{"answers": [...]}``. Takes precedence
                  over ``question`` / ``choices``.

    Returns:
        JSON string with the user's response.
    """
    if questions is not None:
        return _ask_many(questions, callback)
    if not question or not question.strip():
        return tool_error("Question text is required.")

    question = question.strip()

    # Validate and trim choices
    if choices is not None:
        if not isinstance(choices, list):
            return tool_error("choices must be a list of strings.")
        choices = [str(c).strip() for c in choices if str(c).strip()]
        if len(choices) > MAX_CHOICES:
            choices = choices[:MAX_CHOICES]
        if not choices:
            choices = None  # empty list → open-ended

    if callback is None:
        return json.dumps(
            {"error": "Clarify tool is not available in this execution context."},
            ensure_ascii=False,
        )

    try:
        user_response = callback(question, choices)
    except Exception as exc:
        return json.dumps(
            {"error": f"Failed to get user input: {exc}"},
            ensure_ascii=False,
        )

    return json.dumps({
        "question": question,
        "choices_offered": choices,
        "user_response": str(user_response).strip(),
    }, ensure_ascii=False)


def check_clarify_requirements() -> bool:
    """Clarify tool has no external requirements -- always available."""
    return True


# =============================================================================
# OpenAI Function-Calling Schema
# =============================================================================

CLARIFY_SCHEMA = {
    "name": "clarify",
    "description": (
        "Ask the user a question when you need clarification, feedback, or a "
        "decision before proceeding. Supports two modes:\n\n"
        "1. **Multiple choice** — provide up to 4 choices. The user picks one "
        "or types their own answer via a 5th 'Other' option.\n"
        "2. **Open-ended** — omit choices entirely. The user types a free-form "
        "response.\n\n"
        "Use this tool when:\n"
        "- The task is ambiguous and you need the user to choose an approach\n"
        "- You want post-task feedback ('How did that work out?')\n"
        "- You want to offer to save a skill or update memory\n"
        "- A decision has meaningful trade-offs the user should weigh in on\n\n"
        "Do NOT use this tool for simple yes/no confirmation of dangerous "
        "commands (the terminal tool handles that). Prefer making a reasonable "
        "default choice yourself when the decision is low-stakes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question to present to the user (single-question form).",
            },
            "questions": {
                "type": "array",
                "maxItems": MAX_QUESTIONS,
                "description": (
                    f"Up to {MAX_QUESTIONS} independent questions asked one after another, "
                    "each {question, choices?}. Use instead of question/choices when you "
                    "need several unrelated answers at once; the result is {answers: [...]}."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "choices": {"type": "array", "items": {"type": "string"},
                                    "maxItems": MAX_CHOICES},
                    },
                    "required": ["question"],
                },
            },
            "choices": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": MAX_CHOICES,
                "description": (
                    "Up to 4 answer choices. Omit this parameter entirely to "
                    "ask an open-ended question. When provided, the UI "
                    "automatically appends an 'Other (type your answer)' option."
                ),
            },
        },
        "required": [],
    },
}


# --- Registry ---
from tools.registry import registry, tool_error

registry.register(
    name="clarify",
    toolset="clarify",
    schema=CLARIFY_SCHEMA,
    handler=lambda args, **kw: clarify_tool(
        question=args.get("question", ""),
        choices=args.get("choices"),
        callback=kw.get("callback"),
        questions=args.get("questions")),
    check_fn=check_clarify_requirements,
    emoji="❓",
)
