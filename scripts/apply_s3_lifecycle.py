import argparse
import json
import os
import sys
from typing import Any

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv


def _load_environment(base_dir: str, env_name: str) -> str:
    env_file = ".env" if env_name == "prod" else f".env.{env_name}"
    env_path = os.path.join(base_dir, env_file)

    if os.path.exists(env_path):
        print(f"[INFO] Loading environment: {env_file}")
        load_dotenv(env_path, override=True)
        return env_file

    print(f"[WARN] Environment file {env_file} not found, falling back to default .env")
    load_dotenv(os.path.join(base_dir, ".env"), override=True)
    return ".env"


def _ask_confirmation(env_name: str, env_file: str) -> None:
    if os.getenv("AUTO_CONFIRM") == "1":
        return

    confirm = input(f"Apply S3 lifecycle rules for '{env_file if env_name else '.env (PROD)'}'? (y/N): ")
    if confirm.lower() != "y":
        print("[INFO] Operation cancelled.")
        sys.exit(1)

    if env_name == "prod":
        print("\n[WARN] You are about to update PRODUCTION S3 lifecycle configuration.")
        prod_confirm = input("To proceed, please type 'DEPLOY-PROD': ")
        if prod_confirm != "DEPLOY-PROD":
            print("[INFO] Production update aborted.")
            sys.exit(1)


def _resolve_template_path(base_dir: str, template_arg: str) -> str:
    if os.path.isabs(template_arg):
        return template_arg
    return os.path.join(base_dir, template_arg)


def _get_required_int(name: str) -> int:
    raw = os.getenv(name, "").strip()
    if raw == "":
        print(f"[ERROR] {name} is required for --from-env mode.")
        sys.exit(1)
    try:
        return int(raw)
    except ValueError:
        print(f"[ERROR] {name} must be an integer. Current value: {raw}")
        sys.exit(1)


def _get_optional_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[ERROR] {name} must be an integer. Current value: {raw}")
        sys.exit(1)


def _build_rule(prefix: str, rule_id_env: str, default_rule_id: str) -> dict[str, Any]:
    noncurrent_days = _get_required_int(f"S3_LIFECYCLE_{prefix.upper()}_NONCURRENT_DAYS")
    keep_total_versions = _get_required_int(f"S3_LIFECYCLE_{prefix.upper()}_KEEP_TOTAL_VERSIONS")
    multipart_days = _get_optional_int(f"S3_LIFECYCLE_{prefix.upper()}_ABORT_MULTIPART_DAYS", 7)
    if keep_total_versions < 1:
        print(f"[ERROR] S3_LIFECYCLE_{prefix.upper()}_KEEP_TOTAL_VERSIONS must be >= 1.")
        sys.exit(1)

    newer_noncurrent = max(1, keep_total_versions - 1)
    if keep_total_versions == 1:
        print(
            f"[WARN] {prefix}: AWS lifecycle cannot guarantee immediate single-version retention. "
            "Noncurrent versions are evaluated by day granularity."
        )

    rule_id = os.getenv(rule_id_env, default_rule_id).strip() or default_rule_id
    return {
        "ID": rule_id,
        "Status": "Enabled",
        "Filter": {"Prefix": f"{prefix}/"},
        "NoncurrentVersionExpiration": {
            "NoncurrentDays": noncurrent_days,
            "NewerNoncurrentVersions": newer_noncurrent,
        },
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": multipart_days},
    }


def _build_logs_rule() -> dict[str, Any]:
    rule_id = os.getenv("S3_LIFECYCLE_LOGS_RULE_ID", "logs-lifecycle").strip() or "logs-lifecycle"
    expiration_days = _get_required_int("S3_LIFECYCLE_LOGS_EXPIRATION_DAYS")
    noncurrent_days = _get_optional_int("S3_LIFECYCLE_LOGS_NONCURRENT_DAYS", expiration_days)
    keep_last_file = os.getenv("S3_LIFECYCLE_LOGS_KEEP_LAST_FILE", "0").strip() == "1"
    multipart_days = _get_optional_int("S3_LIFECYCLE_LOGS_ABORT_MULTIPART_DAYS", 7)

    if keep_last_file:
        print(
            "[WARN] logs keep-last-file is requested, but S3 lifecycle cannot retain exactly one object "
            "across many keys (e.g., logs/date=YYYY-MM-DD/*)."
        )

    return {
        "ID": rule_id,
        "Status": "Enabled",
        "Filter": {"Prefix": "logs/"},
        "Expiration": {"Days": expiration_days},
        "NoncurrentVersionExpiration": {
            "NoncurrentDays": noncurrent_days,
            "NewerNoncurrentVersions": 1,
        },
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": multipart_days},
    }


def _build_rules_from_env() -> list[dict[str, Any]]:
    return [
        _build_rule("saves", "S3_LIFECYCLE_SAVES_RULE_ID", "saves-lifecycle"),
        _build_logs_rule(),
        _build_rule("mods", "S3_LIFECYCLE_MODS_RULE_ID", "mods-lifecycle"),
        _build_rule("config", "S3_LIFECYCLE_CONFIG_RULE_ID", "config-lifecycle"),
    ]


def _load_template_rules(base_dir: str, template_arg: str) -> list[dict[str, Any]]:
    template_path = _resolve_template_path(base_dir, template_arg)
    if not os.path.exists(template_path):
        print(f"[ERROR] template not found: {template_path}")
        sys.exit(1)

    try:
        with open(template_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
    except json.JSONDecodeError as e:
        print(f"[ERROR] invalid JSON template: {e}")
        sys.exit(1)

    lifecycle_payload = {"Rules": loaded["Rules"]} if "Rules" in loaded else loaded
    if "Rules" not in lifecycle_payload or not isinstance(lifecycle_payload["Rules"], list):
        print("[ERROR] template must contain a Rules array.")
        sys.exit(1)
    return lifecycle_payload["Rules"]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply S3 lifecycle configuration from a JSON template."
    )
    parser.add_argument(
        "env",
        nargs="?",
        default="prod",
        help="Target environment. Example: dev, prod",
    )
    parser.add_argument(
        "--template",
        default="",
        help="Path to lifecycle JSON template (contains Rules array or full LifecycleConfiguration).",
    )
    parser.add_argument(
        "--from-env",
        action="store_true",
        help="Build and apply rules from .env values for saves/logs/mods/config.",
    )
    parser.add_argument(
        "--bucket",
        default="",
        help="Target S3 bucket name. Defaults to S3_BUCKET_NAME from env.",
    )
    parser.add_argument(
        "--replace-all",
        action="store_true",
        help="Replace all existing rules instead of merging by rule ID.",
    )
    args = parser.parse_args()

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_name = args.env
    env_file = _load_environment(base_dir, env_name)
    _ask_confirmation(env_name, env_file)

    bucket = (args.bucket or os.getenv("S3_BUCKET_NAME", "")).strip()
    if not bucket:
        print("[ERROR] S3_BUCKET_NAME is not set. Use --bucket or set it in env.")
        sys.exit(1)

    if args.from_env:
        rules_to_apply = _build_rules_from_env()
        source_description = "env-based rules"
    else:
        if not args.template:
            print("[ERROR] --template is required unless --from-env is specified.")
            sys.exit(1)
        rules_to_apply = _load_template_rules(base_dir, args.template)
        source_description = _resolve_template_path(base_dir, args.template)

    s3 = boto3.client("s3")

    if not args.replace_all:
        try:
            current = s3.get_bucket_lifecycle_configuration(Bucket=bucket)
            current_rules = current.get("Rules", [])
        except ClientError as e:
            err_code = e.response.get("Error", {}).get("Code", "")
            if err_code == "NoSuchLifecycleConfiguration":
                current_rules = []
            else:
                raise

        merged_by_id = {rule.get("ID"): rule for rule in current_rules if rule.get("ID")}
        no_id_rules = [rule for rule in current_rules if not rule.get("ID")]
        for rule in rules_to_apply:
            rule_id = rule.get("ID")
            if not rule_id:
                print("[ERROR] every rule must have an ID when using merge mode.")
                sys.exit(1)
            merged_by_id[rule_id] = rule

        rules_to_apply = no_id_rules + list(merged_by_id.values())

    try:
        s3.put_bucket_lifecycle_configuration(
            Bucket=bucket,
            LifecycleConfiguration={"Rules": rules_to_apply},
        )
        print(f"[OK] Applied lifecycle configuration to s3://{bucket}")
        print(f"[INFO] Source: {source_description}")
        print(f"[INFO] Rules: {len(rules_to_apply)}")
        print(f"[INFO] Mode: {'replace-all' if args.replace_all else 'merge-by-id'}")
    except ClientError as e:
        print(f"[ERROR] Failed to apply lifecycle configuration: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
