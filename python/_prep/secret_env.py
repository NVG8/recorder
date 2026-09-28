"""Load local config plus optional AWS Secrets Manager values into env."""

from __future__ import annotations

import json
import os
from pathlib import Path

import boto3
from dotenv import load_dotenv


def load_project_env(
    env_file: str | Path | None = None,
    *,
    override: bool = False,
    secret_id: str | None = None,
) -> None:
    """Load local .env config and optional AWS Secrets Manager JSON secret.

    Local environment variables win by default. The secret is expected to be a
    JSON object whose keys are environment variable names.
    """
    if env_file:
        load_dotenv(env_file, override=override)
    else:
        load_dotenv(override=override)

    resolved_secret_id = (
        secret_id
        or os.getenv("RECORDER_SECRETS_SECRET_ID", "").strip()
    )
    if not resolved_secret_id:
        return

    region = os.getenv("AWS_REGION", "").strip() or os.getenv("AWS_DEFAULT_REGION", "").strip() or "us-east-1"
    profile = os.getenv("AWS_PROFILE", "").strip() or None
    session = boto3.Session(profile_name=profile, region_name=region) if profile else boto3.Session(region_name=region)
    client = session.client("secretsmanager")
    response = client.get_secret_value(SecretId=resolved_secret_id)
    secret_string = response.get("SecretString", "{}")
    values = json.loads(secret_string)
    if not isinstance(values, dict):
        raise RuntimeError(f"Secrets Manager secret {resolved_secret_id!r} must contain a JSON object")

    for key, value in values.items():
        if value is None:
            continue
        if override or not os.getenv(key):
            os.environ[key] = str(value)


