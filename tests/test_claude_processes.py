"""Tests for the Claude process listing (meridian_desktop/claude_processes.py). Fixed listing text, fake runners only.

No process is started: the default runner is replaced by a fake that records its arguments, so these tests check the
exact command without running it. The parser is checked with fixed tasklist list-form text. A listing that fails, times
out or prints nothing readable gives the unavailable state with fixed text, and the raw error never reaches a result.
"""

import subprocess

import pytest

from meridian_desktop import claude_processes as cp
from meridian_desktop.claude_processes import ClaudeProcess

SECRET_PATH = r"C:\Users\13144\AppData\Roaming\Claude\secret-prompt-text"
SECRET_TOKEN = "sk_meridian_PROCESSSENTINEL0123456789"

# Fixed tasklist /FO LIST text, in the form Windows prints it: one block of labelled lines per process, blocks
# separated by a blank line, and CRLF line endings.
LISTING = (
    "Image Name:   System Idle Process\r\n"
    "PID:          0\r\n"
    "Session Name: Services\r\n"
    "Session#:     0\r\n"
    "Mem Usage:    8 K\r\n"
    "\r\n"
    "Image Name:   Claude.exe\r\n"
    "PID:          14232\r\n"
    "Session Name: Console\r\n"
    "Session#:     1\r\n"
    "Mem Usage:    245,108 K\r\n"
    "\r\n"
    "Image Name:   claude.exe\r\n"
    "PID:          900\r\n"
    "Session Name: Console\r\n"
    "Session#:     1\r\n"
    "Mem Usage:    31,004 K\r\n"
    "\r\n"
    "Image Name:   node.exe\r\n"
    "PID:          7744\r\n"
    "Session Name: Console\r\n"
    "Session#:     1\r\n"
    "Mem Usage:    60,000 K\r\n"
    "\r\n"
    "Image Name:   notepad.exe\r\n"
    "PID:          5000\r\n"
    "Session Name: Console\r\n"
    "Session#:     1\r\n"
    "Mem Usage:    9,000 K\r\n"
)

# A matching record whose later fields hold a path and token-like text. Only the image name and the id are kept.
LISTING_WITH_EXTRA_FIELDS = (
    "Image Name:   claude.exe\r\n"
    "PID:          4321\r\n"
    f"Session Name: {SECRET_PATH}\r\n"
    f"Session#:     {SECRET_TOKEN}\r\n"
    "Mem Usage:    1 K\r\n"
)

# The same shape with German labels. The parser reads the values by position, so the labels do not matter.
LISTING_LOCALISED = (
    "Abbildname:   claude.exe\n"
    "PID:          777\n"
    "Sitzungsname: Console\n"
    "Sitz.-Nr.:    1\n"
    "Speichernutzung: 20.000 K\n"
)


def _names(processes):
    return [(process.image, process.pid) for process in processes]


# ----------------------------------------------------------------- parser with fixed listing text


def test_parser_keeps_only_claude_programs_by_image_name_and_id():
    processes = cp.parse_tasklist(LISTING)
    assert processes == (
        ClaudeProcess(image="claude.exe", pid=900),
        ClaudeProcess(image="Claude.exe", pid=14232),
    )


def test_parser_matches_the_image_name_without_regard_to_case():
    processes = cp.parse_tasklist("Image Name: CLAUDE.EXE\nPID: 12\n")
    assert _names(processes) == [("CLAUDE.EXE", 12)]


def test_parser_does_not_match_node_or_other_programs_named_like_claude():
    text = (
        "Image Name:   node.exe\nPID: 1\n\n"
        "Image Name:   claude-helper.exe\nPID: 2\n\n"
        "Image Name:   claude\nPID: 3\n\n"
        "Image Name:   notclaude.exe\nPID: 4\n"
    )
    assert cp.parse_tasklist(text) == ()


def test_parser_reads_a_listing_with_no_claude_program_as_an_empty_list():
    text = "Image Name:   notepad.exe\nPID: 5000\n\nImage Name:   System\nPID: 4\n"
    assert cp.parse_tasklist(text) == ()


def test_parser_keeps_nothing_but_the_image_name_and_the_id():
    processes = cp.parse_tasklist(LISTING_WITH_EXTRA_FIELDS)
    assert processes == (ClaudeProcess(image="claude.exe", pid=4321),)
    shown = repr(processes)
    assert SECRET_PATH not in shown
    assert SECRET_TOKEN not in shown
    assert "C:" not in shown


def test_parser_reads_localised_labels_by_position():
    assert cp.parse_tasklist(LISTING_LOCALISED) == (ClaudeProcess(image="claude.exe", pid=777),)


def test_parser_skips_a_record_whose_id_or_name_does_not_look_right():
    text = (
        "Image Name:   claude.exe\nPID:          not-a-number\n\n"
        "Image Name:   Claude.exe\nPID:          99\n\n"
        "Image Name:   claude.exe;rm -rf\nPID:  5\n"
    )
    assert cp.parse_tasklist(text) == (ClaudeProcess(image="Claude.exe", pid=99),)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   \n\n",
        "INFO: No tasks are running which match the specified criteria.\n",
        "ERROR: Access is denied.\n",
        "Image Name only, no id\n",
        "Image Name:   claude.exe\nPID:  x\n",
    ],
)
def test_parser_gives_none_when_no_record_can_be_read(text):
    assert cp.parse_tasklist(text) is None


@pytest.mark.parametrize("value", [None, b"Image Name: claude.exe\nPID: 1\n", 42])
def test_parser_gives_none_for_text_that_is_not_text(value):
    assert cp.parse_tasklist(value) is None


def test_processes_are_sorted_by_image_name_then_id():
    text = (
        "Image Name: Claude.exe\nPID: 30\n\n"
        "Image Name: claude.exe\nPID: 20\n\n"
        "Image Name: claude.exe\nPID: 10\n"
    )
    assert _names(cp.parse_tasklist(text)) == [("claude.exe", 10), ("claude.exe", 20), ("Claude.exe", 30)]


# ----------------------------------------------------------------- the listing with a fake runner


def test_listing_with_a_fake_runner_returns_the_matching_processes():
    result = cp.list_claude_processes(run=lambda: LISTING)
    assert result.state == cp.LISTED
    assert result.message == ""
    assert _names(result.processes) == [("claude.exe", 900), ("Claude.exe", 14232)]


def test_listing_with_no_claude_program_is_listed_and_empty():
    result = cp.list_claude_processes(run=lambda: "Image Name: notepad.exe\nPID: 5000\n")
    assert result.state == cp.LISTED
    assert result.processes == ()


def _raises(error):
    def run():
        raise error

    return run


@pytest.mark.parametrize(
    "error",
    [
        OSError(f"tasklist failed near {SECRET_PATH}"),
        FileNotFoundError(f"no such file {SECRET_PATH}"),
        RuntimeError(f"surprise {SECRET_TOKEN}"),
    ],
)
def test_a_failed_listing_is_unavailable_with_fixed_text(error):
    result = cp.list_claude_processes(run=_raises(error))
    assert result.state == cp.UNAVAILABLE
    assert result.message == cp.UNAVAILABLE_MESSAGE
    assert result.processes == ()
    shown = repr(result)
    assert SECRET_PATH not in shown
    assert SECRET_TOKEN not in shown


def test_a_timed_out_listing_is_unavailable_with_its_own_fixed_text():
    result = cp.list_claude_processes(run=_raises(subprocess.TimeoutExpired(cmd="tasklist", timeout=5.0)))
    assert result.state == cp.UNAVAILABLE
    assert result.message == cp.TIMEOUT_MESSAGE


@pytest.mark.parametrize("text", ["", "ERROR: nothing readable\n", None])
def test_unreadable_listing_text_is_unavailable(text):
    result = cp.list_claude_processes(run=lambda: text)
    assert result.state == cp.UNAVAILABLE
    assert result.message == cp.UNAVAILABLE_MESSAGE


def test_the_messages_are_one_plain_sentence_each():
    for message in (cp.UNAVAILABLE_MESSAGE, cp.TIMEOUT_MESSAGE):
        assert message.endswith(".")
        assert "\n" not in message
        assert "Exception" not in message
        assert "Traceback" not in message


# ----------------------------------------------------------------- the default runner, faked at subprocess.run


def test_default_runner_runs_tasklist_in_list_form_with_no_shell_and_a_short_timeout(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout=LISTING, stderr="")

    monkeypatch.setattr(cp.subprocess, "run", fake_run)
    result = cp.list_claude_processes()

    assert result.state == cp.LISTED
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert isinstance(args, list)
    assert args[0].lower().endswith("tasklist.exe")
    assert args[1:] == ["/FO", "LIST"]
    assert kwargs.get("shell", False) is False
    assert kwargs["timeout"] == cp.PROCESS_TIMEOUT == 5.0
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["capture_output"] is True


def test_default_runner_nonzero_exit_is_unavailable(monkeypatch):
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, stdout=LISTING, stderr=SECRET_PATH)

    monkeypatch.setattr(cp.subprocess, "run", fake_run)
    result = cp.list_claude_processes()
    assert result.state == cp.UNAVAILABLE
    assert result.message == cp.UNAVAILABLE_MESSAGE
    assert SECRET_PATH not in repr(result)


def test_default_runner_timeout_is_unavailable_with_the_timeout_text(monkeypatch):
    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs["timeout"])

    monkeypatch.setattr(cp.subprocess, "run", fake_run)
    result = cp.list_claude_processes()
    assert result.state == cp.UNAVAILABLE
    assert result.message == cp.TIMEOUT_MESSAGE


def test_default_runner_missing_program_is_unavailable(monkeypatch):
    def fake_run(args, **kwargs):
        raise FileNotFoundError(args[0])

    monkeypatch.setattr(cp.subprocess, "run", fake_run)
    result = cp.list_claude_processes()
    assert result.state == cp.UNAVAILABLE
    assert result.message == cp.UNAVAILABLE_MESSAGE
