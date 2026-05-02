from cursor2telegram.telegram_ui import (
    code_block,
    html_escape,
    kb_model_menu,
    kb_modes,
    kb_permission,
    kb_question,
    render_plan,
    render_question,
    render_tool_call,
    split_for_telegram,
)


def test_html_escape():
    assert html_escape("<a>") == "&lt;a&gt;"
    assert html_escape("Tom & Jerry") == "Tom &amp; Jerry"


def test_code_block_with_language():
    out = code_block("print(1)", "python")
    assert "<pre>" in out and "language-python" in out


def test_split_for_telegram_long():
    text = ("line\n" * 2000)
    parts = split_for_telegram(text, max_chars=200)
    assert len(parts) > 1
    assert all(len(p) <= 200 for p in parts)


def test_kb_modes_marks_active():
    kb = kb_modes("plan")
    flat = [b.text for row in kb.inline_keyboard for b in row]
    assert any(b.startswith("● plan") for b in flat)


def test_kb_model_menu_uses_short_tokens():
    kb = kb_model_menu(
        [{"modelId": "gpt-5.5[context=272k,reasoning=medium]", "name": "gpt-5.5"}],
        active_model_id="gpt-5.5[context=272k,reasoning=medium]",
        token_map={"m0": "gpt-5.5[context=272k,reasoning=medium]"},
    )
    button = kb.inline_keyboard[0][0]
    assert button.text.startswith("● gpt-5.5")
    assert button.callback_data == "model:set:m0"


def test_kb_permission_callbacks():
    kb = kb_permission("call-1")
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "perm:once:call-1" in cbs
    assert "perm:always:call-1" in cbs
    assert "perm:reject:call-1" in cbs


def test_kb_question_options():
    kb = kb_question("c", "q1", [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}])
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "q:c:q1:a" in cbs and "q:c:q1:b" in cbs
    assert any(cb.endswith("__skip") for cb in cbs)


def test_render_plan_and_question_and_tool():
    plan = render_plan({"name": "P", "overview": "O", "plan": "do x", "todos": [{"content": "t", "status": "in_progress"}]})
    assert "Todos" in plan and "[~] t" in plan
    q = render_question({"title": "Pick", "questions": [{"prompt": "Which?"}]})
    assert "Pick" in q and "Which?" in q
    tc = render_tool_call("ShellExec", {"cmd": "ls -la"})
    assert "ShellExec" in tc and "ls -la" in tc
