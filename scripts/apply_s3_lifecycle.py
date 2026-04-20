import argparse
import os
import sys
from typing import Any

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

VALID_TARGETS = ("saves", "logs", "mods", "config")


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


def _get_required_int(name: str) -> int:
    raw = os.getenv(name, "").strip()
    if raw == "":
        print(f"[ERROR] {name} is required.")
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


def _parse_targets(targets_raw: str) -> list[str]:
    parsed = []
    for item in targets_raw.split(","):
        target = item.strip().lower()
        if not target:
            continue
        if target not in VALID_TARGETS:
            print(f"[ERROR] Invalid target '{target}'. Use: {', '.join(VALID_TARGETS)}")
            sys.exit(1)
        if target not in parsed:
            parsed.append(target)

    if not parsed:
        print(f"[ERROR] --targets must include at least one of: {', '.join(VALID_TARGETS)}")
        sys.exit(1)
    return parsed


def _rule_prefix(rule: dict[str, Any]) -> str:
    prefix = rule.get("Filter", {}).get("Prefix", "")
    if isinstance(prefix, str):
        return prefix
    return ""


def _filter_rules_by_targets(rules: list[dict[str, Any]], targets: list[str]) -> list[dict[str, Any]]:
    target_prefixes = {f"{t}/" for t in targets}
    filtered = [rule for rule in rules if _rule_prefix(rule) in target_prefixes]
    if not filtered:
        print("[ERROR] No lifecycle rules matched --targets in provided source.")
        sys.exit(1)
    return filtered


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply S3 lifecycle configuration using .env settings.",
        epilog=(
            "Examples:\n"
            "  python scripts/apply_s3_lifecycle.py dev\n"
            "  python scripts/apply_s3_lifecycle.py dev --targets saves,logs\n"
            "  python scripts/apply_s3_lifecycle.py prod --targets config"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "env",
        nargs="?",
        default="prod",
        help="Target environment. Example: dev, prod",
    )
    parser.add_argument(
        "--targets",
        default="saves,logs,mods,config",
        help="Comma-separated lifecycle targets. Allowed: saves,logs,mods,config",
    )
    args = parser.parse_args()

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_name = args.env
    env_file = _load_environment(base_dir, env_name)
    _ask_confirmation(env_name, env_file)

    bucket = os.getenv("S3_BUCKET_NAME", "").strip()
    if not bucket:
        print("[ERROR] S3_BUCKET_NAME is not set in environment.")
        sys.exit(1)
    targets = _parse_targets(args.targets)

    rules_to_apply = _filter_rules_by_targets(_build_rules_from_env(), targets)
    source_description = f"env ({env_file}) targets={','.join(targets)}"

    s3 = boto3.client("s3")

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
            print("[ERROR] every rule must have an ID.")
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
        print("[INFO] Mode: merge-by-id")
    except ClientError as e:
        print(f"[ERROR] Failed to apply lifecycle configuration: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
