"""`clarify(questions=[...])` asks several independent questions in one call (plan item 3.1)."""
import json

from tools.clarify_tool import CLARIFY_SCHEMA, MAX_QUESTIONS, clarify_tool


def test_each_question_is_asked_in_order_and_answered_separately():
    asked = []

    def cb(question, choices):
        asked.append((question, choices))
        return f"answer to {question}"

    out = json.loads(clarify_tool(questions=[
        {"question": "Which DB?", "choices": ["Postgres", "SQLite"]},
        {"question": "Deadline?"},
    ], callback=cb))
    assert asked == [("Which DB?", ["Postgres", "SQLite"]), ("Deadline?", None)]
    assert [a["user_response"] for a in out["answers"]] == ["answer to Which DB?",
                                                            "answer to Deadline?"]
    assert [a["question"] for a in out["answers"]] == ["Which DB?", "Deadline?"]


def test_single_question_form_is_unchanged():
    out = json.loads(clarify_tool("Pick", ["a", "b"], callback=lambda q, c: "a"))
    assert out == {"question": "Pick", "choices_offered": ["a", "b"], "user_response": "a"}


def test_questions_are_capped_and_validated():
    calls = []
    many = [{"question": f"q{i}"} for i in range(MAX_QUESTIONS + 3)]
    out = json.loads(clarify_tool(questions=many, callback=lambda q, c: calls.append(q) or "x"))
    assert len(calls) == MAX_QUESTIONS == len(out["answers"])
    assert "error" in json.loads(clarify_tool(questions=[{"choices": ["a"]}], callback=lambda q, c: "x"))
    assert "error" in json.loads(clarify_tool(callback=lambda q, c: "x"))


def test_schema_offers_questions_without_requiring_question():
    params = CLARIFY_SCHEMA["parameters"]
    assert params["properties"]["questions"]["type"] == "array"
    assert "question" not in params.get("required", [])


def test_both_agent_dispatch_sites_forward_questions():
    import inspect
    from agent import agent_runtime_helpers, tool_executor
    for mod in (agent_runtime_helpers, tool_executor):
        src = inspect.getsource(mod)
        i = src.index("_clarify_tool(")
        assert 'questions=next_args.get("questions")' in src[i:i + 400], mod.__name__
