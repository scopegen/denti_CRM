"""Turns a signature, whether a photo of ink on paper or one drawn on screen, into a clean
PNG with a transparent background, so it prints without a white box on any letterhead."""
import base64
import binascii
import io

from PIL import Image, ImageChops, UnidentifiedImageError

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_WIDTH = 800
# Anything this light is paper. Strokes fade out over the band below it, so edges stay smooth.
_WHITE = 235
_FADE = 40


class SignatureError(ValueError):
    pass


def _alpha_for(brightness: int) -> int:
    if brightness >= _WHITE:
        return 0
    if brightness >= _WHITE - _FADE:
        return round(255 * (_WHITE - brightness) / _FADE)
    return 255


_ALPHA_TABLE = [_alpha_for(value) for value in range(256)]


def clean_signature(data_url: str) -> tuple[bytes, int, int]:
    """Returns (png bytes, width, height). Accepts a data URL from a file upload or a drawing."""
    _, _, encoded = data_url.partition(",")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise SignatureError("That image could not be read.")
    if not raw or len(raw) > MAX_UPLOAD_BYTES:
        raise SignatureError("Use an image smaller than 5 MB.")
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except (UnidentifiedImageError, OSError):
        raise SignatureError("That image could not be read. Use a PNG or JPG.")

    image = image.convert("RGBA")
    if image.width > MAX_WIDTH:
        image = image.resize((MAX_WIDTH, round(image.height * MAX_WIDTH / image.width)), Image.LANCZOS)

    # Keep the original transparency, and also fade out anything light enough to be paper.
    paper_alpha = image.convert("L").point(_ALPHA_TABLE)
    image.putalpha(ImageChops.multiply(image.getchannel("A"), paper_alpha))

    bbox = image.getchannel("A").getbbox()
    if bbox is None:
        raise SignatureError("The signature looks empty. Draw or upload it again.")
    image = image.crop(bbox)

    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue(), image.width, image.height
