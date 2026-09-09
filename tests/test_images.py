import io
import unittest
from pathlib import Path

from PIL import Image

from cover_reminder.api import ServiceError, validate_thumbnail_url
from cover_reminder.images import PIXEL_BYTES, difference, normalize_image

FIXTURES = Path(__file__).parent / "fixtures"


class ImageTests(unittest.TestCase):
    def test_same_photograph_has_no_difference(self):
        sample = normalize_image((FIXTURES / "hopper.jpg").read_bytes())
        self.assertEqual(len(sample), PIXEL_BYTES)
        self.assertEqual(difference(sample, sample), 0)

    def test_distinct_photographs_exceed_default_threshold(self):
        first = normalize_image((FIXTURES / "hopper.jpg").read_bytes())
        second = normalize_image((FIXTURES / "flower.jpg").read_bytes())
        self.assertGreater(difference(first, second), 0.05)

    def test_recompression_stays_below_default_threshold(self):
        content = (FIXTURES / "hopper.jpg").read_bytes()
        output = io.BytesIO()
        with Image.open(io.BytesIO(content)) as image:
            image.save(output, format="JPEG", quality=65)
        self.assertLess(difference(normalize_image(content), normalize_image(output.getvalue())), 0.05)

    def test_lossless_reencoding_does_not_count_as_a_cover_edit(self):
        content = (FIXTURES / "hopper.jpg").read_bytes()
        output = io.BytesIO()
        with Image.open(io.BytesIO(content)) as image:
            image.save(output, format="PNG")
        self.assertEqual(difference(normalize_image(content), normalize_image(output.getvalue())), 0)

    def test_empty_image_is_unverifiable(self):
        with self.assertRaises(ValueError):
            normalize_image(b"")

    def test_invalid_normalized_image_is_rejected(self):
        with self.assertRaises(ValueError):
            difference(b"", b"")

    def test_credentials_and_private_destinations_cannot_be_downloaded(self):
        for url in ("http://cdninstagram.com/a", "https://127.0.0.1/a", "https://fbcdn.net.evil.example/a",
                    "https://key@cdninstagram.com/a", "file:///etc/passwd", "https://cdninstagram.com:8080/a"):
            with self.subTest(url=url), self.assertRaises(ServiceError):
                validate_thumbnail_url(url)


if __name__ == "__main__":
    unittest.main()
