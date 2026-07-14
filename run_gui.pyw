"""바탕화면/더블클릭용 GUI 런처 (콘솔 창 없음)."""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
ROOT = Path(sys.executable).resolve().parent if FROZEN else BUNDLE_ROOT
APP_ROOT = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ProductIdeaRadar" if FROZEN else ROOT
LOG_FILE = APP_ROOT / "gui_error.log"
APP_ROOT.mkdir(parents=True, exist_ok=True)


def _write_log(message: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        LOG_FILE.write_text(message, encoding="utf-8")
    except Exception:
        pass


def _show_error(message: str) -> None:
    _write_log(message)
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Product Idea Radar",
            f"{message}\n\n자세한 내용: {LOG_FILE}",
        )
        root.destroy()
    except Exception:
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, message, "Product Idea Radar", 0x10)
        except Exception:
            pass


def main() -> int:
    os.chdir(APP_ROOT)
    if str(BUNDLE_ROOT) not in sys.path:
        sys.path.insert(0, str(BUNDLE_ROOT))

    if len(sys.argv) > 1:
        try:
            from idea_radar.cli import main as cli_main

            cli_args = sys.argv[1:]
            if cli_args and cli_args[0].startswith("-"):
                cli_args = ["crawl", *cli_args]
            return cli_main(cli_args)
        except Exception:
            _show_error(f"CLI 실행 오류:\n\n{traceback.format_exc()}")
            return 1

    try:
        import tkinter as tk  # noqa: F401 — tkinter 설치 여부 먼저 확인
    except ImportError:
        _show_error("tkinter가 없습니다.\nPython 설치 시 tcl/tk 옵션을 포함해야 합니다.")
        return 1

    try:
        from idea_radar.gui import main as gui_main

        if LOG_FILE.exists():
            LOG_FILE.unlink()
        return gui_main()
    except Exception:
        _show_error(f"실행 오류:\n\n{traceback.format_exc()}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
