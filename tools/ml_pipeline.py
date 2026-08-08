"""Export a frozen ComPost manifest to YOLO and train a release candidate."""

import argparse
import json
import os
import shutil
from pathlib import Path

import boto3
from PIL import Image


def load_manifest(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):  # dataset-v1 compatibility
        payload = {"schema_version": 1, "classes": ["contamination"], "samples": payload}
    if not isinstance(payload.get("samples"), list):
        raise ValueError("Manifest must contain a samples list")
    return payload


def s3_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("S3_ENDPOINT", "http://minio:9000"),
        aws_access_key_id=os.environ.get("S3_ACCESS_KEY", "minioadmin"),
        aws_secret_access_key=os.environ.get("S3_SECRET_KEY", "minioadmin"),
    )


def export_yolo(manifest_path: Path, output: Path) -> dict:
    manifest = load_manifest(manifest_path)
    if output.exists():
        shutil.rmtree(output)
    for split in ("train", "val", "test"):
        (output / "images" / split).mkdir(parents=True)
        (output / "labels" / split).mkdir(parents=True)

    client = s3_client()
    bucket = os.environ.get("S3_BUCKET", "compost-photos")
    exported = {"train": 0, "val": 0, "test": 0, "skipped_unboxed_positive": 0}
    for index, sample in enumerate(manifest["samples"]):
        label = sample.get("label")
        if label in ("REJECTED", "contamination") and not sample.get("boxes"):
            exported["skipped_unboxed_positive"] += 1
            continue
        split = sample.get("split", "train")
        if split not in ("train", "val", "test"):
            raise ValueError(f"Unsupported split {split!r}")
        sample_id = "".join(character if character.isalnum() or character in "-_" else "-" for character in str(sample.get("review_id", "sample")))
        stem = f"{index:06d}-{sample_id}"
        image_path = output / "images" / split / f"{stem}.jpg"
        label_path = output / "labels" / split / f"{stem}.txt"
        client.download_file(bucket, sample["photo_key"], str(image_path.with_suffix(".source")))
        try:
            with Image.open(image_path.with_suffix(".source")) as image:
                image.convert("RGB").save(image_path, quality=95)
        finally:
            image_path.with_suffix(".source").unlink(missing_ok=True)

        yolo_rows = []
        if label in ("REJECTED", "contamination"):
            for box in sample.get("boxes", []):
                x = float(box["x"])
                y = float(box["y"])
                width = float(box["width"])
                height = float(box["height"])
                yolo_rows.append(f"0 {x + width / 2:.8f} {y + height / 2:.8f} {width:.8f} {height:.8f}")
        label_path.write_text("\n".join(yolo_rows), encoding="utf-8")
        exported[split] += 1

    yaml = (
        f"path: {output.resolve().as_posix()}\n"
        "train: images/train\n"
        "val: images/val\n"
        "test: images/test\n"
        "names:\n"
        "  0: contamination\n"
    )
    (output / "data.yaml").write_text(yaml, encoding="utf-8")
    metadata = {
        "manifest": str(manifest_path.resolve()),
        "schema_version": manifest.get("schema_version", 1),
        "exported": exported,
    }
    (output / "export-metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return metadata


def evaluate_image_level(model, dataset: Path, split: str, confidence: float) -> dict:
    image_dir = dataset / "images" / split
    label_dir = dataset / "labels" / split
    images = sorted(image_dir.glob("*.jpg"))
    clean = contaminated = false_positives = detected_contamination = 0
    for image_path in images:
        has_target = bool((label_dir / f"{image_path.stem}.txt").read_text(encoding="utf-8").strip())
        prediction = model.predict(str(image_path), conf=confidence, verbose=False)[0]
        detected = len(prediction.boxes) > 0
        if has_target:
            contaminated += 1
            detected_contamination += int(detected)
        else:
            clean += 1
            false_positives += int(detected)
    return {
        "split": split,
        "contaminated_images": contaminated,
        "clean_images": clean,
        "contamination_image_recall": detected_contamination / contaminated if contaminated else None,
        "clean_image_false_positive_rate": false_positives / clean if clean else None,
    }


def train_candidate(
    dataset: Path,
    output: Path,
    model_name: str,
    epochs: int,
    image_size: int,
    confidence: float,
) -> dict:
    from ultralytics import YOLO

    model = YOLO(model_name)
    run = model.train(
        data=str(dataset / "data.yaml"),
        epochs=epochs,
        imgsz=image_size,
        project=str(output),
        name="train",
        exist_ok=True,
    )
    best_path = Path(run.save_dir) / "weights" / "best.pt"
    best = YOLO(str(best_path))
    split = "test" if any((dataset / "images" / "test").glob("*.jpg")) else "val"
    image_metrics = evaluate_image_level(best, dataset, split, confidence)
    recall = image_metrics["contamination_image_recall"]
    false_positive_rate = image_metrics["clean_image_false_positive_rate"]
    gate_passed = (
        recall is not None
        and false_positive_rate is not None
        and recall >= 0.90
        and false_positive_rate <= 0.10
    )
    onnx_path = Path(best.export(format="onnx", imgsz=image_size, dynamic=False, simplify=True, nms=True))
    candidate = output / "candidate"
    candidate.mkdir(parents=True, exist_ok=True)
    shutil.copy2(best_path, candidate / "model.pt")
    shutil.copy2(onnx_path, candidate / "model.onnx")
    report = {
        "status": "PASSED" if gate_passed else "REJECTED",
        "initial_weights": model_name,
        "epochs": epochs,
        "image_size": image_size,
        "confidence_threshold": confidence,
        "quality_gate": {
            "minimum_contamination_image_recall": 0.90,
            "maximum_clean_image_false_positive_rate": 0.10,
        },
        "image_metrics": image_metrics,
        "note": "A PASSED candidate still requires a human review before production activation.",
    }
    (candidate / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    export = subparsers.add_parser("export")
    export.add_argument("manifest", type=Path)
    export.add_argument("--output", type=Path, default=Path("ml-data"))
    train = subparsers.add_parser("train")
    train.add_argument("--dataset", type=Path, default=Path("ml-data"))
    train.add_argument("--output", type=Path, default=Path("ml-runs"))
    train.add_argument("--model", default=os.environ.get("ML_BASE_MODEL", "yolo26n.pt"))
    train.add_argument("--epochs", type=int, default=60)
    train.add_argument("--image-size", type=int, default=640)
    train.add_argument("--confidence", type=float, default=0.35)
    args = parser.parse_args()
    if args.command == "export":
        result = export_yolo(args.manifest, args.output)
    else:
        result = train_candidate(
            args.dataset,
            args.output,
            args.model,
            args.epochs,
            args.image_size,
            args.confidence,
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
