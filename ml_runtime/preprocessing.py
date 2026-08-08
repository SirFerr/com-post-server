import numpy as np
from PIL import Image


def prepare(image: Image.Image, size: int = 640) -> np.ndarray:
    image = image.convert("RGB")
    scale = min(size / image.width, size / image.height)
    target = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    image = image.resize(target, Image.Resampling.BILINEAR)
    canvas = Image.new("RGB", (size, size), (114, 114, 114))
    canvas.paste(image, ((size - image.width) // 2, (size - image.height) // 2))
    array = np.asarray(canvas, dtype=np.float32).transpose(2, 0, 1) / 255.0
    return array[None]
