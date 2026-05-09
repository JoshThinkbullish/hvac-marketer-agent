import base64
import io
import os

from PIL import Image
from openai import OpenAI

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))


def _to_png_bytes(image_path: str) -> bytes:
    """Convert any image format to PNG bytes for the OpenAI API."""
    with Image.open(image_path) as img:
        img = img.convert("RGBA")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()


def generate_ad_images(business: str, offer: str, system_name: str,
                       system_image_path: str, location: str,
                       ad_type: str = "Professional marketing ad",
                       n_images: int = 1) -> list[bytes]:
    prompt = (
        f"Ad concept: {ad_type}. "
        f"Create a professional HVAC advertisement for '{business}' located in {location}. "
        f"Promotion: {offer}. "
        f"Feature the {system_name} HVAC unit as the hero product. "
        f"Bold, clean marketing design with strong call-to-action. "
        f"High contrast, modern layout suitable for social media or print. "
        f"Include the business name '{business}' and location '{location}' as text."
    )

    png_bytes = _to_png_bytes(system_image_path)
    img_file = io.BytesIO(png_bytes)
    img_file.name = "system.png"

    response = client.images.edit(
            model="gpt-image-1",
            image=img_file,
            prompt=prompt,
            n=n_images,
            size="1024x1024",
        )

    images = []
    for item in response.data:
        if item.b64_json:
            images.append(base64.b64decode(item.b64_json))
        elif item.url:
            import urllib.request
            with urllib.request.urlopen(item.url) as r:
                images.append(r.read())

    return images
