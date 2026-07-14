from __future__ import annotations

from types import SimpleNamespace

import idea_radar.fetcher as fetcher_module
from idea_radar.fetcher import InsaneSearchFetcher, _subprocess_creation_flags


def _skill_dir(tmp_path):
    skill_dir = tmp_path / "insane-search"
    engine_dir = skill_dir / "engine"
    engine_dir.mkdir(parents=True)
    (engine_dir / "__main__.py").write_text("", encoding="utf-8")
    return skill_dir


def test_windows_subprocess_creation_flag_hides_console_window() -> None:
    assert _subprocess_creation_flags("nt") == getattr(
        fetcher_module.subprocess, "CREATE_NO_WINDOW", 0x08000000
    )
    assert _subprocess_creation_flags("posix") == 0


def test_fetch_preserves_capture_diagnostics_and_timeout_with_creation_flags(tmp_path, monkeypatch) -> None:
    captured: dict[str, object] = {}
    stderr = "[engine] ok=True verdict=accepted profile=default attempts=2\ndiagnostic detail"

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(
            stdout="[BEGIN UNTRUSTED WEB CONTENT]\nresult body\n[END UNTRUSTED WEB CONTENT]",
            stderr=stderr,
            returncode=0,
        )

    monkeypatch.setattr(fetcher_module.subprocess, "run", fake_run)
    fetcher = InsaneSearchFetcher(skill_dir=_skill_dir(tmp_path), python_bin="python-test", timeout=17)

    result = fetcher.fetch("https://example.com")

    assert captured["command"] == [
        "python-test",
        "-m",
        "engine",
        "https://example.com",
        "--timeout",
        "17",
        "--device",
        "auto",
    ]
    assert captured["capture_output"] is True
    assert captured["timeout"] == 60
    assert captured["creationflags"] == _subprocess_creation_flags(fetcher_module.os.name)
    assert result.ok is True
    assert result.content == "result body"
    assert result.stderr == stderr
    assert result.verdict == "accepted"
    assert result.attempts == 2
