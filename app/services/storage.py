import boto3
from botocore.client import Config

from app.config import settings


class MinioStorage:
    def __init__(self):
        self.client = boto3.client(
            "s3",
            endpoint_url=f"http://{settings.minio_endpoint}",
            aws_access_key_id=settings.minio_access_key,
            aws_secret_access_key=settings.minio_secret_key,
            region_name="us-east-1",
            config=Config(signature_version="s3v4", retries={"max_attempts": 1}),
        )
        self._bucket_ready = False

    def ensure_bucket(self):
        if self._bucket_ready:
            return
        try:
            self.client.head_bucket(Bucket=settings.minio_bucket)
        except self.client.exceptions.ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in {"404", "NoSuchBucket", "NotFound"}:
                raise
            self.client.create_bucket(Bucket=settings.minio_bucket)
        self._bucket_ready = True

    def put_text(self, key: str, text: str) -> str:
        self.ensure_bucket()
        self.client.put_object(Bucket=settings.minio_bucket, Key=key, Body=text.encode(), ContentType="text/csv")
        return f"s3://{settings.minio_bucket}/{key}"

    def get_text(self, key: str) -> str:
        """Read a landed object back (ClickHouse loading reads from MinIO, not from memory)."""
        self.ensure_bucket()
        return self.client.get_object(Bucket=settings.minio_bucket, Key=key)["Body"].read().decode()

    def preview(self, key: str, max_bytes: int = 65536) -> str:
        self.ensure_bucket()
        body = self.client.get_object(Bucket=settings.minio_bucket, Key=key, Range=f"bytes=0-{max_bytes - 1}")["Body"]
        return body.read().decode(errors="replace")

    def list_objects(self, prefix: str = "", limit: int = 500) -> list[dict]:
        self.ensure_bucket()
        found: list[dict] = []
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=settings.minio_bucket, Prefix=prefix):
            for x in page.get("Contents", []):
                found.append({"key": x["Key"], "size": x["Size"], "last_modified": x["LastModified"].isoformat()})
        found.sort(key=lambda x: x["last_modified"], reverse=True)
        return found[:limit]
