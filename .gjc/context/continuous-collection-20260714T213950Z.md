# Continuous collection delivery context

## Task statement
Make the desktop Product Idea Radar keep collecting repeatedly after the user starts it, until the user explicitly stops it. Eliminate the repeated visible terminal-window flashes so the app can run while the user does other work.

## Desired outcome
- One Start action begins a durable in-app collection loop.
- Collection cycles continue automatically until Stop or app close.
- The UI exposes a clear Stop control and truthful running/stopping status.
- Stop prevents new cycles and cleanly lets any active cycle settle without corrupting artifacts.
- Child Python/engine processes launched by the frozen Windows GUI do not create visible console windows.
- Errors are logged and surfaced without permanently killing the continuous loop unless cancellation/app shutdown requires it.
- Tests cover lifecycle state, repeated scheduling, stop behavior, and Windows no-console subprocess flags.
- Rebuilt desktop executable is installed and smoke-tested.

## Known facts and evidence
- `idea_radar/gui.py::_start_crawl` starts exactly one daemon thread and `_on_crawl_done` always resets `_running` and re-enables Start; there is no repeat loop or Stop button.
- `idea_radar/fetcher.py::fetch` calls `subprocess.run` for the insane-search engine without Windows `CREATE_NO_WINDOW` / startup-info suppression, which can flash console windows from the windowed executable.
- `run_gui.pyw` is packaged as a windowed PyInstaller executable and keeps collection in-process.
- Existing full suite currently passes: `python -m unittest discover -s tests` => 41 tests OK.
- Existing live acceptance evidence reports 37 verified real-time ideas and all required Threads lanes attempted.

## Constraints
- Windows 11 desktop delivery; no visible terminal churn.
- Preserve fail-closed verification, dedupe, safety traces, and existing output paths.
- Do not overlap collection cycles.
- Keep Tk mutations on the Tk main thread.
- Do not use forceful thread termination.
- Existing user work and artifacts must not be deleted.

## Unknowns / open questions
- Appropriate default delay between completed cycles; choose a conservative explicit UI setting or documented fixed delay with responsive cancellation.
- Whether a failed cycle should retry immediately or after bounded delay; avoid hot retry loops.
- Frozen Python child-process executable resolution must remain compatible with the installed insane-search engine.

## Likely codebase touchpoints
- `idea_radar/gui.py`
- `idea_radar/fetcher.py`
- `tests/test_social_smoke.py` and/or a focused GUI/fetcher test module
- `ProductIdeaRadar.spec`
- desktop artifact `C:/Users/imda0/Desktop/제품 아이디어 레이더.exe`
