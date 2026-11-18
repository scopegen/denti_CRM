"""Turns an uploaded logo into a tidy PNG for documents and the app's top bar: no wider or taller
than it needs to be, any empty transparent border trimmed, and its own colours and background
kept exactly (unlike a signature, a logo's white is part of the design)."""
import base64
import binascii
import io

from PIL import Image, UnidentifiedImageError

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_SIDE = 600


class LogoError(ValueError):
    pass


def clean_logo(data_url: str) -> tuple[bytes, int, int]:
    """Returns (png bytes, width, height). Accepts a data URL from a file upload."""
    _, _, encoded = data_url.partition(",")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise LogoError("That image could not be read.")
    if not raw or len(raw) > MAX_UPLOAD_BYTES:
        raise LogoError("Use an image smaller than 5 MB.")
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except (UnidentifiedImageError, OSError):
        raise LogoError("That image could not be read. Use a PNG or JPG.")

    image = image.convert("RGBA")
    # A fully transparent border is just padding; trim it so the logo sits snugly.
    bbox = image.getchannel("A").getbbox()
    if bbox is None:
        raise LogoError("The logo looks empty. Choose another image.")
    image = image.crop(bbox)
    image.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)

    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue(), image.width, image.height
