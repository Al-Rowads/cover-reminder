import unittest

from cover_reminder.subscriptions import subscription_command


class SubscriptionCommandTests(unittest.TestCase):
    def test_start_including_deep_link_and_addressed_command(self):
        for text in ("/start", "/start campaign", "/start@CoverBot", "/start@coverbot campaign"):
            with self.subTest(text=text):
                self.assertIs(subscription_command(text, "CoverBot"), True)

    def test_stop(self):
        for text in ("/stop", "/stop@CoverBot"):
            with self.subTest(text=text):
                self.assertIs(subscription_command(text, "CoverBot"), False)

    def test_other_text_does_not_change_subscription(self):
        for text in ("", "hello", "/starting", "please /start", "/start@AnotherBot", "/stop@", "/help"):
            with self.subTest(text=text):
                self.assertIsNone(subscription_command(text, "CoverBot"))
