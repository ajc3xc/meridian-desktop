"""Tests for tools/check_boundary.py: a violation tree fails, a clean tree passes,
and the real repo is checked. The real-repo test passes on this repository and
fails only if a file here breaks the Qt or meridian-latex boundary rules."""

import importlib.util
from functools import partial
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("check_boundary", REPO / "tools" / "check_boundary.py")
check_boundary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_boundary)

LATEX_REF = check_boundary.FORBIDDEN_REFS[0]
LATEX_REF_ESCAPED = check_boundary.FORBIDDEN_REFS[2]  # doubled backslash, as in a Python string


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _clean_tree(root: Path) -> None:
    _write(root, "meridian_desktop/__init__.py", '__version__ = "0.1.0"\n')
    _write(root, "meridian_desktop/settings_store.py", "import json\n")
    _write(root, "meridian_desktop/ui/__init__.py", "")
    _write(root, "meridian_desktop/ui/main_window.py", "from PySide6.QtWidgets import QLabel\n")
    _write(root, "meridian_desktop/tray.py", "from PySide6.QtGui import QIcon\n")
    _write(root, "README.md", "# clean\n")


def test_clean_tree_passes(tmp_path):
    _clean_tree(tmp_path)
    assert check_boundary.check(tmp_path) == []


def test_qt_import_outside_ui_fails(tmp_path):
    _clean_tree(tmp_path)
    _write(tmp_path, "meridian_desktop/logic.py", "import PySide6.QtCore\n")
    problems = check_boundary.check(tmp_path)
    assert len(problems) == 1
    assert "logic.py" in problems[0]
    assert "PySide6.QtCore" in problems[0]


def test_from_qt_import_outside_ui_fails(tmp_path):
    _clean_tree(tmp_path)
    _write(tmp_path, "meridian_desktop/logic.py", "from PySide6 import QtWidgets\n")
    problems = check_boundary.check(tmp_path)
    assert any("logic.py" in p and "PySide6" in p for p in problems)


def test_nested_pyqt_import_outside_ui_fails(tmp_path):
    _clean_tree(tmp_path)
    _write(tmp_path, "meridian_desktop/services/status.py", "from PyQt5.QtCore import QTimer\n")
    problems = check_boundary.check(tmp_path)
    assert any("status.py" in p for p in problems)


def test_forbidden_reference_in_doc_fails(tmp_path):
    _clean_tree(tmp_path)
    _write(tmp_path, "docs/notes.md", f"see {LATEX_REF}\n")
    problems = check_boundary.check(tmp_path)
    assert any("notes.md" in p for p in problems)


def test_forbidden_reference_in_python_string_fails(tmp_path):
    _clean_tree(tmp_path)
    # Valid Python: the file holds the escaped form, the value has one backslash.
    _write(tmp_path, "meridian_desktop/paths.py", f'ENGINE = "{LATEX_REF_ESCAPED}"\n')
    problems = check_boundary.check(tmp_path)
    assert any("paths.py" in p for p in problems)


def test_forbidden_single_backslash_reference_fails(tmp_path):
    _clean_tree(tmp_path)
    _write(tmp_path, "docs/paths.md", f"path: {check_boundary.FORBIDDEN_REFS[1]}\n")
    problems = check_boundary.check(tmp_path)
    assert any("paths.md" in p for p in problems)


def test_missing_package_fails_closed(tmp_path):
    problems = check_boundary.check(tmp_path)
    assert problems
    assert "fail closed" in problems[0]


def test_violation_tree_exits_1_and_lists_each_violation(tmp_path, capsys):
    _clean_tree(tmp_path)
    _write(tmp_path, "meridian_desktop/a.py", "import PySide6\n")
    _write(tmp_path, "meridian_desktop/b.py", "from PySide6.QtCore import QObject\n")
    code = check_boundary.main(["--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1
    assert "a.py" in out
    assert "b.py" in out
    assert "boundary ok" not in out


def test_clean_tree_exits_0_with_ok_line(tmp_path, capsys):
    _clean_tree(tmp_path)
    code = check_boundary.main(["--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "boundary ok" in out


def test_pixi_env_is_not_scanned(tmp_path):
    _clean_tree(tmp_path)
    _write(tmp_path, ".pixi/envs/default/share/notes.md", f"see {LATEX_REF}\n")
    assert check_boundary.check(tmp_path) == []


def test_pixi_skip_does_not_hide_same_reference_elsewhere(tmp_path):
    # Control: the skip is limited to the .pixi directory.
    _clean_tree(tmp_path)
    _write(tmp_path, ".pixi/envs/default/share/notes.md", f"see {LATEX_REF}\n")
    _write(tmp_path, "envs/default/share/notes.md", f"see {LATEX_REF}\n")
    problems = check_boundary.check(tmp_path)
    assert len(problems) == 1
    assert "envs/default/share/notes.md" in problems[0]


def test_repo_files_prunes_pixi(tmp_path):
    _write(tmp_path, ".pixi/envs/default/x.txt", "x\n")
    _write(tmp_path, "README.md", "x\n")
    rels = [p.relative_to(tmp_path).as_posix() for p in check_boundary._repo_files(tmp_path)]
    assert rels == ["README.md"]


def test_real_repo_passes():
    problems = check_boundary.check(REPO)
    assert problems == [], "\n".join(problems)


# Qt roots the rule must cover. Written out here rather than read from
# check_boundary.QT_ROOTS, so that dropping a root from the checker is caught.
QT_ROOT_NAMES = ("PySide6", "PySide2", "PyQt5", "PyQt6", "PyQt4", "shiboken6", "shiboken2")


# Each case_* function takes a checker module and an empty directory, builds a
# tree, runs check(), and raises AssertionError on a wrong result. The tests below
# run them against the real checker. tests/test_boundary_mutation.py runs the same
# cases (via BOUNDARY_CASES) against mutated copies of the checker.


def case_qt_root_outside_ui_fails(checker, root, *, qt_root):
    _clean_tree(root)
    _write(root, "meridian_desktop/logic.py", f"from {qt_root}.QtCore import QObject\n")
    problems = checker.check(root)
    assert len(problems) == 1, problems
    assert "logic.py" in problems[0]
    assert qt_root in problems[0]


def case_import_inside_function_body_fails(checker, root):
    _clean_tree(root)
    _write(
        root,
        "meridian_desktop/logic.py",
        "def load_qt():\n    from PySide6.QtCore import QObject\n    return QObject\n",
    )
    problems = checker.check(root)
    assert len(problems) == 1, problems
    assert "logic.py" in problems[0]
    assert "PySide6" in problems[0]


def case_uppercase_windows_reference_fails(checker, root):
    _clean_tree(root)
    # The Windows form of the engine path, upper-cased. The scan ignores case.
    upper_ref = checker.FORBIDDEN_REFS[1].upper()
    _write(root, "docs/notes.md", f"path: {upper_ref}\n")
    problems = checker.check(root)
    assert len(problems) == 1, problems
    assert "notes.md" in problems[0]


def case_syntax_error_fails_closed(checker, root):
    _clean_tree(root)
    _write(root, "meridian_desktop/broken.py", "def broken(:\n    pass\n")
    problems = checker.check(root)
    assert len(problems) == 1, problems
    assert "broken.py" in problems[0]
    assert "fail closed" in problems[0]


def case_build_tree_is_not_scanned(checker, root):
    _clean_tree(root)
    _write(root, "build/lib/notes.md", f"see {LATEX_REF}\n")
    assert checker.check(root) == []


BOUNDARY_CASES = [
    (f"qt_root_{name}", partial(case_qt_root_outside_ui_fails, qt_root=name))
    for name in QT_ROOT_NAMES
] + [
    ("import_inside_function_body", case_import_inside_function_body_fails),
    ("uppercase_windows_reference", case_uppercase_windows_reference_fails),
    ("syntax_error_fails_closed", case_syntax_error_fails_closed),
    ("build_tree_not_scanned", case_build_tree_is_not_scanned),
]


@pytest.mark.parametrize("qt_root", QT_ROOT_NAMES)
def test_each_other_qt_root_outside_ui_fails(tmp_path, qt_root):
    case_qt_root_outside_ui_fails(check_boundary, tmp_path, qt_root=qt_root)


def test_import_inside_function_body_fails(tmp_path):
    case_import_inside_function_body_fails(check_boundary, tmp_path)


def test_uppercase_windows_reference_fails(tmp_path):
    case_uppercase_windows_reference_fails(check_boundary, tmp_path)


def test_syntax_error_fails_closed(tmp_path):
    case_syntax_error_fails_closed(check_boundary, tmp_path)


def test_build_tree_is_not_scanned(tmp_path):
    case_build_tree_is_not_scanned(check_boundary, tmp_path)
