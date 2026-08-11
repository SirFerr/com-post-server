import json
import math
from pathlib import Path

import boto3
from fastapi import HTTPException

from ..config import get_settings


def firmware_s3():
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=settings.firmware_s3_endpoint,
        aws_access_key_id=settings.firmware_s3_access_key,
        aws_secret_access_key=settings.firmware_s3_secret_key,
    )


def firmware_rows() -> list[dict]:
    settings = get_settings()
    client = firmware_s3()
    result = []
    paginator = client.get_paginator("list_objects_v2")
    objects = (
        item
        for page in paginator.paginate(Bucket=settings.firmware_s3_bucket)
        for item in page.get("Contents", [])
    )
    for item in objects:
        head = client.head_object(Bucket=settings.firmware_s3_bucket, Key=item["Key"])
        result.append({
            "key": item["Key"],
            "name": Path(item["Key"]).name,
            "size": item["Size"],
            "updated_at": item["LastModified"],
            "address": head.get("Metadata", {}).get("address", "0x10000"),
            "chip": head.get("Metadata", {}).get("chip", "ESP32"),
        })
    return sorted(result, key=lambda row: row["updated_at"], reverse=True)


def human_size(value: int) -> str:
    size = float(value)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024 or unit == "ГБ":
            return f"{size:.1f} {unit}" if unit != "Б" else f"{int(size)} Б"
        size /= 1024
    return f"{value} Б"


def storage_file_label(key: str) -> str:
    if key.startswith("deposits/"):
        return "Фото закладки"
    if key.startswith("engineering/"):
        return "Инженерное фото"
    if key.startswith("maintenance/"):
        return "Фото обслуживания"
    return "Изображение"


def normalized_annotations(raw: str) -> list[dict]:
    try:
        values = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise HTTPException(422, "Некорректная разметка") from exc
    if not isinstance(values, list) or len(values) > 100:
        raise HTTPException(422, "Некорректная разметка")
    result = []
    for value in values:
        if not isinstance(value, dict):
            raise HTTPException(422, "Некорректная разметка")
        try:
            x, y, width, height = (float(value[key]) for key in ("x", "y", "width", "height"))
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(422, "Некорректная разметка") from exc
        if not all(math.isfinite(item) for item in (x, y, width, height)):
            raise HTTPException(422, "Некорректная разметка")
        if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1.000001 or y + height > 1.000001:
            raise HTTPException(422, "Координаты разметки выходят за границы изображения")
        result.append({
            "x": round(x, 6),
            "y": round(y, 6),
            "width": round(width, 6),
            "height": round(height, 6),
            "label": str(value.get("label") or "contamination")[:80],
        })
    return result
