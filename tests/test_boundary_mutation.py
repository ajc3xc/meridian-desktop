"""Mutation check for tools/check_boundary.py.

Each mutant is a copy of the checker with one guard removed or weakened. The
mutant is written under pytest's temp directory and loaded from there, so the
real checker is never modified. A mutant counts as killed when at least one case
from BOUNDARY_CASES (tests/test_boundary.py) fails on it, either with a wrong
result or with an exception.
"""

import importlib.util
from pathlib import Path

from test_boundary import BOUNDARY_CASES, REPO, check_boundary

CHECKER_SOURCE = REPO / "tools" / "check_boundary.py"

# Each mutant maps to (old, new). Every old text must occur exactly once in the
# checker source, so a mutant cannot quietly become a no-op or hit the wrong line.
# If the checker is edited and an anchor stops matching, this test fails and the
# anchor is updated here.
MUTANTS = {
    "drop_pyside2": ('"PySide2", ', ""),
    "drop_shiboken6": ('"shiboken6", ', ""),
    "walk_only_tree_body": ("for node in ast.walk(tree):", "for node in tree.body:"),
    # Only the file content is lowered. The FORBIDDEN_REFS constants are already
    # lower-case, so dropping ref.lower() would be an equivalent mutant.
    "drop_lower": ("lowered = path.read_bytes().lower()", "lowered = path.read_bytes()"),
    "remove_syntax_error_branch": (
        '''        try:
            with warnings.catch_warnings():
                # Escape-sequence warnings in scanned files are not this tool's concern.
                warnings.simplefilter("ignore")
                tree = ast.parse(path.read_bytes(), filename=str(path))
        except SyntaxError as exc:
            problems.append(
                f"{rel.as_posix()}:{exc.lineno}: cannot parse ({exc.msg}); "
                "boundary not checked (fail closed)"
            )
            continue
''',
        """        with warnings.catch_warnings():
            # Escape-sequence warnings in scanned files are not this tool's concern.
            warnings.simplefilter("ignore")
            tree = ast.parse(path.read_bytes(), filename=str(path))
""",
    ),
    "build_not_skipped": ('    "build",\n', ""),
}


def _mutate(source: str, name: str) -> str:
    old, new = MUTANTS[name]
    found = source.count(old)
    assert found == 1, f"mutant {name}: anchor found {found} times, expected exactly 1"
    return source.replace(old, new)


def _load(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _first_failing_case(checker, base: Path):
    """Run every boundary case against checker. Return (case, error) for the first failure, else None."""
    for case_name, case in BOUNDARY_CASES:
        root = base / case_name
        root.mkdir(parents=True)
        try:
            case(checker, root)
        except Exception as exc:  # an AssertionError or an exception from the checker both count
            return case_name, f"{type(exc).__name__}: {exc}"
    return None


def test_every_boundary_mutant_is_killed(tmp_path):
    source = CHECKER_SOURCE.read_text(encoding="utf-8")

    # The real checker must pass every case first. Otherwise every mutant would look killed.
    baseline = _first_failing_case(check_boundary, tmp_path / "baseline")
    assert baseline is None, f"the real checker fails a boundary case: {baseline}"

    survivors = []
    for name in MUTANTS:
        mutant_path = tmp_path / f"mutant_{name}.py"
        mutant_path.write_text(_mutate(source, name), encoding="utf-8")
        mutant = _load(mutant_path, f"check_boundary_mutant_{name}")
        if _first_failing_case(mutant, tmp_path / name) is None:
            survivors.append(name)

    assert survivors == [], f"mutants survived every boundary case: {survivors}"
