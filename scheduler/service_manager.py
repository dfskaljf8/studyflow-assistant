import os
import plistlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from config.settings import settings

SERVICE_LABEL = "com.studyflow.assistant"
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"


@dataclass
class ServiceInfo:
    label: str
    domain: str
    service_target: str
    plist_path: Path
    python_path: Path
    project_root: Path
    stdout_log_path: Path
    stderr_log_path: Path


def _resolve_python_path() -> Path:
    venv_python = settings.project_root / "venv" / "bin" / "python"
    if venv_python.exists():
        return venv_python
    return Path(sys.executable)


def get_service_info() -> ServiceInfo:
    project_root = settings.project_root
    log_dir = project_root / "service_logs"
    domain = f"gui/{os.getuid()}"
    return ServiceInfo(
        label=SERVICE_LABEL,
        domain=domain,
        service_target=f"{domain}/{SERVICE_LABEL}",
        plist_path=LAUNCH_AGENTS_DIR / f"{SERVICE_LABEL}.plist",
        python_path=_resolve_python_path(),
        project_root=project_root,
        stdout_log_path=log_dir / "launchd.out.log",
        stderr_log_path=log_dir / "launchd.err.log",
    )


def _ensure_paths(info: ServiceInfo) -> None:
    LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    info.stdout_log_path.parent.mkdir(parents=True, exist_ok=True)


def _run_launchctl(args: list[str], allow_failure: bool = False) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(["launchctl", *args], capture_output=True, text=True)
    if proc.returncode != 0 and not allow_failure:
        details = (proc.stderr or proc.stdout).strip() or f"exit={proc.returncode}"
        raise RuntimeError(f"launchctl {' '.join(args)} failed: {details}")
    return proc


def _ok_to_ignore_bootstrap_error(output: str) -> bool:
    text = output.lower()
    return "already loaded" in text or "in progress" in text


def _ok_to_ignore_bootout_error(output: str) -> bool:
    text = output.lower()
    return "could not find service" in text or "no such process" in text or "not found" in text


def _is_service_loaded(service_target: str) -> bool:
    probe = _run_launchctl(["print", service_target], allow_failure=True)
    return probe.returncode == 0


def _build_plist_payload(info: ServiceInfo) -> dict:
    wrapper = info.project_root / "scheduler" / "run_awake.sh"
    return {
        "Label": info.label,
        "ProgramArguments": ["/bin/bash", str(wrapper)],
        "WorkingDirectory": str(info.project_root),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": str(info.stdout_log_path),
        "StandardErrorPath": str(info.stderr_log_path),
        "EnvironmentVariables": {
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": str(info.project_root),
            "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin",
        },
    }


def write_launch_agent_plist() -> Path:
    info = get_service_info()
    _ensure_paths(info)

    with info.plist_path.open("wb") as fh:
        plistlib.dump(_build_plist_payload(info), fh, sort_keys=False)

    return info.plist_path


def install_service(start: bool = True) -> ServiceInfo:
    info = get_service_info()
    write_launch_agent_plist()

    bootout = _run_launchctl(["bootout", info.service_target], allow_failure=True)
    bootout_output = f"{bootout.stdout}\n{bootout.stderr}".strip()
    if bootout.returncode != 0 and bootout_output and not _ok_to_ignore_bootout_error(bootout_output):
        raise RuntimeError(f"Failed to unload existing service before install: {bootout_output}")

    bootstrap = _run_launchctl(["bootstrap", info.domain, str(info.plist_path)], allow_failure=True)
    bootstrap_output = f"{bootstrap.stdout}\n{bootstrap.stderr}".strip()
    if (
        bootstrap.returncode != 0
        and not _ok_to_ignore_bootstrap_error(bootstrap_output)
        and not _is_service_loaded(info.service_target)
    ):
        raise RuntimeError(f"Failed to bootstrap service: {bootstrap_output}")

    _run_launchctl(["enable", info.service_target], allow_failure=True)
    if start:
        kickstart = _run_launchctl(["kickstart", "-k", info.service_target], allow_failure=True)
        kickstart_output = f"{kickstart.stdout}\n{kickstart.stderr}".strip()
        if kickstart.returncode != 0:
            raise RuntimeError(f"Failed to start service: {kickstart_output}")

    return info


def start_service() -> ServiceInfo:
    info = get_service_info()
    if not info.plist_path.exists():
        raise RuntimeError("Service plist not found. Run 'python main.py service-install' first.")

    _run_launchctl(["enable", info.service_target], allow_failure=True)

    kickstart = _run_launchctl(["kickstart", "-k", info.service_target], allow_failure=True)
    kickstart_output = f"{kickstart.stdout}\n{kickstart.stderr}".strip()
    if kickstart.returncode == 0:
        return info

    bootstrap = _run_launchctl(["bootstrap", info.domain, str(info.plist_path)], allow_failure=True)
    bootstrap_output = f"{bootstrap.stdout}\n{bootstrap.stderr}".strip()
    if (
        bootstrap.returncode != 0
        and not _ok_to_ignore_bootstrap_error(bootstrap_output)
        and not _is_service_loaded(info.service_target)
    ):
        raise RuntimeError(f"Failed to bootstrap service: {bootstrap_output}")

    kickstart = _run_launchctl(["kickstart", "-k", info.service_target], allow_failure=True)
    kickstart_output = f"{kickstart.stdout}\n{kickstart.stderr}".strip()
    if kickstart.returncode != 0:
        raise RuntimeError(f"Failed to start service: {kickstart_output}")

    return info


def stop_service() -> ServiceInfo:
    info = get_service_info()
    proc = _run_launchctl(["bootout", info.service_target], allow_failure=True)
    output = f"{proc.stdout}\n{proc.stderr}".strip()
    if proc.returncode != 0 and output and not _ok_to_ignore_bootout_error(output):
        raise RuntimeError(f"Failed to stop service: {output}")
    return info


def uninstall_service() -> ServiceInfo:
    info = stop_service()
    if info.plist_path.exists():
        info.plist_path.unlink()
    return info


def service_status() -> dict:
    info = get_service_info()
    proc = _run_launchctl(["print", info.service_target], allow_failure=True)

    loaded = proc.returncode == 0
    state = "not_loaded"
    raw_output = f"{proc.stdout}\n{proc.stderr}".strip()

    if loaded:
        state_match = re.search(r"\bstate\s*=\s*(\w+)", raw_output)
        if state_match:
            state = state_match.group(1)
        else:
            state = "loaded"

    return {
        "label": info.label,
        "domain": info.domain,
        "service_target": info.service_target,
        "plist_path": str(info.plist_path),
        "stdout_log_path": str(info.stdout_log_path),
        "stderr_log_path": str(info.stderr_log_path),
        "loaded": loaded,
        "state": state,
        "raw_output": raw_output,
    }
