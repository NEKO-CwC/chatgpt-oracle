from chatgpt_oracle.browser import classify_page_text, is_transient_assistant_text


def test_running_research_is_not_complete() -> None:
    state, action = classify_page_text("Searching 4 sources", has_new_output=True, active_hint=True)
    assert state == "RUNNING"
    assert action is None


def test_captcha_requires_manual_takeover() -> None:
    state, action = classify_page_text("Verify you are human", has_new_output=False)
    assert state == "NEEDS_USER_INPUT"
    assert action


def test_output_without_progress_is_complete() -> None:
    state, action = classify_page_text("Final answer with citations", has_new_output=True)
    assert state == "COMPLETE"
    assert action is None


def test_pro_thinking_placeholder_is_transient() -> None:
    assert is_transient_assistant_text("Pro 思考中") is True
    assert is_transient_assistant_text("Pro summary of the file") is False


def test_answer_mentioning_captcha_is_not_a_challenge() -> None:
    state, action = classify_page_text(
        "This service does not automate CAPTCHA or login.",
        has_new_output=True,
        active_hint=False,
    )
    assert state == "COMPLETE"
    assert action is None
