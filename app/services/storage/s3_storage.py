"""S3-compatible storage backend (AWS S3, MinIO, Cloudflare R2)."""
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


class S3StorageBackend:
    """Store files on S3-compatible object storage."""

    def __init__(self):
        import boto3

        self.bucket = os.environ["STORAGE_S3_BUCKET"]
        self.region = os.getenv("STORAGE_S3_REGION", "us-east-1")
        self.public_url_prefix = os.getenv("STORAGE_S3_PUBLIC_URL_PREFIX", "")

        kwargs = {
            "region_name": self.region,
            "aws_access_key_id": os.getenv("STORAGE_S3_ACCESS_KEY"),
            "aws_secret_access_key": os.getenv("STORAGE_S3_SECRET_KEY"),
        }
        endpoint_url = os.getenv("STORAGE_S3_ENDPOINT_URL")
        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url

        self.client = boto3.client("s3", **kwargs)

    def save(self, key: str, data: bytes, content_type: str) -> str:
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        logger.info(f"Saved {len(data)} bytes to s3://{self.bucket}/{key}")
        return key

    def get_url(self, key: str) -> str:
        if self.public_url_prefix:
            return f"{self.public_url_prefix.rstrip('/')}/{key}"
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=3600,
        )

    def delete(self, key: str) -> bool:
        self.client.delete_object(Bucket=self.bucket, Key=key)
        logger.info(f"Deleted s3://{self.bucket}/{key}")
        return True

    def get_local_path(self, key: str) -> str:
        tmp_dir = Path(tempfile.gettempdir()) / "versya_storage"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        filename = Path(key).name
        local_path = tmp_dir / filename
        self.client.download_file(self.bucket, key, str(local_path))
        return str(local_path)
