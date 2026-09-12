import json
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
        self.store.apply_update(1, 101, True)
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
        self.store.sent(self.reel.media_id, 24, 101, 1, 24 * HOUR)
        self.observe(47)
        self.assertIsNone(self.due(47))
        self.observe(48)
        self.assertEqual(self.due(48), 48)
        self.store.sent(self.reel.media_id, 48, 101, 2, 48 * HOUR)
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(self.store.summary()["reminders"], {"sent": 2})
        self.assertIsNone(self.due(72))

    def test_restart_preserves_sent_reminder_and_baseline(self):
        self.observe(24)
        self.due(24)
        self.store.sent(self.reel.media_id, 24, 101, 1, 24 * HOUR)
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
        self.assertEqual(len(self.store.pending_reminders_for_delivery(24 * HOUR)), 1)
        self.assertIsNone(self.due(25))
        self.assertEqual(self.store.pending_reminders_for_delivery(25 * HOUR), [])
        self.observe(25)
        self.assertEqual(self.due(25), 24)
        self.assertEqual(len(self.store.pending_reminders_for_delivery(25 * HOUR)), 1)
        self.assertEqual(self.store.summary()["reminders"], {"pending": 1})

    def test_failed_cover_fetch_defers_due_reminder(self):
        self.observe(24)
        self.store.observation_failed(self.reel.media_id, "thumbnail:network_error")
        self.assertIsNone(self.due(24))
        self.observe(25)
        self.assertEqual(self.due(25), 24)

    def test_changed_cover_before_first_reminder_queues_alert_and_cancels_both(self):
        observation = self.observe(23, self.changed)
        self.assertEqual(observation["status"], "changed")
        self.assertEqual(self.store.reel(self.reel.media_id)["change_streak"], 1)
        self.assertEqual(self.store.pending_change_deliveries(self.reel.media_id), [101])
        self.assertEqual(self.store.summary()["cover_change_alerts"], {"pending": 1})
        self.assertIsNone(self.due(24))
        self.assertIsNone(self.due(48))

    def test_changed_cover_after_first_reminder_cancels_second(self):
        self.observe(24)
        self.due(24)
        self.store.sent(self.reel.media_id, 24, 101, 1, 24 * HOUR)
        self.observe(25, self.changed)
        self.assertIsNone(self.due(48))
        self.assertEqual(self.store.summary()["reminders"], {"sent": 1})
        self.assertEqual(self.store.pending_change_deliveries(self.reel.media_id), [101])

    def test_below_threshold_variation_does_not_count_as_a_change(self):
        sample = bytes(round(before * 0.99 + after * 0.01)
                       for before, after in zip(self.baseline, self.changed, strict=True))
        observation = self.observe(23, sample)
        self.assertEqual(observation["status"], "pending")
        self.assertEqual(observation["change_streak"], 0)
        self.assertEqual(self.store.summary()["cover_change_alerts"], {})

    def test_original_baseline_survives_detected_change(self):
        self.observe(23, self.changed)
        self.assertEqual(self.store.reel(self.reel.media_id)["baseline"], self.baseline)

    def test_threshold_is_applied_to_each_observation(self):
        observation = self.store.observe(self.reel.media_id, self.changed, 23 * HOUR, 1.0)
        self.assertEqual(observation["status"], "pending")
        observation = self.store.observe(self.reel.media_id, self.changed, 24 * HOUR, 0.05)
        self.assertEqual(observation["status"], "changed")
        self.assertEqual(observation["change_streak"], 1)

    def test_both_overdue_milestones_are_coalesced(self):
        self.observe(24)
        self.due(24)
        self.observe(60)
        self.assertEqual(self.due(60), 48)
        self.assertEqual(self.store.summary()["reminders"], {"pending": 1, "superseded": 1})

    def test_detected_change_wins_at_first_successful_check_after_final_deadline(self):
        observation = self.observe(60, self.changed)
        self.assertEqual(observation["status"], "changed")
        self.assertIsNone(self.due(60))
        self.assertEqual(self.store.summary()["reminders"], {})
        self.assertEqual(self.store.pending_change_deliveries(self.reel.media_id), [101])

    def test_detected_change_supersedes_a_failed_delivery(self):
        self.observe(24)
        self.due(24)
        self.observe(25, self.changed)
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
        self.store.sent(self.reel.media_id, 24, 101, 1, 24 * HOUR)
        with self.assertRaises(ValueError):
            self.store.sent(self.reel.media_id, 24, 101, 2, 25 * HOUR)

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

    def test_monitor_activates_without_live_verification(self):
        config = Config("", "", "me", "20260819_00", "", self.path, "")
        self.store.activate(config, HOUR)
        self.assertEqual(self.store.get("monitor_identity"), config.identity())

    def test_monitor_still_rejects_a_different_instagram_account(self):
        config = Config("", "first", "me", "20260819_00", "", self.path, "user")
        self.store.activate(config, HOUR)
        other = Config("", "second", "me", "20260819_00", "", self.path, "user")
        with self.assertRaises(ConfigurationError):
            self.store.activate(other, 2 * HOUR)

    def test_provider_cooldown_survives_restart(self):
        config = Config("", "", "me", "20260819_00", "", self.path, "")
        first = Worker(config, self.store, Event())
        first.http.cool_down("telegram", 2 * HOUR)
        until = first.http.cooldowns["telegram"]
        self.store.close()
        self.store = Store(self.path)
        second = Worker(config, self.store, Event())
        self.assertEqual(second.http.cooldowns["telegram"], until)

    def test_rate_limited_cycle_keeps_checkpoint_and_defers_notifications(self):
        config = Config("", "", "me", "20260819_00", "", self.path, "")
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
        self.store.set("telegram_updates", {"ok": True, "checked_at": 100})
        self.store.set("last_cycle", {"ok": True, "completed_at": 100})
        self.assertTrue(healthcheck(self.path, 101))
        self.assertFalse(healthcheck(self.path, 100 + 2 * HOUR + 301))
        self.store.set("last_cycle", {"ok": False, "completed_at": 100})
        self.assertFalse(healthcheck(self.path, 101))

    def test_health_does_not_create_a_missing_database(self):
        missing = Path(self.directory.name) / "missing.sqlite3"
        self.assertFalse(healthcheck(missing, 100))
        self.assertFalse(missing.exists())

    def test_subscription_and_offset_survive_restart_and_repeated_start(self):
        self.store.apply_update(2, 101, True)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.subscribers(), [101])
        self.assertEqual(self.store.get("telegram_update_offset"), 3)
        self.store.apply_update(3, 101, False)
        self.assertFalse(self.store.apply_update(2, 101, True))
        self.assertEqual(self.store.subscribers(), [])
        self.store.apply_update(4, 101, True)
        self.assertEqual(self.store.subscribers(), [101])

    def test_subscription_failure_does_not_advance_offset(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.apply_update(2, -1, True)
        self.assertEqual(self.store.get("telegram_update_offset"), 2)
        self.assertEqual(self.store.subscribers(), [101])

    def test_ignored_update_advances_offset_without_subscribing(self):
        self.store.apply_update(2)
        self.assertEqual(self.store.get("telegram_update_offset"), 3)
        self.assertEqual(self.store.subscribers(), [101])

    def test_update_id_can_reset_after_a_quiet_period(self):
        self.store.apply_update(1000, 102, True, received_at=0)
        self.assertTrue(self.store.apply_update(10, 103, True, received_at=8 * 86400))
        self.assertEqual(self.store.get("telegram_update_offset"), 11)
        self.assertEqual(self.store.subscribers(), [101, 102, 103])
        self.assertFalse(self.store.apply_update(10, 103, True, received_at=8 * 86400 + 1))

    def test_old_update_receipt_does_not_suppress_a_reused_id(self):
        self.store.apply_update(1000, 102, True, received_at=0)
        self.store.deactivate(102)
        self.assertTrue(self.store.apply_update(1000, 102, True, received_at=8 * 86400))
        self.assertEqual(self.store.subscribers(), [101, 102])

    def test_partial_broadcast_retries_only_unsent_recipient_after_restart(self):
        self.store.apply_update(2, 102, True)
        self.observe(24)
        self.assertEqual(self.due(24), 24)
        self.assertEqual(self.store.pending_deliveries(self.reel.media_id, 24), [101, 102])
        self.store.sent(self.reel.media_id, 24, 101, 1, 24 * HOUR)
        self.store.close()
        self.store = Store(self.path)
        self.assertIsNone(self.due(25))
        self.observe(25)
        self.assertEqual(self.due(25), 24)
        self.assertEqual(self.store.pending_deliveries(self.reel.media_id, 24), [102])
        self.store.sent(self.reel.media_id, 24, 102, 1, 25 * HOUR)
        self.assertEqual(self.store.summary()["reminders"], {"sent": 1})
        self.assertEqual(self.store.summary()["deliveries"], {"sent": 2})

    def test_final_cover_check_completes_before_all_deliveries_are_terminal(self):
        self.store.apply_update(2, 102, True)
        self.observe(48)
        self.due(48)
        self.assertEqual(self.store.pending(), [])
        self.store.sent(self.reel.media_id, 48, 101, 1, 48 * HOUR)
        self.assertEqual(len(self.store.pending_reminders_for_delivery(49 * HOUR)), 1)
        self.store.deactivate(102)
        self.assertEqual(self.store.pending_reminders_for_delivery(49 * HOUR), [])
        self.assertEqual(self.store.summary()["deliveries"], {"sent": 1, "cancelled": 1})

    def test_new_subscriber_gets_next_broadcast_for_existing_reel(self):
        self.observe(24)
        self.due(24)
        self.store.apply_update(2, 102, True)
        self.assertEqual(self.store.pending_deliveries(self.reel.media_id, 24), [101])
        self.store.sent(self.reel.media_id, 24, 101, 1, 24 * HOUR)
        self.observe(25)
        self.assertIsNone(self.due(25))
        self.observe(48)
        self.due(48)
        self.assertEqual(self.store.pending_deliveries(self.reel.media_id, 48), [101, 102])

    def test_unsubscribe_cancels_pending_deliveries_and_start_does_not_reopen_them(self):
        self.store.apply_update(2, 102, True)
        self.observe(24)
        self.due(24)
        self.store.apply_update(3, 101, False)
        self.store.apply_update(4, 101, True)
        self.assertEqual(self.store.pending_deliveries(self.reel.media_id, 24), [102])
        self.assertFalse(self.store.delivery_pending(self.reel.media_id, 24, 101))
        with self.assertRaises(ValueError):
            self.store.sent(self.reel.media_id, 24, 101, 1, 24 * HOUR)
        self.observe(48)
        self.due(48)
        self.assertEqual(self.store.pending_deliveries(self.reel.media_id, 48), [101, 102])

    def test_empty_broadcast_is_not_replayed_and_final_milestone_completes(self):
        self.store.deactivate(101)
        self.observe(24)
        self.assertIsNone(self.due(24))
        self.store.apply_update(2, 102, True)
        self.observe(25)
        self.assertIsNone(self.due(25))
        self.store.deactivate(102)
        self.observe(48)
        self.assertIsNone(self.due(48))
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(self.store.summary()["reminders"], {"superseded": 2})

    def test_cover_change_cancels_only_unsent_recipients(self):
        self.store.apply_update(2, 102, True)
        self.observe(24)
        self.due(24)
        self.store.sent(self.reel.media_id, 24, 101, 1, 24 * HOUR)
        self.observe(25, self.changed)
        self.assertEqual(self.store.pending_deliveries(self.reel.media_id, 24), [])
        self.assertEqual(self.store.summary()["deliveries"], {"sent": 1, "cancelled": 1})

    def test_change_alert_partial_delivery_survives_restart(self):
        self.store.apply_update(2, 102, True)
        self.observe(23, self.changed)
        self.assertEqual(self.store.pending_change_deliveries(self.reel.media_id), [101, 102])
        self.store.change_sent(self.reel.media_id, 101, 11, 23 * HOUR)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.pending_change_deliveries(self.reel.media_id), [102])
        self.store.change_sent(self.reel.media_id, 102, 12, 24 * HOUR)
        self.assertEqual(self.store.summary()["cover_change_alerts"], {"sent": 1})
        self.assertEqual(self.store.summary()["cover_change_deliveries"], {"sent": 2})
        with self.assertRaises(ValueError):
            self.store.change_sent(self.reel.media_id, 102, 13, 25 * HOUR)

    def test_change_alert_snapshot_excludes_later_subscribers(self):
        self.observe(23, self.changed)
        self.store.apply_update(2, 102, True)
        self.assertEqual(self.store.pending_change_deliveries(self.reel.media_id), [101])

    def test_unsubscribe_cancels_change_alert_without_reopening_it(self):
        self.store.apply_update(2, 102, True)
        self.observe(23, self.changed)
        self.store.apply_update(3, 101, False)
        self.store.apply_update(4, 101, True)
        self.assertEqual(self.store.pending_change_deliveries(self.reel.media_id), [102])
        self.store.change_sent(self.reel.media_id, 102, 12, 24 * HOUR)
        self.assertEqual(self.store.summary()["cover_change_deliveries"], {"sent": 1, "cancelled": 1})

    def test_change_without_subscribers_is_not_replayed(self):
        self.store.deactivate(101)
        self.observe(23, self.changed)
        self.assertEqual(self.store.pending_change_alerts(), [])
        self.store.apply_update(2, 102, True)
        self.assertEqual(self.store.pending_change_deliveries(self.reel.media_id), [])
        self.assertEqual(self.store.summary()["cover_change_alerts"], {"superseded": 1})

    def test_overdue_final_broadcast_cancels_unsent_first_reminder(self):
        self.store.apply_update(2, 102, True)
        self.observe(24)
        self.due(24)
        self.store.sent(self.reel.media_id, 24, 101, 1, 24 * HOUR)
        self.observe(49)
        self.assertEqual(self.due(49), 48)
        self.assertEqual(self.store.pending_deliveries(self.reel.media_id, 24), [])
        self.assertEqual(self.store.pending_deliveries(self.reel.media_id, 48), [101, 102])

    def test_bot_identity_survives_restart_and_rejects_a_different_bot(self):
        self.store.bind_bot(501)
        self.store.close()
        self.store = Store(self.path)
        self.store.bind_bot(501)
        with self.assertRaises(ConfigurationError):
            self.store.bind_bot(502)
        self.assertEqual(self.store.get("telegram_bot_id"), 501)

    def test_migration_preserves_legacy_history_without_subscribing_destination(self):
        config = Config("", "", "me", "20260819_00", "", self.path, "")
        self.store.set("monitor_identity", {**config.identity(), "telegram_chat_id": "101"})
        self.store.set("next_poll_at", 25 * HOUR)
        self.observe(24)
        self.due(24)
        self.store.close()
        # Recreate the prior application's schema and actual legacy reminder fields.
        with sqlite3.connect(self.path) as db:
            db.execute("DROP TABLE cover_change_deliveries")
            db.execute("DROP TABLE cover_change_alerts")
            db.execute("DROP TABLE deliveries")
            db.execute("DROP TABLE subscribers")
            db.execute("ALTER TABLE reminders DROP COLUMN audience_captured")
            db.execute("UPDATE reminders SET status='sent', message_id=7, sent_at=?", (24 * HOUR,))
            db.execute("DELETE FROM settings WHERE key='telegram_update_offset'")
            db.execute("DELETE FROM settings WHERE key='telegram_recent_updates'")
            db.execute("PRAGMA user_version=1")
        self.store = Store(self.path)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 3)
        self.assertEqual(self.store.db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.store.activate(config, 25 * HOUR)
        self.assertEqual(self.store.subscribers(), [])
        self.assertEqual(self.store.get("monitor_identity"), config.identity())
        self.assertEqual(self.store.get("activated_at"), 0)
        self.assertEqual(self.store.get("next_poll_at"), 25 * HOUR)
        self.assertEqual(self.store.reel(self.reel.media_id)["baseline"], self.baseline)
        self.assertEqual(self.store.db.execute("SELECT message_id FROM reminders").fetchone()[0], 7)
        self.store.apply_update(1, 102, True)
        self.observe(25)
        self.assertIsNone(self.due(25))
        self.observe(48)
        self.assertEqual(self.due(48), 48)
        self.assertEqual(self.store.pending_deliveries(self.reel.media_id, 48), [102])

    def test_failed_migration_rolls_back_schema_changes(self):
        self.store.close()
        with sqlite3.connect(self.path) as db:
            db.execute("DROP TABLE cover_change_deliveries")
            db.execute("DROP TABLE cover_change_alerts")
            db.execute("DROP TABLE deliveries")
            db.execute("DROP TABLE subscribers")
            db.execute("ALTER TABLE reminders DROP COLUMN audience_captured")
            db.execute("INSERT INTO settings VALUES ('monitor_identity', ?)", ("invalid json",))
            db.execute("PRAGMA user_version=1")
        with self.assertRaises(json.JSONDecodeError):
            Store(self.path)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertNotIn("audience_captured", [row[1] for row in db.execute("PRAGMA table_info(reminders)")])
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='subscribers'").fetchone())
            self.assertIsNone(db.execute(
                "SELECT name FROM sqlite_master WHERE name='cover_change_alerts'"
            ).fetchone())
            db.execute("DELETE FROM settings WHERE key='monitor_identity'")
        self.store = Store(self.path)

    def test_version_two_database_adds_change_alert_tables(self):
        self.store.close()
        with sqlite3.connect(self.path) as db:
            db.execute("DROP TABLE cover_change_deliveries")
            db.execute("DROP TABLE cover_change_alerts")
            db.execute("PRAGMA user_version=2")
        self.store = Store(self.path)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 3)
        self.assertIsNotNone(self.store.db.execute(
            "SELECT name FROM sqlite_master WHERE name='cover_change_alerts'"
        ).fetchone())

    def test_update_failure_preserves_offset_and_reports_unhealthy(self):
        config = Config("", "", "me", "20260819_00", "", self.path, "")
        worker = Worker(config, self.store, Event())
        worker.http.cool_down("telegram", HOUR)
        with self.assertLogs("cover_reminder.worker", level="ERROR"):
            self.assertFalse(worker.sync_updates())
        self.assertEqual(self.store.get("telegram_update_offset"), 2)
        self.assertEqual(self.store.subscribers(), [101])
        self.store.set("last_cycle", {"ok": True, "completed_at": 100})
        self.assertFalse(healthcheck(self.path, 101))

    def test_health_requires_recent_successful_update_poll(self):
        self.store.set("last_cycle", {"ok": True, "completed_at": 100})
        self.assertFalse(healthcheck(self.path, 101))
        self.store.set("telegram_updates", {"ok": True, "checked_at": 100})
        self.assertTrue(healthcheck(self.path, 101))
        self.store.set("telegram_updates", {"ok": False, "checked_at": 100})
        self.assertFalse(healthcheck(self.path, 101))


if __name__ == "__main__":
    unittest.main()
