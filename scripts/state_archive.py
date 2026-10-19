#!/usr/bin/env python3
"""Logical backup of a dedicated private artifact bucket, called only while quiesced."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path

import boto3
from botocore.config import Config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["dump", "restore"])
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    client = boto3.client("s3", endpoint_url=os.environ["SOVITE_S3_ENDPOINT"],
                          region_name=os.environ.get("SOVITE_S3_REGION", "garage"),
                          config=Config(s3={"addressing_style": "path"}))
    bucket = os.environ.get("SOVITE_S3_BUCKET", "sovite-private")
    count = 0
    if args.operation == "dump":
        with args.path.open("x", encoding="utf-8") as stream:
            for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
                for obj in page.get("Contents", []):
                    result = client.get_object(Bucket=bucket, Key=obj["Key"])
                    body = result["Body"].read()
                    row = {"key": obj["Key"], "sha256": hashlib.sha256(body).hexdigest(),
                           "body": base64.b64encode(body).decode(), "content_type": result.get("ContentType", "application/json")}
                    stream.write(json.dumps(row, separators=(",", ":")) + "\n")
                    count += 1
        os.chmod(args.path, 0o600)
    else:
        # Restore immutable objects first. Existing unreferenced objects are retained for forensic review.
        with args.path.open(encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                body = base64.b64decode(row["body"], validate=True)
                if hashlib.sha256(body).hexdigest() != row["sha256"]:
                    raise ValueError("Artifact checksum mismatch")
                client.put_object(Bucket=bucket, Key=row["key"], Body=body, ContentType=row["content_type"])
                count += 1
    print(json.dumps({"operation": args.operation, "artifacts": count}))


if __name__ == "__main__":
    main()
