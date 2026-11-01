"""The red-team script's verdict logic (the live run itself needs API quota)."""

from scripts.red_team import CASES, judge


def out(decision, pages=(), error_kind=""):
    return {
        "decision": decision,
        "citations": [{"pages": [p], "section": "s"} for p in pages],
        "error_kind": error_kind,
        "error": "detail",
    }


def test_refusal_cases():
    assert judge(out("not_in_document"), "refuse", [], [])[0] == "pass"
    assert judge(out("answer", [22]), "refuse", [], [])[0] == "FAIL"


def test_answer_cases_need_an_expected_page_and_no_forbidden_page():
    assert judge(out("answer", [80]), "answer", [80], [3])[0] == "pass"
    assert (
        judge(out("answer", [80, 3]), "answer", [80], [3])[0] == "FAIL"
    )  # cited the false premise
    assert judge(out("answer", [41]), "answer", [80], [])[0] == "FAIL"
    assert judge(out("not_in_document"), "answer", [80], [])[0] == "FAIL"


def test_quota_exhaustion_is_not_counted_as_a_failure():
    assert judge(out("error", error_kind="rate_limit"), "answer", [80], [])[0] == "not run"
    assert judge(out("error", error_kind="unavailable"), "answer", [80], [])[0] == "error"


def test_cases_are_well_formed():
    ids = [c[0] for c in CASES]
    assert len(ids) == len(set(ids))
    for _id, _kind, prompt, expected, pages, _forbidden in CASES:
        assert prompt and expected in {"refuse", "answer"}
        assert (expected == "answer") == bool(pages)
