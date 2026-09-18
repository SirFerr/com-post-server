"""Background worker for training requests created in the web ML center."""

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3
from sqlalchemy import select

from app.database import SessionLocal
from app.ml_dataset import require_private_training_manifest
from app.models import DatasetVersion, ModelTrainingRun
from tools.ml_pipeline import export_yolo, train_candidate
from tools.taco_pretrain import ensure_taco_pretrained


def storage():
    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("S3_ENDPOINT", "http://minio:9000"),
        aws_access_key_id=os.environ.get("S3_ACCESS_KEY", "minioadmin"),
        aws_secret_access_key=os.environ.get("S3_SECRET_KEY", "minioadmin"),
    )


def training_base_model() -> str:
    base_model = os.environ.get("ML_BASE_MODEL", "yolo26n.pt")
    if os.environ.get("ML_TACO_PRETRAIN", "true").lower() not in {"1", "true", "yes"}:
        return base_model
    weights = ensure_taco_pretrained(
        Path(os.environ.get("ML_TACO_ROOT", "/root/.cache/compost/taco")),
        base_model=base_model,
        epochs=int(os.environ.get("ML_TACO_EPOCHS", "30")),
        image_size=int(os.environ.get("ML_IMAGE_SIZE", "640")),
        download_workers=int(os.environ.get("ML_TACO_DOWNLOAD_WORKERS", "8")),
    )
    return str(weights)


def run_next() -> bool:
    with SessionLocal() as db:
        run = db.scalar(
            select(ModelTrainingRun)
            .where(ModelTrainingRun.status == "QUEUED")
            .order_by(ModelTrainingRun.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if not run:
            return False
        run.status = "RUNNING"
        run.started_at = datetime.now(timezone.utc)
        db.commit()
        run_id = run.id
        dataset_version = run.dataset_version

    try:
        with SessionLocal() as db:
            dataset = db.scalar(
                select(DatasetVersion).where(DatasetVersion.version == dataset_version)
            )
            manifest = dataset.manifest
            require_private_training_manifest(json.loads(manifest))
        with tempfile.TemporaryDirectory(prefix=f"compost-ml-{run_id}-") as directory:
            root = Path(directory)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(manifest, encoding="utf-8")
            data_path = root / "dataset"
            output_path = root / "runs"
            export_yolo(manifest_path, data_path)
            report = train_candidate(
                data_path,
                output_path,
                training_base_model(),
                int(os.environ.get("ML_EPOCHS", "60")),
                int(os.environ.get("ML_IMAGE_SIZE", "640")),
                float(os.environ.get("ML_CONFIDENCE", "0.35")),
            )
            prefix = f"ml/runs/{run_id}"
            client = storage()
            bucket = os.environ.get("S3_BUCKET", "compost-photos")
            for name in ("model.onnx", "model.pt", "report.json"):
                client.upload_file(str(output_path / "candidate" / name), bucket, f"{prefix}/{name}")
        with SessionLocal() as db:
            run = db.get(ModelTrainingRun, run_id)
            run.status = report["status"]
            run.metrics = json.dumps(report, ensure_ascii=False)
            run.artifact_prefix = prefix
            run.completed_at = datetime.now(timezone.utc)
            db.commit()
    except Exception as exc:
        with SessionLocal() as db:
            run = db.get(ModelTrainingRun, run_id)
            run.status = "FAILED"
            run.error = str(exc)[:4000]
            run.completed_at = datetime.now(timezone.utc)
            db.commit()
    return True


def main() -> None:
    once = os.environ.get("ML_WORKER_ONCE", "").lower() in {"1", "true", "yes"}
    while True:
        handled = run_next()
        if once:
            return
        if not handled:
            time.sleep(10)


if __name__ == "__main__":
    main()
