"""S3-compatible object storage client (MinIO locally)."""

from contextlib import AbstractAsyncContextManager

import aioboto3
from aiobotocore.config import AioConfig
from types_aiobotocore_s3 import S3Client

from app.core.config import Settings


def create_s3_client(settings: Settings) -> AbstractAsyncContextManager[S3Client]:
    session = aioboto3.Session()
    client: AbstractAsyncContextManager[S3Client] = session.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
        config=AioConfig(
            signature_version="s3v4",
            s3={"addressing_style": "path"},  # MinIO serves buckets on the path, not subdomains
        ),
    )
    return client
