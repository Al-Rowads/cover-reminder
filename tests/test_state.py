import sqlite3
import tempfile
import unittest
from pathlib import Path
from threading import Event

from cover_reminder.__main__ import healthcheck
from cover_reminder.config import Config, ConfigurationError
from cover_reminder.images import normalize_image
from cover_reminder.model import Reel
from cover_reminder.storage import Store, exclusive_worker
from cover_reminder.worker import Worker

FIXTURES = Path(__file__).parent / "fixtures"
HOUR = 3600


class StateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "state.sqlite3"
        self.store = Store(self.path)
        self.store.set("activated_at", 0)
        self.store.set("discovery_checkpoint", 0)
        self.baseline = normalize_image((FIXTURES / "hopper.jpg").read_bytes())
        self.changed = normalize_image((FIXTURES / "flower.jpg").read_bytes())
        # Local domain identifiers are never submitted to an API.
        self.reel = Reel("local-reel", "", 0, None)
        self.store.add(self.reel, 0)
        self.store.observe(self.reel.media_id, self.baseline, 0, 0.05)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def observe(self, hours, sample=None):
        return self.store.observe(
            self.reel.media_id, self.baseline if sample is None else sample, hours * HOUR, 0.05,
        )

    def due(self, hours):
        return self.store.queue_due(self.reel.media_id, hours * HOUR)

    def test_reminders_start_at_24_hours_and_finish_at_48(self):
        self.observe(24 - 1 / HOUR)
        self.assertIsNone(self.due(24 - 1 / HOUR))
        self.observe(24)
        self.assertEqual(self.due(24), 24)
        self.store.sent(self.reel.media_id, 24, 1, 24 * HOUR)
        self.observe(47)
        self.assertIsNone(self.due(47))
        self.observe(48)
        self.assertEqual(self.due(48), 48)
        self.store.sent(self.reel.media_id, 48, 2, 48 * HOUR)
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(self.store.summary()["reminders"], {"sent": 2})
        self.assertIsNone(self.due(72))

    def test_restart_preserves_sent_reminder_and_baseline(self):
        self.observe(24)
        self.due(24)
        self.store.sent(self.reel.media_id, 24, 1, 24 * HOUR)
        self.store.set("next_poll_at", 25 * HOUR)
        self.store.close()
        self.store = Store(self.path)
        self.observe(25)
        self.assertIsNone(self.due(25))
        self.assertEqual(self.store.get("next_poll_at"), 25 * HOUR)
        self.assertEqual(self.store.reel(self.reel.media_id)["baseline"], self.baseline)

    def test_unsent_reminder_is_retried_only_after_a_fresh_cover_check(self):
        self.observe(24)
        self.assertEqual(self.due(24), 24)
        self.assertIsNone(self.due(25))
        self.observe(25)
        self.assertEqual(self.due(25), 24)
        self.assertEqual(self.store.summary()["reminders"], {"pending": 1})

    def test_failed_cover_fetch_defers_due_reminder(self):
        self.observe(24)
        self.store.observation_failed(self.reel.media_id, "thumbnail:network_error")
        self.assertIsNone(self.due(24))
        self.observe(25)
        self.assertEqual(self.due(25), 24)

    def test_changed_cover_before_first_reminder_cancels_both(self):
        self.observe(23, self.changed)
        self.assertEqual(self.store.reel(self.reel.media_id)["change_streak"], 1)
        self.assertEqual(self.observe(24, self.changed)["status"], "changed")
        self.assertIsNone(self.due(24))
        self.assertIsNone(self.due(48))

    def test_changed_cover_after_first_reminder_cancels_second(self):
        self.observe(24)
        self.due(24)
        self.store.sent(self.reel.media_id, 24, 1, 24 * HOUR)
        self.observe(25, self.changed)
        self.observe(26, self.changed)
        self.assertIsNone(self.due(48))
        self.assertEqual(self.store.summary()["reminders"], {"sent": 1})

    def test_tentative_change_at_deadline_defers_delivery(self):
        self.observe(24, self.changed)
        self.assertIsNone(self.due(24))
        self.observe(25, self.baseline)
        self.assertEqual(self.due(25), 24)

    def test_error_breaks_change_confirmation(self):
        self.observe(22, self.changed)
        self.store.observation_failed(self.reel.media_id, "thumbnail:missing_thumbnail")
        self.assertEqual(self.observe(24, self.changed)["status"], "pending")
        self.assertEqual(self.observe(25, self.changed)["status"], "changed")

    def test_gap_breaks_change_confirmation(self):
        self.observe(20, self.changed)
        self.assertEqual(self.observe(24, self.changed)["status"], "pending")
        self.assertEqual(self.observe(25, self.changed)["status"], "changed")

    def test_repeating_same_observation_does_not_confirm_a_change(self):
        self.observe(23, self.changed)
        self.assertEqual(self.observe(23, self.changed)["change_streak"], 1)

    def test_original_baseline_survives_change_candidates(self):
        self.observe(23, self.changed)
        self.assertEqual(self.store.reel(self.reel.media_id)["baseline"], self.baseline)

    def test_threshold_change_does_not_reuse_an_invalid_candidate(self):
        def blend(fraction):
            return bytes(round(before * (1 - fraction) + after * fraction)
                         for before, after in zip(self.baseline, self.changed, strict=True))

        self.store.observe(self.reel.media_id, blend(0.2), 23 * HOUR, 0.05)
        self.assertEqual(self.store.reel(self.reel.media_id)["change_streak"], 1)
        observation = self.store.observe(self.reel.media_id, blend(0.3), 24 * HOUR, 0.08)
        self.assertEqual(observation["status"], "pending")
        self.assertEqual(observation["change_streak"], 1)

    def test_both_overdue_milestones_are_coalesced(self):
        self.observe(24)
        self.due(24)
        self.observe(60)
        self.assertEqual(self.due(60), 48)
        self.assertEqual(self.store.summary()["reminders"], {"pending": 1, "superseded": 1})

    def test_confirmed_change_supersedes_a_failed_delivery(self):
        self.observe(24)
        self.due(24)
        self.observe(25, self.changed)
        self.observe(26, self.changed)
        self.assertEqual(self.store.summary()["reminders"], {"superseded": 1})

    def test_overlapping_discovery_is_deduplicated(self):
        self.assertFalse(self.store.add(self.reel, HOUR))
        self.assertEqual(len(self.store.pending()), 1)

    def test_reels_before_activation_are_ignored(self):
        self.assertFalse(self.store.add(Reel("older-local-reel", "", -1, None), HOUR))

    def test_future_publication_time_does_not_enter_monitoring(self):
        with self.assertRaises(ValueError):
            self.store.add(Reel("future-local-reel", "", 301, None), 0)

    def test_duplicate_delivery_record_is_rejected(self):
        self.observe(24)
        self.due(24)
        self.store.sent(self.reel.media_id, 24, 1, 24 * HOUR)
        with self.assertRaises(ValueError):
            self.store.sent(self.reel.media_id, 24, 2, 25 * HOUR)

    def test_unknown_database_version_is_rejected(self):
        other = Path(self.directory.name) / "newer.sqlite3"
        with sqlite3.connect(other) as db:
            db.execute("PRAGMA user_version=99")
        with self.assertRaises(ConfigurationError):
            Store(other)

    def test_second_worker_cannot_acquire_the_lock(self):
        with exclusive_worker(self.path):
            with self.assertRaises(ConfigurationError):
                with exclusive_worker(self.path):
                    self.fail("second worker acquired the lock")

    def test_monitor_rejects_missing_live_verification(self):
        config = Config("", "", "me", "20260819_00", "", "", self.path)
        with self.assertRaises(ConfigurationError):
            self.store.activate(config, HOUR)

    def test_provider_cooldown_survives_restart(self):
        config = Config("", "", "me", "20260819_00", "", "", self.path)
        first = Worker(config, self.store, Event())
        first.http.cool_down("telegram", 2 * HOUR)
        until = first.http.cooldowns["telegram"]
        self.store.close()
        self.store = Store(self.path)
        second = Worker(config, self.store, Event())
        self.assertEqual(second.http.cooldowns["telegram"], until)

    def test_rate_limited_cycle_keeps_checkpoint_and_defers_notifications(self):
        config = Config("", "", "me", "20260819_00", "", "", self.path)
        worker = Worker(config, self.store, Event())
        # Exercise the real transport's cooldown guard; no HTTP request is made.
        worker.http.cool_down("composio", 2 * HOUR)
        worker.http.cool_down("telegram", 2 * HOUR)
        with self.assertLogs("cover_reminder.worker", level="ERROR"):
            self.assertFalse(worker.cycle(24 * HOUR))
        self.assertEqual(self.store.get("discovery_checkpoint"), 0)
        self.assertEqual(self.store.get("next_poll_at"), 25 * HOUR)
        self.assertFalse(self.store.get("last_cycle")["ok"])
        self.assertEqual(self.store.summary()["reminders"], {})

    def test_health_reports_failed_and_stale_polls(self):
        self.assertFalse(healthcheck(self.path, 100))
        self.store.set("last_cycle", {"ok": True, "completed_at": 100})
        self.assertTrue(healthcheck(self.path, 101))
        self.assertFalse(healthcheck(self.path, 100 + 2 * HOUR + 301))
        self.store.set("last_cycle", {"ok": False, "completed_at": 100})
        self.assertFalse(healthcheck(self.path, 101))

    def test_health_does_not_create_a_missing_database(self):
        missing = Path(self.directory.name) / "missing.sqlite3"
        self.assertFalse(healthcheck(missing, 100))
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
