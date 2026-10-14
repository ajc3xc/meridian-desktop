#!/usr/bin/env python3
"""Verify the MeridianDesktop ONEDIR bundle against packaging/lgpl_manifest.toml.

Run after packaging/build.py. Exit 0 only when every check passes.

Fail closed: a check that cannot run (pefile or PyInstaller's archive reader
missing, a manifest table missing, an unreadable or unparsable file, any other
unexpected error) prints a FAIL line and is counted. It is never a skip, and it
never escapes as a traceback.

PYZ contents are scanned with PyInstaller's own ZlibArchiveReader. A bundle with
no PYZ archive, or a PYZ that cannot be read, fails with "PYZ contents not
scanned".

Licences come from the installed distributions via importlib.metadata (standard
library only).

Hashes: the first verified build writes sha256 values for every Qt6*.dll and
every Qt plugin into the [hashes] table of the manifest. Later builds must
match those values exactly. Writing happens only when every other check passes.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import re
import zipfile
from collections.abc import Callable
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None  # load_manifest turns this into a FAIL line

REPO = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO / "packaging" / "lgpl_manifest.toml"
DEFAULT_DIST = REPO / "dist" / "MeridianDesktop"
DEFAULT_REPORT = REPO / "build" / "linkage_report.json"
# Extracted PYZ entries go here: repo build/, never under dist/.
ARCHIVE_SCRATCH = REPO / "build" / "archive-scan"
SOURCE_PACKAGE = REPO / "meridian_desktop"
APP_EXE = "MeridianDesktop.exe"

FORBIDDEN_PATH_PARTS = {".git", ".venv", "tests"}
FORBIDDEN_FILE_NAMES = {".env", "meridian.toml"}
SECRET_MARKERS = (b"sk_meridian_", b"BEARER_TOKEN=")
# Matched case-insensitively against the file bytes.
ABS_PATH_MARKERS = (b"c:\\users\\13144", b"c:/users/13144")
QT_DLL_RE = re.compile(r"^Qt6.+\.dll$", re.IGNORECASE)
LGPL_TOKEN = "LGPL-3.0-only"
HASHES_HEADER_RE = re.compile(r"^\s*\[hashes\]\s*(#.*)?$")
TABLE_HEADER_RE = re.compile(r"^\s*\[")


# ---------------------------------------------------------------- helpers


def _cannot_run(what: str, exc: BaseException) -> str:
    return f"{what} could not run: {exc.__class__.__name__}: {exc} (fail closed)"


def _run_check(fn: Callable, *args) -> list[str]:
    """Run one check. An unexpected error becomes a FAIL line, never a traceback."""
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001 - this is the fail-closed boundary
        return [_cannot_run(fn.__name__, exc)]


def _run_check_pair(fn: Callable, *args) -> tuple[list[str], dict]:
    """Same as _run_check for checks that return (problems, report)."""
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001
        return [_cannot_run(fn.__name__, exc)], {}


def load_manifest(path: Path) -> dict:
    if tomllib is None:
        raise ValueError("neither tomllib nor tomli is installed")
    with path.open("rb") as fh:
        return tomllib.load(fh)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rel_posix(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def normalize_dist_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# ---------------------------------------------------------------- checks


def check_forbidden_paths(root: Path) -> list[str]:
    """No .git, .venv, tests/, .env or meridian.toml anywhere in the bundle."""
    problems = []
    for path in root.rglob("*"):
        rel = rel_posix(root, path)
        hit = set(path.relative_to(root).parts) & FORBIDDEN_PATH_PARTS
        if hit:
            problems.append(f"forbidden path component {sorted(hit)}: {rel}")
        if path.is_file() and (
            path.name in FORBIDDEN_FILE_NAMES or path.name.startswith(".env.")
        ):
            problems.append(f"forbidden file: {rel}")
    return problems


def check_source_leak(root: Path, source_package: Path = SOURCE_PACKAGE) -> list[str]:
    """No plain .py from meridian_desktop (by path or by content) in the bundle."""
    problems = []
    source_hashes = {
        sha256_file(p): rel_posix(source_package, p)
        for p in source_package.rglob("*.py")
    }
    for path in root.rglob("*.py"):
        rel = rel_posix(root, path)
        if "meridian_desktop" in rel:
            problems.append(f"meridian_desktop python source in bundle: {rel}")
        elif sha256_file(path) in source_hashes:
            problems.append(f"copy of meridian_desktop source in bundle: {rel}")
    for path in root.rglob("meridian_desktop"):
        problems.append(f"meridian_desktop package directory in bundle: {rel_posix(root, path)}")
    return problems


def _scan_blob(label: str, data: bytes, markers, case_insensitive: bool, problems: list[str]) -> None:
    haystack = data.lower() if case_insensitive else data
    for marker in markers:
        if marker in haystack:
            problems.append(f"{label}: contains marker {marker.decode('ascii', 'replace')!r}")


def scan_bundle_bytes(root: Path, markers, case_insensitive: bool) -> list[str]:
    """Scan every file, and every member of zip archives, for byte markers.

    Compressed content (PYZ, zip members that fail to open) is reported, not
    skipped. See scan_pyz_files for PYZ contents.
    """
    problems: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = rel_posix(root, path)
        _scan_blob(rel, path.read_bytes(), markers, case_insensitive, problems)
        if path.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(path) as archive:
                    for member in archive.namelist():
                        _scan_blob(f"{rel}!{member}", archive.read(member), markers, case_insensitive, problems)
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{rel}: zip archive not scanned ({exc.__class__.__name__}: {exc}) (fail closed)")
    return problems


def check_secrets(root: Path) -> list[str]:
    return [
        p.replace("contains marker", "secret-shaped string, contains marker")
        for p in scan_bundle_bytes(root, SECRET_MARKERS, case_insensitive=False)
    ]


def check_absolute_paths(root: Path) -> list[str]:
    return [
        p.replace("contains marker", "absolute developer path, contains marker")
        for p in scan_bundle_bytes(root, ABS_PATH_MARKERS, case_insensitive=True)
    ]


def check_qt_modules(root: Path, manifest: dict) -> tuple[list[str], dict]:
    """Every Qt6*.dll and every PySide6 .pyd maps to an allowed Qt module."""
    qt = manifest["qt"]
    allowed = set(qt["allowed_dlls"])
    forbidden = set(qt["forbidden_dlls"])
    allowed_plugin_dirs = set(qt["allowed_plugin_dirs"])
    allowed_plugin_files = set(qt.get("allowed_plugin_files", []))
    allowed_non_qt_pyd = set(qt.get("allowed_non_qt_pyd", []))
    problems: list[str] = []
    inventory: dict[str, list[str]] = {"qt_dlls": [], "pyside_pyd": [], "plugins": []}

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = rel_posix(root, path)
        parts = path.relative_to(root).parts
        name = path.name

        if QT_DLL_RE.match(name):
            stem = name[:-4]
            inventory["qt_dlls"].append(rel)
            if stem in forbidden:
                problems.append(f"forbidden Qt module DLL: {rel}")
            elif stem not in allowed:
                problems.append(f"Qt DLL not on allowed list (fail closed): {rel}")

        if "PySide6" in parts and path.suffix.lower() == ".pyd":
            inventory["pyside_pyd"].append(rel)
            stem = path.stem
            if stem.startswith("Qt"):
                dll = "Qt6" + stem[2:]
                if dll in forbidden:
                    problems.append(f"forbidden Qt module extension: {rel} needs {dll}")
                elif dll not in allowed:
                    problems.append(f"PySide6 extension for a Qt module not allowed: {rel} needs {dll}")
            elif stem not in allowed_non_qt_pyd:
                problems.append(f"non-Qt PySide6 extension not on allowed list: {rel}")

        if "PySide6" in parts and "plugins" in parts:
            idx = parts.index("plugins")
            if len(parts) > idx + 2:
                category = parts[idx + 1]
                inventory["plugins"].append(rel)
                if category not in allowed_plugin_dirs:
                    problems.append(f"Qt plugin category not allowed: {rel}")
                elif "/".join(parts[idx + 1 :]) not in allowed_plugin_files:
                    problems.append(f"Qt plugin file not on allowed list: {rel}")

    if not any(p.endswith("platforms/qwindows.dll") for p in inventory["plugins"]):
        problems.append("Qt platform plugin platforms/qwindows.dll missing")
    if not any(Path(p).name == "Qt6Core.dll" for p in inventory["qt_dlls"]):
        problems.append("Qt6Core.dll missing")
    return problems, inventory


def _one_line(value) -> str:
    return " ".join(str(value or "").split())


def run_license_inventory() -> list[dict]:
    """Licence inventory of the installed distributions, standard library only.

    Each row has Name, Version and License. License comes from the License field,
    then from License-Expression (PEP 639), then from the "License ::" classifiers.
    An empty License means the distribution declared no licence. check_licenses
    reports that as unknown.
    """
    rows: list[dict] = []
    for dist in importlib.metadata.distributions():
        meta = dist.metadata
        name = _one_line(meta.get("Name"))
        if not name:
            continue
        licence = _one_line(meta.get("License"))
        if not licence or licence.upper() == "UNKNOWN":
            licence = _one_line(meta.get("License-Expression"))
        if not licence:
            classifiers = [
                _one_line(c.split("::")[-1])
                for c in (meta.get_all("Classifier") or [])
                if c.strip().startswith("License ::")
            ]
            licence = " OR ".join(c for c in classifiers if c)
        rows.append({"Name": name, "Version": _one_line(meta.get("Version")), "License": licence})
    return rows


def _licence_problems(manifest: dict) -> list[str]:
    return check_licenses(run_license_inventory(), manifest)


def check_licenses(records: list[dict], manifest: dict) -> list[str]:
    """Every bundled distribution must carry an allowed licence.

    records: inventory rows from run_license_inventory(), each with Name, Version and License.
    """
    lic = manifest["licenses"]
    bundled = {normalize_dist_name(n) for n in manifest["bundled"]["distributions"]}
    family = {normalize_dist_name(n) for n in lic["lgpl_family"]}
    allowed = set(lic["allowed_spdx"])
    aliases = lic["aliases"]
    by_name = {normalize_dist_name(r["Name"]): r for r in records}
    problems: list[str] = []

    for name in sorted(bundled):
        row = by_name.get(name)
        if row is None:
            problems.append(f"bundled distribution has no licence record (fail closed): {name}")
            continue
        raw = (row.get("License") or "").strip()
        if not raw or raw.upper() in {"UNKNOWN", "NONE"}:
            problems.append(f"bundled distribution has unknown licence: {name}")
            continue
        if name in family:
            options = [t.strip() for t in re.split(r"\s+OR\s+", raw)]
            if LGPL_TOKEN in options or aliases.get(raw) == LGPL_TOKEN:
                continue
            problems.append(f"LGPL family distribution does not elect {LGPL_TOKEN}: {name} ({raw})")
            continue
        spdx = aliases.get(raw)
        if spdx is None:
            problems.append(f"bundled distribution licence not recognised (fail closed): {name} ({raw})")
        elif spdx.upper().startswith("GPL"):
            problems.append(f"GPL-only licence in bundle: {name} ({raw})")
        elif spdx not in allowed:
            problems.append(f"licence not on allowed list: {name} ({spdx})")
    return problems


def check_notices(root: Path, manifest: dict) -> list[str]:
    qt_version = manifest["pyside6"]["qt_version"]
    problems: list[str] = []

    def need(rel: str, markers: list[str]) -> None:
        path = root / rel
        if not path.is_file():
            problems.append(f"notice file missing: {rel}")
            return
        text = path.read_text(encoding="utf-8", errors="replace")
        for marker in markers:
            if marker not in text:
                problems.append(f"{rel} lacks required marker {marker!r}")

    lgpl = root / "LICENSES" / "LGPL-3.0.txt"
    if not lgpl.is_file() or lgpl.stat().st_size == 0:
        problems.append("LICENSES/LGPL-3.0.txt missing or empty")
    else:
        if "GNU LESSER GENERAL PUBLIC LICENSE" not in lgpl.read_text(encoding="utf-8", errors="replace"):
            problems.append("LICENSES/LGPL-3.0.txt lacks the LGPL title")
    need("NOTICE.md", ["PySide6", "shiboken6", qt_version, LGPL_TOKEN])
    need("OFFER-SOURCE.md", ["PySide6", qt_version, "https://"])
    need("EULA.txt", ["DRAFT", "reverse engineer", "modif", "counsel"])
    return problems


# PYZ contents: every module's code object is scanned.


def _scan_code(code, label: str, problems: list[str]) -> None:
    """Scan one code object and every code object nested in it."""
    filename = code.co_filename.lower()
    for marker in ABS_PATH_MARKERS:
        if marker.decode() in filename:
            problems.append(f"{label}: code object filename holds absolute developer path")
    for const in code.co_consts:
        _scan_constant(const, label, problems)


def _scan_constant(const, label: str, problems: list[str]) -> None:
    if hasattr(const, "co_filename"):
        _scan_code(const, label, problems)
    elif isinstance(const, (tuple, frozenset)):
        for item in const:
            _scan_constant(item, label, problems)
    elif isinstance(const, (str, bytes)):
        data = const if isinstance(const, bytes) else const.encode("utf-8", "replace")
        lowered = data.lower()
        for marker in SECRET_MARKERS:
            if marker in data:
                problems.append(
                    f"{label}: secret-shaped string in code constants, contains marker {marker.decode()!r}"
                )
        for marker in ABS_PATH_MARKERS:
            if marker in lowered:
                problems.append(
                    f"{label}: absolute developer path in code constants, contains marker {marker.decode()!r}"
                )


def scan_pyz_files(pyz_files: list[Path]) -> list[str]:
    """Scan every module of each PYZ archive with PyInstaller's ZlibArchiveReader.

    A PYZ that cannot be opened, or an entry that cannot be extracted, is reported
    as "PYZ contents not scanned". It is never a silent pass.
    """
    try:
        from PyInstaller.loader.pyimod01_archive import ZlibArchiveReader
    except Exception as exc:  # noqa: BLE001
        return [
            "PYZ contents not scanned: PyInstaller archive reader unavailable "
            f"({exc.__class__.__name__}: {exc}) (fail closed)"
        ]

    problems: list[str] = []
    for pyz in pyz_files:
        try:
            reader = ZlibArchiveReader(str(pyz))
            names = sorted(reader.toc)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{pyz.name}: PYZ contents not scanned ({exc.__class__.__name__}: {exc}) (fail closed)")
            continue
        for name in names:
            label = f"{pyz.name}:{name}"
            try:
                code = reader.extract(name)
            except (Exception, SystemExit) as exc:  # the reader exits if the file vanished
                problems.append(f"{label}: PYZ entry not scanned ({exc.__class__.__name__}: {exc}) (fail closed)")
                continue
            if code is None:
                continue  # PEP 420 namespace package: no code object
            if not hasattr(code, "co_filename"):
                problems.append(f"{label}: PYZ entry is not a code object, not scanned (fail closed)")
                continue
            _scan_code(code, label, problems)
    return problems


def check_pyinstaller_archive(exe: Path, scratch: Path = ARCHIVE_SCRATCH) -> list[str]:
    """Collect the bundle's PYZ archives and scan them.

    PYZ files beside the exe are scanned in place. PYZ entries inside the exe's
    CArchive are extracted to `scratch` (build/archive-scan at the repo root, never
    under dist/) and scanned from there. A bundle with no PYZ at all fails.
    """
    try:
        from PyInstaller.archive.readers import CArchiveReader
    except Exception as exc:  # noqa: BLE001
        return [
            "PYZ contents not scanned: PyInstaller archive reader unavailable "
            f"({exc.__class__.__name__}: {exc}) (fail closed)"
        ]
    try:
        archive = CArchiveReader(str(exe))
    except Exception as exc:  # noqa: BLE001
        return [
            f"{exe.name}: PYZ contents not scanned, exe is not a readable PyInstaller archive "
            f"({exc.__class__.__name__}: {exc}) (fail closed)"
        ]

    problems: list[str] = []
    pyz_files = sorted(exe.parent.rglob("*.pyz"))
    for entry in sorted(archive.toc):
        if not entry.endswith(".pyz"):
            continue
        try:
            blob = archive.extract(entry)
            if isinstance(blob, tuple):
                blob = blob[1]
            scratch.mkdir(parents=True, exist_ok=True)
            target = scratch / entry
            target.write_bytes(blob)
            pyz_files.append(target)
        except Exception as exc:  # noqa: BLE001
            problems.append(
                f"{exe.name}!{entry}: PYZ contents not scanned ({exc.__class__.__name__}: {exc}) (fail closed)"
            )
    if not pyz_files:
        problems.append(f"{exe.name}: no PYZ archive found: PYZ contents not scanned (fail closed)")
        return problems
    return problems + scan_pyz_files(pyz_files)


# PE linkage


def _pe_imports(pefile, path: Path) -> list[str]:
    pe = pefile.PE(str(path), fast_load=True)
    try:
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
        return [entry.dll.decode("ascii", "replace") for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", [])]
    finally:
        pe.close()


def _file_version(pefile, path: Path) -> str:
    pe = pefile.PE(str(path), fast_load=True)
    try:
        # fast_load skips resources, so the version resource must be parsed explicitly.
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]])
        info = pe.VS_FIXEDFILEINFO[0]
        return ".".join(
            str(x)
            for x in (
                info.FileVersionMS >> 16,
                info.FileVersionMS & 0xFFFF,
                info.FileVersionLS >> 16,
            )
        )
    finally:
        pe.close()


def check_pe_linkage(root: Path, manifest: dict, exe: Path) -> tuple[list[str], dict]:
    """Parse PE import tables with pefile. Every Qt DLL a PySide6 extension, Qt DLL
    or plugin imports must be an allowed Qt DLL that exists as a separate file.
    """
    try:
        import pefile  # noqa: WPS433
    except ImportError:
        return ["pefile not installed: PE import tables cannot be verified (fail closed)"], {}

    problems: list[str] = []
    report: dict[str, dict] = {}
    allowed = set(manifest["qt"]["allowed_dlls"])
    present = {p.name.lower(): p for p in root.rglob("*.dll")}

    targets = [p for p in root.rglob("*") if p.is_file() and (
        p.suffix.lower() == ".pyd" and "PySide6" in p.relative_to(root).parts
        or QT_DLL_RE.match(p.name)
        or ("PySide6" in p.relative_to(root).parts and "plugins" in p.relative_to(root).parts and p.suffix.lower() == ".dll")
    )]
    for path in sorted(targets):
        rel = rel_posix(root, path)
        try:
            imports = _pe_imports(pefile, path)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{rel}: PE parse failed ({exc.__class__.__name__}: {exc}) (fail closed)")
            continue
        qt_imports = sorted({d for d in imports if QT_DLL_RE.match(d)})
        report[rel] = {"imports": sorted(set(imports)), "qt_imports": qt_imports}
        for dll in qt_imports:
            stem = dll[:-4]
            if stem not in allowed:
                problems.append(f"{rel} imports Qt DLL not on allowed list: {dll}")
            if dll.lower() not in present:
                problems.append(f"{rel} imports {dll} which is not a separate file in the bundle")

    report["_exe"] = {"path": APP_EXE}
    try:
        _pe_imports(pefile, exe)  # the exe must parse as a PE image
    except Exception as exc:  # noqa: BLE001
        problems.append(f"{APP_EXE}: PE parse failed ({exc.__class__.__name__}: {exc}) (fail closed)")

    qt_core = present.get("qt6core.dll")
    if qt_core is not None:
        try:
            version = _file_version(pefile, qt_core)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"Qt6Core.dll: version unreadable ({exc.__class__.__name__}: {exc}) (fail closed)")
        else:
            report["_qt_core_version"] = {"file_version": version}
            if not version.startswith(manifest["pyside6"]["qt_version"]):
                problems.append(
                    f"Qt6Core.dll version {version} does not match manifest qt_version "
                    f"{manifest['pyside6']['qt_version']}"
                )
    return problems, report


def check_exe_embeds_no_pe(exe: Path) -> list[str]:
    """The exe must not embed another PE image (Qt DLLs stay separate files)."""
    data = exe.read_bytes()
    problems: list[str] = []
    start = 1
    while True:
        idx = data.find(b"MZ", start)
        if idx < 0:
            break
        start = idx + 1
        if idx + 0x40 > len(data):
            continue
        e_lfanew = int.from_bytes(data[idx + 0x3C : idx + 0x40], "little")
        pe_off = idx + e_lfanew
        if 0 < e_lfanew < 0x1000 and pe_off + 4 <= len(data) and data[pe_off : pe_off + 4] == b"PE\0\0":
            problems.append(f"exe embeds a PE image at offset {idx}")
    return problems


def check_hashes(root: Path, manifest: dict) -> tuple[list[str], dict[str, str]]:
    """Return (problems, computed). The caller writes the table on a first pass."""
    targets = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        parts = path.relative_to(root).parts
        if QT_DLL_RE.match(path.name) or ("PySide6" in parts and "plugins" in parts):
            targets[rel_posix(root, path)] = sha256_file(path)

    expected = manifest.get("hashes", {})
    problems: list[str] = []
    if not expected:
        return ["hashes table empty: first verified build must write it"], targets
    for key in sorted(set(targets) - set(expected)):
        problems.append(f"file not in hashes table: {key}")
    for key in sorted(set(expected) - set(targets)):
        problems.append(f"hashed file missing from bundle: {key}")
    for key in sorted(set(targets) & set(expected)):
        if targets[key] != expected[key]:
            problems.append(f"sha256 mismatch: {key}")
    return problems, targets


def write_hashes(manifest_path: Path, hashes: dict[str, str]) -> None:
    """Replace the [hashes] table with `hashes`.

    [hashes] may sit on any line, including the first. A missing table is appended.
    Refuses to rewrite when another table follows [hashes], because that would
    delete it.
    """
    head: list[str] = []
    tail: list[str] = []
    found = False
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        if not found and HASHES_HEADER_RE.match(line):
            found = True
        elif found:
            tail.append(line)
        else:
            head.append(line)
    if any(TABLE_HEADER_RE.match(line) for line in tail):
        raise ValueError("[hashes] is followed by another table; refusing to rewrite the manifest")

    while head and not head[-1].strip():
        head.pop()
    body = head + [""] if head else []
    body += [
        "[hashes]",
        "# sha256 of every Qt6*.dll and Qt plugin, written by verify_bundle.py on the",
        "# first verified build. Later builds must match exactly.",
    ]
    body += [f'"{key}" = "{hashes[key]}"' for key in sorted(hashes)]
    manifest_path.write_text("\n".join(body) + "\n", encoding="utf-8", newline="\n")


# ---------------------------------------------------------------- driver


def run(dist: Path, manifest_path: Path, report_path: Path, write: bool) -> int:
    try:
        manifest = load_manifest(manifest_path)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL manifest unreadable: {manifest_path} ({exc.__class__.__name__}: {exc}) (fail closed)")
        print("RESULT FAIL (1 problems)")
        return 1

    exe = dist / APP_EXE
    if not dist.is_dir() or not exe.is_file():
        print(f"FAIL bundle not found: {dist}")
        return 1

    sections: list[tuple[str, list[str]]] = [
        ("forbidden paths", _run_check(check_forbidden_paths, dist)),
        ("no meridian_desktop source", _run_check(check_source_leak, dist)),
        ("no secret-shaped strings", _run_check(check_secrets, dist)),
        ("no absolute developer paths", _run_check(check_absolute_paths, dist)),
        ("pyinstaller archive scan", _run_check(check_pyinstaller_archive, exe)),
        ("exe embeds no PE image", _run_check(check_exe_embeds_no_pe, exe)),
    ]

    report: dict = {}
    qt_problems, inventory = _run_check_pair(check_qt_modules, dist, manifest)
    report["inventory"] = inventory
    sections.append(("allowed Qt modules only", qt_problems))
    sections.append(("bundled licences allowed", _run_check(_licence_problems, manifest)))
    sections.append(("notices present", _run_check(check_notices, dist, manifest)))

    pe_problems, pe_report = _run_check_pair(check_pe_linkage, dist, manifest, exe)
    report["pe"] = pe_report
    sections.append(("PE import tables dynamic", pe_problems))

    failures = 0
    for title, problems in sections:
        status = "PASS" if not problems else "FAIL"
        print(f"{status} {title}")
        for problem in problems:
            print(f"     - {problem}")
        failures += len(problems)

    # Hashes: write on the first verified build only, when nothing else failed.
    if failures == 0 and write and not manifest.get("hashes"):
        try:
            _ignored, computed = check_hashes(dist, {"hashes": {}})
            write_hashes(manifest_path, computed)
            print(f"WROTE {len(computed)} sha256 hashes into {manifest_path.name} (first verified build)")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL sha256 write: {_cannot_run('writing hashes', exc)}")
    else:
        hash_problems, _computed = _run_check_pair(check_hashes, dist, manifest)
        status = "PASS" if not hash_problems else "FAIL"
        print(f"{status} sha256 match manifest")
        for problem in hash_problems:
            print(f"     - {problem}")
        failures += len(hash_problems)

    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"linkage report: {report_path}")
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print(f"FAIL linkage report not written: {_cannot_run('writing the report', exc)}")

    print("RESULT", "PASS" if failures == 0 else f"FAIL ({failures} problems)")
    return 0 if failures == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the MeridianDesktop ONEDIR bundle.")
    parser.add_argument("--dist", type=Path, default=DEFAULT_DIST)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--no-write-hashes", action="store_true")
    args = parser.parse_args(argv)
    return run(args.dist, args.manifest, args.report, write=not args.no_write_hashes)


if __name__ == "__main__":
    raise SystemExit(main())
