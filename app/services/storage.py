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
            config=Config(signature_version="s3v4"),
        )

    def ensure_bucket(self):
        try:
            self.client.head_bucket(Bucket=settings.minio_bucket)
        except Exception:
            self.client.create_bucket(Bucket=settings.minio_bucket)

    def put_text(self, key: str, text: str) -> str:
        self.ensure_bucket()
        self.client.put_object(Bucket=settings.minio_bucket, Key=key, Body=text.encode(), ContentType="text/csv")
        return f"s3://{settings.minio_bucket}/{key}"

    def list_objects(self, prefix: str = "") -> list[dict]:
        self.ensure_bucket()
        response = self.client.list_objects_v2(Bucket=settings.minio_bucket, Prefix=prefix)
        return [
            {"key": x["Key"], "size": x["Size"], "last_modified": x["LastModified"].isoformat()}
            for x in response.get("Contents", [])
        ]
