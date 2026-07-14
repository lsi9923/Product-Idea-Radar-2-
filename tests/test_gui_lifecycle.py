from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from idea_radar.gui import COLLECTION_INTERVAL_MS, GUI_DISPLAY_LIMIT, IdeaRadarApp


class GuiLifecycleTests(unittest.TestCase):
    def make_app(self, *, running: bool = True, cycle_active: bool = False) -> IdeaRadarApp:
        app = object.__new__(IdeaRadarApp)
        app._running = running
        app._cycle_active = cycle_active
        app._closing = False
        app._repeat_after_id = None
        app._crawl_options = object()
        app._crawl_thread = object()
        app.start_btn = SimpleNamespace(configure=Mock())
        app.stop_btn = SimpleNamespace(configure=Mock())
        app.after = Mock(return_value="after-1")
        app.after_cancel = Mock()
        app.destroy = Mock()
        app._append_log = Mock()
        app._set_status = Mock()
        app._populate_tree = Mock()
        return app

    def test_successful_cycle_schedules_exactly_one_later_cycle(self) -> None:
        app = self.make_app(cycle_active=True)
        result = SimpleNamespace(ideas=[], errors={})

        app._on_crawl_done(result, None)

        self.assertFalse(app._cycle_active)
        self.assertIsNone(app._crawl_thread)
        app.after.assert_called_once_with(COLLECTION_INTERVAL_MS, app._begin_crawl_cycle)
        self.assertEqual("after-1", app._repeat_after_id)
        app._populate_tree.assert_called_once_with([], top=GUI_DISPLAY_LIMIT)

    def test_begin_cycle_does_not_overlap_an_active_cycle(self) -> None:
        app = self.make_app(cycle_active=True)

        app._begin_crawl_cycle()

        app.after.assert_not_called()
        self.assertTrue(app._cycle_active)

    def test_stop_cancels_pending_cycle_and_restores_idle_controls(self) -> None:
        app = self.make_app(cycle_active=False)
        app._repeat_after_id = "after-existing"

        app._stop_crawl()

        self.assertFalse(app._running)
        self.assertIsNone(app._repeat_after_id)
        self.assertIsNone(app._crawl_options)
        app.after_cancel.assert_called_once_with("after-existing")
        app.start_btn.configure.assert_called_with(state="normal", text="▶  수집 시작")
        app.stop_btn.configure.assert_called_with(state="disabled")
        app._set_status.assert_called_with("수집 중지됨")

    def test_stop_waits_for_active_cycle_without_scheduling_another(self) -> None:
        app = self.make_app(cycle_active=True)

        app._stop_crawl()
        self.assertFalse(app._running)
        self.assertTrue(app._cycle_active)
        app._set_status.assert_called_with("중지 중 — 현재 수집 완료 대기", running=True)

        app._on_crawl_done(SimpleNamespace(ideas=[], errors={}), None)

        app.after.assert_not_called()
        self.assertIsNone(app._crawl_options)
        app.start_btn.configure.assert_called_with(state="normal", text="▶  수집 시작")

    def test_cycle_error_is_logged_and_retried_after_bounded_delay(self) -> None:
        app = self.make_app(cycle_active=True)
        error = RuntimeError("engine failed")

        app._on_crawl_done(None, error)

        app._append_log.assert_called_once_with("[실패] engine failed")
        app.after.assert_called_once_with(COLLECTION_INTERVAL_MS, app._begin_crawl_cycle)
        app._set_status.assert_called_with(
            f"오류 — {COLLECTION_INTERVAL_MS // 1_000}초 후 재시도",
            running=True,
        )
        self.assertTrue(app._running)

    def test_close_waits_for_active_cycle_then_destroys_without_repeat(self) -> None:
        app = self.make_app(cycle_active=True)

        app._on_close()

        self.assertTrue(app._closing)
        self.assertFalse(app._running)
        app.destroy.assert_not_called()
        app._set_status.assert_called_with("종료 중 — 현재 수집 완료 대기", running=True)

        app._on_crawl_done(None, RuntimeError("closing"))

        app.destroy.assert_called_once_with()
        app.after.assert_not_called()


if __name__ == "__main__":
    unittest.main()
