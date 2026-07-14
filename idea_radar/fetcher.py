from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_BOUNDARY_RE = re.compile(
    r"\[BEGIN UNTRUSTED WEB CONTENT\].*?\n(?P<body>.*?)\n\[END UNTRUSTED WEB CONTENT\]",
    re.DOTALL,
)
_ENGINE_STATUS_RE = re.compile(
    r"\[engine\]\s+ok=(?P<ok>True|False)\s+verdict=(?P<verdict>\S+)\s+profile=(?P<profile>\S+)\s+attempts=(?P<attempts>\d+)"
)
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def _subprocess_creation_flags(platform: str) -> int:
    """Hide console subprocess windows on Windows without changing I/O capture."""
    return _CREATE_NO_WINDOW if platform == "nt" else 0


@dataclass(slots=True)
class FetchResult:
    url: str
    ok: bool
    content: str
    stderr: str
    returncode: int
    verdict: str | None = None
    attempts: int | None = None
    skill_dir: str | None = None


class InsaneSearchFetcher:
    """Subprocess wrapper around the installed insane-search skill engine."""

    def __init__(
        self,
        skill_dir: str | Path | None = None,
        python_bin: str | None = None,
        timeout: int = 40,
        trace: bool = False,
    ) -> None:
        self.skill_dir = self.resolve_skill_dir(skill_dir)
        self.python_bin = python_bin or os.environ.get("INSANE_SEARCH_PYTHON") or self._default_python()
        self.timeout = timeout
        self.trace = trace

    @staticmethod
    def _default_python() -> str:
        return "python3" if os.name == "nt" else sys.executable

    @staticmethod
    def resolve_skill_dir(explicit: str | Path | None = None) -> Path:
        candidates: list[Path] = []
        if explicit:
            candidates.append(Path(explicit).expanduser())
        env_dir = os.environ.get("INSANE_SEARCH_DIR")
        if env_dir:
            candidates.append(Path(env_dir).expanduser())
        cwd = Path.cwd()
        home = Path.home()
        candidates.extend(
            [
                cwd / ".gjc" / "skills" / "insane-search",
                cwd / "insane-search" / "skills" / "insane-search",
                home / ".claude" / "skills" / "insane-search",
                home / ".gjc" / "skills" / "insane-search",
            ]
        )
        for candidate in candidates:
            if (candidate / "engine" / "__main__.py").exists():
                return candidate
        searched = ", ".join(str(c) for c in candidates)
        raise FileNotFoundError(f"insane-search skill not found. Searched: {searched}")

    def fetch(self, url: str, *, selectors: list[str] | None = None, device: str = "auto") -> FetchResult:
        cmd = [self.python_bin, "-m", "engine", url, "--timeout", str(self.timeout), "--device", device]
        for selector in selectors or []:
            cmd.extend(["--selector", selector])
        if self.trace:
            cmd.append("--trace")
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")
        proc = subprocess.run(
            cmd,
            cwd=str(self.skill_dir),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(self.timeout + 30, 60),
            creationflags=_subprocess_creation_flags(os.name),
        )
        content = self._extract_body(proc.stdout)
        ok, verdict, attempts = self._parse_status(proc.stderr)
        if ok is None:
            ok = proc.returncode == 0 and bool(content.strip())
        return FetchResult(
            url=url,
            ok=ok,
            content=content,
            stderr=proc.stderr,
            returncode=proc.returncode,
            verdict=verdict,
            attempts=attempts,
            skill_dir=str(self.skill_dir),
        )

    @staticmethod
    def _extract_body(stdout: str) -> str:
        match = _BOUNDARY_RE.search(stdout or "")
        if match:
            return match.group("body").strip()
        return (stdout or "").strip()

    @staticmethod
    def _parse_status(stderr: str) -> tuple[bool | None, str | None, int | None]:
        match = _ENGINE_STATUS_RE.search(stderr or "")
        if not match:
            return None, None, None
        return (
            match.group("ok") == "True",
            match.group("verdict"),
            int(match.group("attempts")),
        )
