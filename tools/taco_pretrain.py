"""Prepare the official TACO dataset and pretrain the contamination detector."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath
from urllib.request import Request, urlopen

from PIL import Image


TACO_REVISION = "29de1a9ba05a647b83a90f18d7772e20bb23d846"
ANNOTATIONS_URL = f"https://raw.githubusercontent.com/pedropro/TACO/{TACO_REVISION}/data/annotations.json"
USER_AGENT = "ComPost-ML/1.0 (+https://github.com/SirFerr/com-post-server)"
CONTAMINANT_SUPERCATEGORIES = {
    "Aluminium foil",
    "Battery",
    "Blister pack",
    "Bottle",
    "Bottle cap",
    "Broken glass",
    "Can",
    "Glass jar",
    "Lid",
    "Other plastic",
    "Plastic bag & wrapper",
    "Plastic container",
    "Plastic glooves",
    "Plastic utensils",
    "Pop tab",
    "Rope & strings",
    "Scrap metal",
    "Shoe",
    "Squeezable tube",
    "Styrofoam piece",
}
CONTAMINANT_CATEGORY_NAMES = {
    "Disposable plastic cup",
    "Foam cup",
    "Glass cup",
    "Other plastic cup",
    "Plastic straw",
    "Cigarette",
}


def _safe_relative_path(value: str) -> Path:
    parts = PurePosixPath(value.replace("\\", "/")).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"Unsafe TACO image path: {value!r}")
    return Path(*parts)


def _split_for(value: str) -> str:
    # TACO is only transfer-learning data. The untouched ComPost test split is
    # the source of truth for release decisions.
    return "train" if hashlib.sha256(value.encode("utf-8")).digest()[0] < 230 else "val"


def _download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=60) as response, temporary.open("wb") as output:
        shutil.copyfileobj(response, output)
    temporary.replace(destination)


def download_taco(root: Path, workers: int = 8) -> tuple[Path, Path, dict]:
    source = root / "source"
    annotations_path = source / "annotations.json"
    if not annotations_path.exists():
        _download(ANNOTATIONS_URL, annotations_path)
    payload = json.loads(annotations_path.read_text(encoding="utf-8"))
    images_root = source / "images"

    def download_image(image: dict) -> tuple[int, str | None]:
        relative = _safe_relative_path(image["file_name"])
        destination = images_root / relative
        if destination.exists():
            return image["id"], None
        errors = []
        for url in (image.get("flickr_640_url"), image.get("flickr_url")):
            if not url:
                continue
            try:
                _download(url, destination)
                with Image.open(destination) as opened:
                    opened.verify()
                return image["id"], None
            except Exception as exc:  # individual Flickr images can disappear
                destination.unlink(missing_ok=True)
                errors.append(str(exc))
        return image["id"], "; ".join(errors) or "No image URL"

    failures = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = [executor.submit(download_image, image) for image in payload["images"]]
        for future in as_completed(futures):
            image_id, error = future.result()
            if error:
                failures.append({"image_id": image_id, "error": error})
    available = sum((images_root / _safe_relative_path(image["file_name"])).exists() for image in payload["images"])
    if available < 100:
        raise RuntimeError(f"Only {available} TACO images were downloaded; at least 100 are required")
    download_report = {"images": len(payload["images"]), "available": available, "failed": failures}
    (source / "download-report.json").write_text(json.dumps(download_report, indent=2), encoding="utf-8")
    return annotations_path, images_root, download_report


def prepare_taco_dataset(annotations_path: Path, images_root: Path, output: Path) -> dict:
    payload = json.loads(annotations_path.read_text(encoding="utf-8"))
    categories = payload.get("categories", [])
    contaminant_category_ids = {
        category["id"]
        for category in categories
        if category.get("supercategory") in CONTAMINANT_SUPERCATEGORIES
        or category.get("name") in CONTAMINANT_CATEGORY_NAMES
    }
    annotations_by_image: dict[int, list[dict]] = {}
    for annotation in payload.get("annotations", []):
        if annotation.get("category_id") in contaminant_category_ids:
            annotations_by_image.setdefault(annotation["image_id"], []).append(annotation)

    if output.exists():
        shutil.rmtree(output)
    for split in ("train", "val"):
        (output / "images" / split).mkdir(parents=True)
        (output / "labels" / split).mkdir(parents=True)

    exported = {"train": 0, "val": 0, "boxes": 0, "missing": 0, "without_clear_contaminants": 0}
    for image in sorted(payload.get("images", []), key=lambda item: item["id"]):
        source = images_root / _safe_relative_path(image["file_name"])
        if not source.exists():
            exported["missing"] += 1
            continue
        split = _split_for(image["file_name"])
        stem = f"taco-{int(image['id']):06d}"
        image_path = output / "images" / split / f"{stem}.jpg"
        with Image.open(source) as opened:
            source_width, source_height = opened.size
            opened.convert("RGB").save(image_path, quality=95)
        width = float(image.get("width") or source_width)
        height = float(image.get("height") or source_height)
        rows = []
        for annotation in annotations_by_image.get(image["id"], []):
            x, y, box_width, box_height = (float(value) for value in annotation["bbox"])
            x1 = min(max(x, 0.0), width)
            y1 = min(max(y, 0.0), height)
            x2 = min(max(x + box_width, 0.0), width)
            y2 = min(max(y + box_height, 0.0), height)
            normalized_width = (x2 - x1) / width
            normalized_height = (y2 - y1) / height
            if normalized_width <= 0 or normalized_height <= 0:
                continue
            center_x = (x1 + x2) / (2 * width)
            center_y = (y1 + y2) / (2 * height)
            rows.append(f"0 {center_x:.8f} {center_y:.8f} {normalized_width:.8f} {normalized_height:.8f}")
        if not rows:
            image_path.unlink(missing_ok=True)
            exported["without_clear_contaminants"] += 1
            continue
        (output / "labels" / split / f"{stem}.txt").write_text("\n".join(rows), encoding="utf-8")
        exported[split] += 1
        exported["boxes"] += len(rows)

    if not exported["train"] or not exported["val"]:
        raise RuntimeError("TACO export must contain both train and validation images")
    (output / "data.yaml").write_text(
        f"path: {output.resolve().as_posix()}\n"
        "train: images/train\n"
        "val: images/val\n"
        "names:\n"
        "  0: contamination\n",
        encoding="utf-8",
    )
    metadata = {
        "source": "pedropro/TACO",
        "revision": TACO_REVISION,
        "annotations_url": ANNOTATIONS_URL,
        "annotations_sha256": hashlib.sha256(annotations_path.read_bytes()).hexdigest(),
        "mapping": "clearly non-compostable TACO categories -> contamination",
        "exported": exported,
        "licenses": payload.get("licenses", []),
        "included_categories": [category for category in categories if category["id"] in contaminant_category_ids],
        "excluded_categories": [category for category in categories if category["id"] not in contaminant_category_ids],
    }
    (output / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return metadata


def ensure_taco_pretrained(
    root: Path,
    base_model: str = "yolo26n.pt",
    epochs: int = 30,
    image_size: int = 640,
    download_workers: int = 8,
) -> Path:
    weights = root / "taco-pretrained.pt"
    if weights.exists():
        return weights
    root.mkdir(parents=True, exist_ok=True)
    annotations, images, _ = download_taco(root, download_workers)
    dataset = root / "dataset"
    if not (dataset / "data.yaml").exists():
        prepare_taco_dataset(annotations, images, dataset)

    from ultralytics import YOLO

    model = YOLO(base_model)
    result = model.train(
        data=str(dataset / "data.yaml"),
        epochs=epochs,
        imgsz=image_size,
        project=str(root / "runs"),
        name="taco-pretrain",
        exist_ok=True,
    )
    best = Path(result.save_dir) / "weights" / "best.pt"
    temporary = weights.with_suffix(".pt.part")
    shutil.copy2(best, temporary)
    temporary.replace(weights)
    report = {
        "base_model": base_model,
        "epochs": epochs,
        "image_size": image_size,
        "weights": str(weights),
        "dataset_metadata": json.loads((dataset / "metadata.json").read_text(encoding="utf-8")),
    }
    (root / "pretraining-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return weights


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "pretrain"))
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("ML_TACO_ROOT", "ml-cache/taco")))
    parser.add_argument("--annotations", type=Path)
    parser.add_argument("--images", type=Path)
    parser.add_argument("--model", default=os.environ.get("ML_BASE_MODEL", "yolo26n.pt"))
    parser.add_argument("--epochs", type=int, default=int(os.environ.get("ML_TACO_EPOCHS", "30")))
    parser.add_argument("--image-size", type=int, default=int(os.environ.get("ML_IMAGE_SIZE", "640")))
    parser.add_argument("--download-workers", type=int, default=int(os.environ.get("ML_TACO_DOWNLOAD_WORKERS", "8")))
    args = parser.parse_args()
    if args.command == "prepare":
        if bool(args.annotations) != bool(args.images):
            parser.error("--annotations and --images must be provided together")
        if args.annotations:
            result = prepare_taco_dataset(args.annotations, args.images, args.root / "dataset")
        else:
            annotations, images, download_report = download_taco(args.root, args.download_workers)
            result = prepare_taco_dataset(annotations, images, args.root / "dataset")
            result["download"] = download_report
    else:
        weights = ensure_taco_pretrained(args.root, args.model, args.epochs, args.image_size, args.download_workers)
        result = {"weights": str(weights), "report": str(args.root / "pretraining-report.json")}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
