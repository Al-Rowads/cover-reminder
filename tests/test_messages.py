import unittest

from cover_reminder.worker import (
    SUBSCRIBED_MESSAGE,
    TEST_MESSAGE,
    UNSUBSCRIBED_MESSAGE,
    reminder_message,
)


class MessageTests(unittest.TestCase):
    def test_subscription_messages_are_formal_persian(self):
        self.assertEqual(
            SUBSCRIBED_MESSAGE,
            "برای دریافت یادآوری‌های کاور عضو شدید. برای لغو عضویت، دستور /stop را ارسال کنید.",
        )
        self.assertEqual(
            UNSUBSCRIBED_MESSAGE,
            "عضویت شما در یادآوری‌های کاور لغو شد. برای عضویت دوباره، دستور /start را ارسال کنید.",
        )

    def test_delivery_test_message_is_persian(self):
        self.assertEqual(TEST_MESSAGE, "یادآور کاور: پیام آزمایشی تلگرام با موفقیت ارسال شد.")

    def test_24_hour_reminder_uses_persian_numerals_and_keeps_permalink(self):
        permalink = "https://www.instagram.com/"
        self.assertEqual(
            reminder_message(24, permalink),
            "بررسی کاور: از انتشار این ریلز دست‌کم ۲۴ ساعت گذشته است و تغییری در کاور آن تشخیص داده "
            "نشده است. لطفاً کاور را بررسی کنید.\n\nhttps://www.instagram.com/",
        )

    def test_48_hour_reminder_marks_the_final_notification(self):
        permalink = "https://www.instagram.com/"
        self.assertEqual(
            reminder_message(48, permalink),
            "بررسی کاور: از انتشار این ریلز دست‌کم ۴۸ ساعت گذشته است و تغییری در کاور آن تشخیص داده "
            "نشده است. لطفاً کاور را بررسی کنید. این آخرین یادآوری است.\n\n"
            "https://www.instagram.com/",
        )


if __name__ == "__main__":
    unittest.main()
