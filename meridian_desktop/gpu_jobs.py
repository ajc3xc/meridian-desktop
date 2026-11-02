"""Read-only GPU jobs for the GPUs tab: this computer's GPUs and their processes, the SSH hosts saved in Settings, Slurm and
LSF GPU jobs, and RunPod pods from the local Meridian server.

Pure Python, no Qt. Every outside call goes through a runner the caller passes in. A runner is ``run(argv, timeout)``
and returns a CommandResult. The default runner starts a program from an argument list with no shell, with a timeout,
and with no input, so a password prompt cannot wait. Tests pass a fake runner, so they start no process and open no
socket.

Nothing here starts, stops, submits or changes anything. The commands are fixed. The only text from the owner is a
saved SSH host name. It is checked against ALIAS_PATTERN, and it is always passed after "--", so it can never read as
an option.

What each part reads, and what it says when it cannot read:
- This computer, GPUs: nvidia-smi, two queries. Without nvidia-smi the GPUs part says so.
- Each saved host: one ssh call that runs the same nvidia-smi queries, then squeue and bjobs when they exist on that
  host. A host that fails names its reason (host key unknown, auth failed, timed out, unreachable). It never stops the
  other hosts.
- Slurm and LSF on this computer: squeue --me and bjobs. Without them the part says "not on this machine".
- RunPod: the pods the local Meridian server lists. Without that server the part says "not connected".
"""

from __future__ import annotations

import csv
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

# ----------------------------------------------------------------- limits and fixed text

MAX_HOSTS = 8
MAX_WORKERS = 4
LOCAL_TIMEOUT_SECONDS = 10.0
SSH_TIMEOUT_SECONDS = 20.0
# A saved host is an SSH alias from the owner's own ssh config: letters, digits, dot, dash, underscore and "@" for
# user@host. It may not start with a dash, so it can never be read as an option.
ALIAS_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.@-]{0,62}")

LOCAL_HOST = "This computer"

SOURCE_GPU_PROCESS = "GPU process"
SOURCE_SLURM = "Slurm"
SOURCE_LSF = "LSF"
SOURCE_RUNPOD = "RunPod"

PART_GPUS = "GPUs"
PART_PROCESSES = "Processes"
PART_SLURM = "Slurm"
PART_LSF = "LSF"
PART_RUNPOD = "RunPod"

STATE_OK = "ok"
STATE_EMPTY = "empty"  # the tool answered and reported nothing
STATE_ABSENT = "absent"  # the tool is not on this machine or host
STATE_FAILED = "failed"  # the tool was there but the read failed

# The nvidia-smi fields the GPU table shows, in the order the query asks for them.
GPU_FIELDS = "index,name,uuid,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit"
APP_FIELDS = "pid,process_name,used_memory,gpu_uuid"
NVIDIA_FORMAT = "--format=csv,noheader,nounits"
NVIDIA_GPU_ARGV = ("nvidia-smi", f"--query-gpu={GPU_FIELDS}", NVIDIA_FORMAT)
NVIDIA_APPS_ARGV = ("nvidia-smi", f"--query-compute-apps={APP_FIELDS}", NVIDIA_FORMAT)
# squeue: job id, user, name, state, elapsed, nodes, gres. A job name may hold "|", so the name is what sits between
# the user and the state, and the fixed fields are counted from the end.
SQUEUE_FORMAT = "%i|%u|%j|%T|%M|%D|%b"
SQUEUE_ARGV = ("squeue", "--me", "--noheader", f"--format={SQUEUE_FORMAT}")
# bjobs: the GPU column is gpu_alloc. Its name on an LSF host is not verified here, so an unknown name fails the read
# in plain words rather than showing a wrong job.
LSF_FIELDS = "jobid stat user run_time gpu_alloc job_name"
BJOBS_ARGV = ("bjobs", "-noheader", "-o", LSF_FIELDS)

# One remote command for a saved host. Each section after the first starts with a marker line, so the reader can tell
# "the tool is not there" (no marker) from "it is there and reported nothing" (marker, no rows).
APPS_MARKER = "---APPS---"
SLURM_MARKER = "---SLURM---"
LSF_MARKER = "---LSF---"
REMOTE_COMMAND = " ; ".join(
    [
        f"nvidia-smi --query-gpu={GPU_FIELDS} {NVIDIA_FORMAT}",
        f"echo {APPS_MARKER}",
        f"nvidia-smi --query-compute-apps={APP_FIELDS} {NVIDIA_FORMAT}",
        f"if command -v squeue >/dev/null 2>&1; then echo {SLURM_MARKER}; squeue --me --noheader '--format={SQUEUE_FORMAT}'; fi",
        f"if command -v bjobs >/dev/null 2>&1; then echo {LSF_MARKER}; bjobs -noheader -o '{LSF_FIELDS}'; fi",
    ]
)

# nvidia-smi writes these where a value does not exist or the process owner cannot be read.
UNREPORTED_VALUES = frozenset({"", "[N/A]", "N/A", "[Not Supported]", "[Unknown Error]"})
NAME_HIDDEN_PREFIX = "["  # nvidia-smi writes a process name in brackets when it cannot read it, for example [Insufficient Permissions]

RUNPOD_NOT_CONNECTED_TEXT = "Not connected. The local Meridian server is not answering, so RunPod pods are not listed."
RUNPOD_NOT_LISTED_TEXT = "Not connected. The local Meridian server did not list RunPod pods."

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ----------------------------------------------------------------- data


@dataclass(frozen=True)
class CommandResult:
    """What one program call returned. ``returncode`` is None when the program never finished."""

    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    missing: bool = False  # the program is not on this machine
    timed_out: bool = False
    failed_to_start: bool = False


Runner = Callable[[Sequence[str], float], CommandResult]


@dataclass(frozen=True)
class GpuRow:
    host: str
    index: str
    name: str
    uuid: str
    utilization: int | None
    memory_used_mib: int | None
    memory_total_mib: int | None
    temperature_c: int | None
    power_w: float | None
    power_limit_w: float | None


@dataclass(frozen=True)
class GpuJob:
    """One GPU job or process. ``gpu`` is the GPU index, the GPU's text, or the job's GPU request."""

    source: str
    host: str
    job_id: str
    user: str
    name: str
    state: str
    gpu: str
    gpu_memory_mib: int | None


@dataclass(frozen=True)
class SourceStatus:
    """One line of the host status list: where it was read, which part, whether it worked, and what to say."""

    where: str
    part: str
    state: str
    text: str


@dataclass(frozen=True)
class GpuSnapshot:
    checked_at: datetime
    gpus: tuple[GpuRow, ...] = ()
    jobs: tuple[GpuJob, ...] = ()
    statuses: tuple[SourceStatus, ...] = ()


@dataclass(frozen=True)
class _Reading:
    gpus: tuple[GpuRow, ...] = ()
    jobs: tuple[GpuJob, ...] = ()
    statuses: tuple[SourceStatus, ...] = ()


# ----------------------------------------------------------------- the default runner


def run_command(argv: Sequence[str], timeout: float) -> CommandResult:
    """Start one program from an argument list. No shell, no input, no window, and a timeout. Never raises."""
    kwargs: dict[str, Any] = {}
    if _NO_WINDOW:
        kwargs["creationflags"] = _NO_WINDOW
    try:
        done = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            check=False,
            stdin=subprocess.DEVNULL,
            **kwargs,
        )
    except FileNotFoundError:
        return CommandResult(returncode=None, missing=True)
    except subprocess.TimeoutExpired:
        return CommandResult(returncode=None, timed_out=True)
    except OSError:
        return CommandResult(returncode=None, failed_to_start=True)
    return CommandResult(returncode=done.returncode, stdout=done.stdout, stderr=done.stderr)


def ssh_argv(alias: str) -> tuple[str, ...]:
    """The ssh call for one saved host. Batch mode asks for no password, and an unknown host key is refused, not
    accepted. The alias comes after "--" and has already passed ALIAS_PATTERN."""
    return (
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "StrictHostKeyChecking=yes",
        "--",
        alias,
        REMOTE_COMMAND,
    )


# ----------------------------------------------------------------- host names


def check_alias(text: str) -> bool:
    """True when the text is a safe SSH host name for a saved host."""
    # fullmatch, not match with "$": a "$" also matches before a trailing newline, which would let "gpu-box\n" through.
    return isinstance(text, str) and ALIAS_PATTERN.fullmatch(text) is not None


def parse_host_list(text: str) -> tuple[tuple[str, ...], list[str]]:
    """Split a comma-separated line of SSH host names. Returns the valid names, in order and without repeats, and the
    plain problems with the rest. An empty line means no hosts and no problem."""
    hosts: list[str] = []
    problems: list[str] = []
    for raw in text.split(","):
        item = raw.strip()
        if not item:
            continue
        if not check_alias(item):
            problems.append(f'"{item[:40]}" is not a valid SSH host name. Use letters, digits, dot, dash or underscore.')
            continue
        if item not in hosts:
            hosts.append(item)
    if len(hosts) > MAX_HOSTS:
        problems.append(f"Save at most {MAX_HOSTS} SSH hosts. The first {MAX_HOSTS} are kept.")
        hosts = hosts[:MAX_HOSTS]
    return tuple(hosts), problems


# ----------------------------------------------------------------- pure parsers


def _number(text: str) -> float | None:
    cleaned = text.strip()
    if cleaned in UNREPORTED_VALUES:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _whole(text: str) -> int | None:
    value = _number(text)
    return None if value is None else int(value)


def _csv_fields(line: str) -> list[str]:
    return [field.strip() for field in next(csv.reader([line], skipinitialspace=True))]


def _lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def parse_gpus(text: str, host: str) -> tuple[tuple[GpuRow, ...], int]:
    """Parse the GPU query. Returns the rows and how many non-empty lines did not match the nine fields."""
    rows: list[GpuRow] = []
    unmatched = 0
    for line in _lines(text):
        fields = _csv_fields(line)
        if len(fields) != 9:
            unmatched += 1
            continue
        rows.append(
            GpuRow(
                host=host,
                index=fields[0],
                name=fields[1],
                uuid=fields[2],
                utilization=_whole(fields[3]),
                memory_used_mib=_whole(fields[4]),
                memory_total_mib=_whole(fields[5]),
                temperature_c=_whole(fields[6]),
                power_w=_number(fields[7]),
                power_limit_w=_number(fields[8]),
            )
        )
    return tuple(rows), unmatched


def _process_name(raw: str) -> tuple[str, bool]:
    """A short name for a process: the file name of its program. A name nvidia-smi could not read is said so, and
    reported as hidden, so the status line can explain it."""
    cleaned = raw.strip()
    if cleaned.startswith(NAME_HIDDEN_PREFIX) and cleaned.endswith("]"):
        return f"(not readable: {cleaned[1:-1]})", True
    base = re.split(r"[\\/]", cleaned)[-1] if cleaned else ""
    return base or "(no name)", False


def parse_apps(text: str, host: str, gpus: Sequence[GpuRow]) -> tuple[tuple[GpuJob, ...], int, bool]:
    """Parse the compute-apps query. Returns the jobs, the unmatched line count, and whether any name was hidden."""
    index_by_uuid = {row.uuid: row.index for row in gpus if row.uuid}
    jobs: list[GpuJob] = []
    unmatched = 0
    hidden = False
    for line in _lines(text):
        fields = _csv_fields(line)
        if len(fields) != 4:
            unmatched += 1
            continue
        pid, raw_name, used, uuid = fields
        name, was_hidden = _process_name(raw_name)
        hidden = hidden or was_hidden
        jobs.append(
            GpuJob(
                source=SOURCE_GPU_PROCESS,
                host=host,
                job_id=pid,
                user="",
                name=name,
                state="running",
                gpu=index_by_uuid.get(uuid, uuid),
                gpu_memory_mib=_whole(used),
            )
        )
    return tuple(jobs), unmatched, hidden


def parse_squeue(text: str, host: str) -> tuple[tuple[GpuJob, ...], int]:
    """Parse squeue output in SQUEUE_FORMAT. Only jobs that request a GPU (a gres with "gpu") are returned."""
    jobs: list[GpuJob] = []
    unmatched = 0
    for line in _lines(text):
        parts = line.split("|")
        if len(parts) < 7:
            unmatched += 1
            continue
        gres = parts[-1].strip()
        if "gpu" not in gres.lower():
            continue
        jobs.append(
            GpuJob(
                source=SOURCE_SLURM,
                host=host,
                job_id=parts[0].strip(),
                user=parts[1].strip(),
                name="|".join(parts[2:-4]).strip(),
                state=parts[-4].strip(),
                gpu=gres,
                gpu_memory_mib=None,
            )
        )
    return tuple(jobs), unmatched


def parse_bjobs(text: str, host: str) -> tuple[tuple[GpuJob, ...], int]:
    """Parse bjobs output in LSF_FIELDS. Only jobs with a GPU in gpu_alloc are returned. A dash means no GPU."""
    jobs: list[GpuJob] = []
    unmatched = 0
    for line in _lines(text):
        tokens = line.split(None, 5)
        if len(tokens) < 6:
            unmatched += 1
            continue
        job_id, state, user, _elapsed, gpu_alloc, name = tokens
        if gpu_alloc in ("-", "") or not any(char.isdigit() for char in gpu_alloc):
            continue
        jobs.append(
            GpuJob(
                source=SOURCE_LSF,
                host=host,
                job_id=job_id,
                user=user,
                name=name.strip(),
                state=state,
                gpu=gpu_alloc,
                gpu_memory_mib=None,
            )
        )
    return tuple(jobs), unmatched


def parse_runpod_pods(payload: Mapping[str, Any]) -> tuple[GpuJob, ...]:
    """Pods from the local server's RunPod list. A pod with no readable id is skipped."""
    raw = payload.get("pods")
    if not isinstance(raw, list):
        return ()
    jobs: list[GpuJob] = []
    for pod in raw:
        if not isinstance(pod, dict) or not pod.get("id"):
            continue
        gpu = pod.get("gpu")
        gpu_text = ""
        if isinstance(gpu, dict) and gpu.get("id") is not None:
            gpu_text = str(gpu["id"]) if gpu.get("count") is None else f"{gpu['id']} x{gpu['count']}"
        jobs.append(
            GpuJob(
                source=SOURCE_RUNPOD,
                host=SOURCE_RUNPOD,
                job_id=str(pod["id"]),
                user="",
                name=str(pod.get("name") or pod["id"]),
                state=str(pod.get("status") or ""),
                gpu=gpu_text,
                gpu_memory_mib=None,
            )
        )
    return tuple(jobs)


# ----------------------------------------------------------------- reading one place


def _nvidia_gpu_status(where: str, result: CommandResult, *, remote: bool) -> tuple[tuple[GpuRow, ...], SourceStatus]:
    """The GPU rows and the status line for one nvidia-smi GPU query."""
    if result.missing:
        text = (
            "nvidia-smi is not on this host, so its GPUs cannot be read."
            if remote
            else "nvidia-smi is not on this computer, so its GPUs cannot be read."
        )
        return (), SourceStatus(where, PART_GPUS, STATE_ABSENT, text)
    if result.timed_out:
        return (), SourceStatus(where, PART_GPUS, STATE_FAILED, "nvidia-smi timed out.")
    if result.returncode != 0:
        return (), SourceStatus(
            where, PART_GPUS, STATE_FAILED, f"nvidia-smi could not read the GPUs (exit code {result.returncode})."
        )
    rows, unmatched = parse_gpus(result.stdout, where)
    if not rows:
        if unmatched:
            return (), SourceStatus(where, PART_GPUS, STATE_FAILED, "nvidia-smi printed output this desktop does not read.")
        return (), SourceStatus(where, PART_GPUS, STATE_EMPTY, "No GPUs reported.")
    noun = "GPU" if len(rows) == 1 else "GPUs"
    return rows, SourceStatus(where, PART_GPUS, STATE_OK, f"{len(rows)} {noun} read.")


def _apps_status(where: str, result: CommandResult, gpus: Sequence[GpuRow]) -> tuple[tuple[GpuJob, ...], SourceStatus]:
    """The GPU process rows and the status line for one nvidia-smi process query."""
    if result.timed_out:
        return (), SourceStatus(where, PART_PROCESSES, STATE_FAILED, "GPU processes timed out.")
    if result.missing or result.returncode != 0:
        return (), SourceStatus(where, PART_PROCESSES, STATE_FAILED, "GPU processes could not be read.")
    jobs, unmatched, hidden = parse_apps(result.stdout, where, gpus)
    if unmatched and not jobs:
        return (), SourceStatus(where, PART_PROCESSES, STATE_FAILED, "GPU processes printed output this desktop does not read.")
    if not jobs:
        return (), SourceStatus(where, PART_PROCESSES, STATE_EMPTY, "No GPU processes are running.")
    noun = "process" if len(jobs) == 1 else "processes"
    text = f"{len(jobs)} GPU {noun} listed."
    if hidden:
        text += " Some names need admin rights to read."
    return jobs, SourceStatus(where, PART_PROCESSES, STATE_OK, text)


def _scheduler_status(
    where: str, part: str, result: CommandResult | None, parser: Callable[[str, str], tuple[tuple[GpuJob, ...], int]]
) -> tuple[tuple[GpuJob, ...], SourceStatus]:
    """Slurm or LSF on one place. ``result`` is None when the tool is not there (a remote host with no marker)."""
    if result is None or result.missing:
        text = f"{part}: not on this machine." if where == LOCAL_HOST else f"{part}: not on this host."
        return (), SourceStatus(where, part, STATE_ABSENT, text)
    if result.timed_out or result.returncode != 0:
        return (), SourceStatus(where, part, STATE_FAILED, f"{part} could not be read.")
    jobs, unmatched = parser(result.stdout, where)
    if not jobs:
        text = f"No {part} GPU jobs."
        if unmatched:
            text = f"{part} printed output this desktop does not read."
            return (), SourceStatus(where, part, STATE_FAILED, text)
        return (), SourceStatus(where, part, STATE_EMPTY, text)
    noun = "job" if len(jobs) == 1 else "jobs"
    return jobs, SourceStatus(where, part, STATE_OK, f"{len(jobs)} GPU {noun}.")


def _read_local(run: Runner) -> _Reading:
    gpu_result = run(NVIDIA_GPU_ARGV, LOCAL_TIMEOUT_SECONDS)
    gpus, gpu_status = _nvidia_gpu_status(LOCAL_HOST, gpu_result, remote=False)
    apps_result = run(NVIDIA_APPS_ARGV, LOCAL_TIMEOUT_SECONDS)
    apps, apps_status = _apps_status(LOCAL_HOST, apps_result, gpus)
    slurm_result = run(SQUEUE_ARGV, LOCAL_TIMEOUT_SECONDS)
    slurm, slurm_status = _scheduler_status(
        LOCAL_HOST, PART_SLURM, slurm_result, lambda text, host: parse_squeue(text, host)
    )
    lsf_result = run(BJOBS_ARGV, LOCAL_TIMEOUT_SECONDS)
    lsf, lsf_status = _scheduler_status(LOCAL_HOST, PART_LSF, lsf_result, lambda text, host: parse_bjobs(text, host))
    return _Reading(
        gpus=gpus,
        jobs=apps + slurm + lsf,
        statuses=(gpu_status, apps_status, slurm_status, lsf_status),
    )


def _ssh_failure(where: str, result: CommandResult) -> SourceStatus:
    """Name the reason an ssh call failed. The text is fixed: the raw error is never shown."""
    if result.missing:
        return SourceStatus(where, PART_GPUS, STATE_ABSENT, "ssh is not on this computer, so this host cannot be read.")
    if result.failed_to_start:
        return SourceStatus(where, PART_GPUS, STATE_FAILED, "ssh could not be started.")
    if result.timed_out:
        return SourceStatus(where, PART_GPUS, STATE_FAILED, "Timed out. The host did not answer in time.")
    stderr = result.stderr.lower()
    if "host key verification failed" in stderr or "no matching host key" in stderr:
        reason = "Host key unknown. Connect to it once in a terminal to accept its key."
    elif "permission denied" in stderr:
        reason = "Auth failed. The key for this host is not accepted without a password."
    elif "timed out" in stderr:
        reason = "Timed out. The host did not answer in time."
    else:
        reason = "Unreachable. The host name or the network did not answer."
    return SourceStatus(where, PART_GPUS, STATE_FAILED, reason)


def _read_remote(run: Runner, alias: str) -> _Reading:
    result = run(ssh_argv(alias), SSH_TIMEOUT_SECONDS)
    if result.missing or result.timed_out or result.failed_to_start or result.returncode == 255:
        return _Reading(statuses=(_ssh_failure(alias, result),))
    sections = _sections(result.stdout)
    # The remote exit code belongs to the last command in the line, so the GPU query's own failure is read from its
    # stderr instead: "not found" means nvidia-smi is not on the host.
    gpus, gpu_status = _nvidia_gpu_status(alias, CommandResult(returncode=0, stdout=sections["gpu"]), remote=True)
    if not gpus and "not found" in result.stderr.lower():
        gpu_status = SourceStatus(alias, PART_GPUS, STATE_ABSENT, "nvidia-smi is not on this host, so its GPUs cannot be read.")
    elif not gpus and "nvidia-smi" in result.stderr.lower():
        gpu_status = SourceStatus(alias, PART_GPUS, STATE_FAILED, "nvidia-smi could not read the GPUs on this host.")
    apps_result = (
        CommandResult(returncode=0, stdout=sections["apps"]) if sections["apps_seen"] else CommandResult(None, missing=True)
    )
    apps, apps_status = _apps_status(alias, apps_result, gpus)
    slurm_result = _section_result(sections, "slurm")
    slurm, slurm_status = _scheduler_status(
        alias, PART_SLURM, slurm_result, lambda text, host: parse_squeue(text, host)
    )
    lsf_result = _section_result(sections, "lsf")
    lsf, lsf_status = _scheduler_status(alias, PART_LSF, lsf_result, lambda text, host: parse_bjobs(text, host))
    return _Reading(
        gpus=gpus,
        jobs=apps + slurm + lsf,
        statuses=(gpu_status, apps_status, slurm_status, lsf_status),
    )


def _sections(text: str) -> dict[str, Any]:
    """Split one remote reply at its marker lines. ``*_seen`` is True when the marker appeared, so a tool that is not
    on the host is told apart from one that printed nothing."""
    parts: dict[str, Any] = {"gpu": [], "apps": [], "slurm": [], "lsf": [], "apps_seen": False, "slurm_seen": False, "lsf_seen": False}
    current = "gpu"
    for line in text.splitlines():
        if line == APPS_MARKER:
            current = "apps"
            parts["apps_seen"] = True
        elif line == SLURM_MARKER:
            current = "slurm"
            parts["slurm_seen"] = True
        elif line == LSF_MARKER:
            current = "lsf"
            parts["lsf_seen"] = True
        else:
            parts[current].append(line)
    for key in ("gpu", "apps", "slurm", "lsf"):
        parts[key] = "\n".join(parts[key])
    return parts


def _section_result(sections: Mapping[str, Any], name: str) -> CommandResult | None:
    if not sections[f"{name}_seen"]:
        return None
    return CommandResult(returncode=0, stdout=sections[name])


# ----------------------------------------------------------------- collect


def _runpod_reading(runpod: Callable[[], Mapping[str, Any]] | None) -> _Reading:
    if runpod is None:
        return _Reading(statuses=(SourceStatus(SOURCE_RUNPOD, PART_RUNPOD, STATE_ABSENT, RUNPOD_NOT_CONNECTED_TEXT),))
    try:
        payload = runpod()
    except Exception:  # noqa: BLE001 - an unreachable server is named in plain words, never by class or value
        return _Reading(statuses=(SourceStatus(SOURCE_RUNPOD, PART_RUNPOD, STATE_ABSENT, RUNPOD_NOT_CONNECTED_TEXT),))
    if not isinstance(payload, Mapping) or payload.get("ok") is False:
        return _Reading(statuses=(SourceStatus(SOURCE_RUNPOD, PART_RUNPOD, STATE_FAILED, RUNPOD_NOT_LISTED_TEXT),))
    pods = parse_runpod_pods(payload)
    if not pods:
        return _Reading(statuses=(SourceStatus(SOURCE_RUNPOD, PART_RUNPOD, STATE_EMPTY, "No RunPod pods."),))
    noun = "pod" if len(pods) == 1 else "pods"
    return _Reading(jobs=pods, statuses=(SourceStatus(SOURCE_RUNPOD, PART_RUNPOD, STATE_OK, f"{len(pods)} RunPod {noun}."),))


def _failed_reading(where: str) -> _Reading:
    return _Reading(statuses=(SourceStatus(where, PART_GPUS, STATE_FAILED, "This could not be read."),))


def collect(
    run: Runner,
    hosts: Sequence[str],
    runpod: Callable[[], Mapping[str, Any]] | None = None,
    *,
    now: datetime | None = None,
) -> GpuSnapshot:
    """Read every part once and return one snapshot. Never raises: a part that fails says so in its status line.

    ``hosts`` are saved SSH host names, read in parallel. ``runpod`` returns the local server's pod list, or raises
    when that server is not answering. Passing None means RunPod is not connected.
    """
    checked_at = now if now is not None else datetime.now(timezone.utc)
    names = [name for name in hosts[:MAX_HOSTS] if check_alias(name)]
    readings: list[_Reading] = [_read_local(run)]
    if names:
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(names))) as pool:
            remote = list(pool.map(lambda alias: _guarded_remote(run, alias), names))
        readings.extend(remote)
    readings.append(_runpod_reading(runpod))
    gpus: list[GpuRow] = []
    jobs: list[GpuJob] = []
    statuses: list[SourceStatus] = []
    for reading in readings:
        gpus.extend(reading.gpus)
        jobs.extend(reading.jobs)
        statuses.extend(reading.statuses)
    return GpuSnapshot(checked_at=checked_at, gpus=tuple(gpus), jobs=tuple(jobs), statuses=tuple(statuses))


def _guarded_remote(run: Runner, alias: str) -> _Reading:
    try:
        return _read_remote(run, alias)
    except Exception:  # noqa: BLE001 - one bad host never stops the others
        return _failed_reading(alias)


def failed_snapshot(now: datetime | None = None) -> GpuSnapshot:
    """What the tab shows when a whole read failed before any part could answer."""
    checked_at = now if now is not None else datetime.now(timezone.utc)
    return GpuSnapshot(
        checked_at=checked_at,
        statuses=(SourceStatus(LOCAL_HOST, PART_GPUS, STATE_FAILED, "The GPU read failed. Press Refresh to try again."),),
    )


# ----------------------------------------------------------------- display text (pure)


def memory_text(used: int | None, total: int | None) -> str:
    if used is None or total is None:
        return "-"
    return f"{used} / {total} MiB"


def utilization_text(value: int | None) -> str:
    return "-" if value is None else f"{value}%"


def temperature_text(value: int | None) -> str:
    return "-" if value is None else f"{value} °C"


def power_text(draw: float | None, limit: float | None) -> str:
    if draw is None:
        return "-"
    if limit is None:
        return f"{draw:.0f} W"
    return f"{draw:.0f} / {limit:.0f} W"


def summary_text(snapshot: GpuSnapshot) -> str:
    """One line for the top of the tab: how many GPUs, how many are busy, and how much GPU memory is in use."""
    gpus = snapshot.gpus
    if not gpus:
        return "No GPUs were read."
    hosts = len({row.host for row in gpus})
    busy = sum(1 for row in gpus if row.utilization is not None and row.utilization > 0)
    used = sum(row.memory_used_mib or 0 for row in gpus)
    total = sum(row.memory_total_mib or 0 for row in gpus)
    gpu_noun = "GPU" if len(gpus) == 1 else "GPUs"
    host_noun = "host" if hosts == 1 else "hosts"
    return (
        f"{len(gpus)} {gpu_noun} on {hosts} {host_noun}. {busy} busy. "
        f"{used / 1024:.1f} of {total / 1024:.1f} GB GPU memory in use."
    )


def gpu_table_rows(snapshot: GpuSnapshot) -> list[tuple[str, ...]]:
    return [
        (
            row.host,
            row.index,
            row.name,
            utilization_text(row.utilization),
            memory_text(row.memory_used_mib, row.memory_total_mib),
            temperature_text(row.temperature_c),
            power_text(row.power_w, row.power_limit_w),
        )
        for row in snapshot.gpus
    ]


def job_table_rows(snapshot: GpuSnapshot) -> list[tuple[str, ...]]:
    return [
        (
            job.source,
            job.host,
            job.job_id,
            job.user or "-",
            job.name or "-",
            job.state or "-",
            job.gpu or "-",
            "-" if job.gpu_memory_mib is None else f"{job.gpu_memory_mib} MiB",
        )
        for job in snapshot.jobs
    ]


def status_table_rows(snapshot: GpuSnapshot) -> list[tuple[str, ...]]:
    return [(status.where, status.part, status.text) for status in snapshot.statuses]


__all__ = [
    "ALIAS_PATTERN",
    "APP_FIELDS",
    "BJOBS_ARGV",
    "GPU_FIELDS",
    "LOCAL_HOST",
    "MAX_HOSTS",
    "NVIDIA_APPS_ARGV",
    "NVIDIA_GPU_ARGV",
    "PART_GPUS",
    "PART_LSF",
    "PART_PROCESSES",
    "PART_RUNPOD",
    "PART_SLURM",
    "REMOTE_COMMAND",
    "SQUEUE_ARGV",
    "SOURCE_GPU_PROCESS",
    "SOURCE_LSF",
    "SOURCE_RUNPOD",
    "SOURCE_SLURM",
    "STATE_ABSENT",
    "STATE_EMPTY",
    "STATE_FAILED",
    "STATE_OK",
    "CommandResult",
    "GpuJob",
    "GpuRow",
    "GpuSnapshot",
    "Runner",
    "SourceStatus",
    "check_alias",
    "collect",
    "failed_snapshot",
    "gpu_table_rows",
    "job_table_rows",
    "parse_apps",
    "parse_bjobs",
    "parse_gpus",
    "parse_host_list",
    "parse_runpod_pods",
    "parse_squeue",
    "run_command",
    "ssh_argv",
    "status_table_rows",
    "summary_text",
]
