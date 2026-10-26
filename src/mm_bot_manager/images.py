"""Bounded decoding and normalization of uploaded bot artwork."""

import warnings
from io import BytesIO
from threading import Lock

from PIL import Image, ImageOps, UnidentifiedImageError

from .i18n import UserFacingError

AVATAR_LIMIT = 5 * 1024 * 1024
_DECODE_LOCK = Lock()


def normalize_image(data: bytes) -> bytes:
    # Workers run in threads. Keep full-size decodes serial, including when an
    # awaiting coroutine is cancelled, to bound memory in the 256 MiB container.
    with _DECODE_LOCK:
        return _normalize_image(data)


def _normalize_image(data: bytes) -> bytes:
    try:
        if not data or len(data) > AVATAR_LIMIT:
            raise ValueError
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                if (
                    image.format not in {"PNG", "JPEG"}
                    or max(image.size) > 4096
                    or getattr(image, "n_frames", 1) != 1
                ):
                    raise ValueError
                image.load()
                # Crop before orientation/conversion so copies stay at 512x512.
                normalized = ImageOps.exif_transpose(ImageOps.fit(image, (512, 512))).convert(
                    "RGBA"
                )
                output = BytesIO()
                normalized.info.clear()
                normalized.save(output, format="PNG")
                return output.getvalue()
    except (
        ValueError,
        OSError,
        UnidentifiedImageError,
        Image.DecompressionBombWarning,
        Image.DecompressionBombError,
    ) as exc:
        raise UserFacingError("images.invalid") from exc
