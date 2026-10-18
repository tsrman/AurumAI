"""Live bucket IAM acceptance against the fixed disposable MinIO endpoint only."""

from uuid import uuid4

import boto3
from botocore.exceptions import ClientError


def client(user: str, password: str):
    return boto3.client("s3", endpoint_url="http://127.0.0.1:59000", aws_access_key_id=user,
                        aws_secret_access_key=password, region_name="us-east-1")


def main() -> None:
    admin = client("lifecycle-minio", "lifecycle-minio-password")
    users = {name: client(f"lifecycle-{name}", f"lifecycle-{name}-password")
             for name in ("business", "archives")}
    key = f"archives/v1/{uuid4()}/iam-probe"
    payload = b"owned lifecycle IAM fixture"
    buckets = ["lifecycle-business", "lifecycle-archives"]
    try:
        for bucket in buckets:
            admin.put_object(Bucket=bucket, Key=key, Body=payload)
        for name, connection in users.items():
            own = f"lifecycle-{name}"
            connection.put_object(Bucket=own, Key=key, Body=payload)
            response = connection.get_object(Bucket=own, Key=key)
            with response["Body"] as body:
                assert body.read() == payload
            foreign = next(bucket for bucket in buckets if bucket != own)
            for method in ("get_object", "put_object"):
                args = {"Bucket": foreign, "Key": key}
                if method == "put_object":
                    args["Body"] = payload
                try:
                    getattr(connection, method)(**args)
                except ClientError as exc:
                    assert exc.response["Error"]["Code"] == "AccessDenied"
                else:
                    raise AssertionError("cross_bucket_access_allowed")
        print("Bucket IAM: 4 allowed and 4 denied operations passed")
    finally:
        for bucket in buckets:
            admin.delete_object(Bucket=bucket, Key=key)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("Owned bucket IAM acceptance failed; diagnostics suppressed") from None
