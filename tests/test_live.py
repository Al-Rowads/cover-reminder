import os
import unittest

from cover_reminder.api import Http, Instagram, Telegram
from cover_reminder.config import Config
from cover_reminder.images import normalize_image
from cover_reminder.model import Reel


@unittest.skipUnless(os.environ.get("RUN_LIVE_TESTS") == "1", "Requires explicitly enabled live account access")
class LiveReadTests(unittest.TestCase):
    def test_account_schemas_and_thumbnail(self):
        config = Config.from_environment()
        http = Http()
        instagram = Instagram(config, http)
        page = instagram.page(limit=25)
        self.assertIsInstance(page.get("data"), list)
        reels = [reel for item in page["data"] if (reel := Reel.from_media(item))]
        self.assertTrue(reels, "Publish a real Reel or select an account with a recent Reel")
        reel = instagram.get(reels[0].media_id)
        self.assertEqual(reel.media_id, reels[0].media_id)
        self.assertTrue(normalize_image(instagram.thumbnail(reel)))
        self.assertTrue(Telegram(config, http).check().get("is_bot"))


if __name__ == "__main__":
    unittest.main()
