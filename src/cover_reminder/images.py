import io
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

PIXEL_BYTES = 64 * 64 * 3
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def normalize_image(content: bytes) -> bytes:
    if len(content) > MAX_IMAGE_BYTES:
        raise ValueError("thumbnail_too_large")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content), formats=["JPEG", "PNG", "WEBP"]) as image:
                if image.width * image.height > 20_000_000:
                    raise ValueError("thumbnail_too_large")
                normalized = ImageOps.exif_transpose(image).convert("RGB").resize(
                    (64, 64), Image.Resampling.LANCZOS,
                )
                return normalized.tobytes()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError,
            Image.DecompressionBombWarning):
        raise ValueError("thumbnail_unreadable") from None


def difference(first: bytes, second: bytes) -> float:
    if len(first) != PIXEL_BYTES or len(second) != PIXEL_BYTES:
        raise ValueError("invalid_normalized_image")
    return sum(abs(a - b) for a, b in zip(first, second, strict=True)) / (PIXEL_BYTES * 255)
