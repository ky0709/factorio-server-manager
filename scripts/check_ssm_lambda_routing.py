"""
SSM 上の Lambda 名（Interactor ルーティング用）と .env の整合を確認する。
任意で Interactor の CloudWatch ログを検索し、「Critical SSM parameters missing」の有無を確認する。

使用例:
  python scripts/check_ssm_lambda_routing.py dev
  python scripts/check_ssm_lambda_routing.py dev --logs
  python scripts/check_ssm_lambda_routing.py prod --logs --since-minutes 120
"""
import argparse
import os
import sys
import time

import boto3
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

REQUIRED_SSM_KEYS = (
    "executor_lambda_name",
    "worker_lambda_name",
    "notifier_lambda_name",
)


def load_env(env_arg: str) -> str:
    env_file = ".env" if env_arg == "prod" else f".env.{env_arg}"
    env_path = os.path.join(BASE_DIR, env_file)
    if os.path.exists(env_path):
        print(f"📖 Loading environment: {env_file}")
        load_dotenv(env_path, override=True)
    else:
        load_dotenv(os.path.join(BASE_DIR, ".env"), override=True)
    return env_file


def fetch_ssm_config(path: str, region: str) -> dict:
    """factorio_common.fetch_config_from_ssm と同じキー規則（パス末尾を lower）。"""
    ssm = boto3.client("ssm", region_name=region)
    config = {}
    paginator = ssm.get_paginator("get_parameters_by_path")
    for page in paginator.paginate(Path=path, WithDecryption=True):
        for p in page["Parameters"]:
            key = p["Name"].split("/")[-1].lower()
            config[key] = p["Value"].strip("'\" ")
    return config


def check_logs(
    log_group: str,
    region: str,
    since_ms: int,
    patterns: tuple,
) -> dict:
    """filter_log_events でパターンごとにヒット件数を返す。"""
    logs = boto3.client("logs", region_name=region)
    out = {pat: [] for pat in patterns}
    try:
        for pat in patterns:
            resp = logs.filter_log_events(
                logGroupName=log_group,
                startTime=since_ms,
                filterPattern=pat,
                limit=20,
            )
            out[pat] = resp.get("events", [])
    except logs.exceptions.ResourceNotFoundException:
        return {"__error__": f"log group not found: {log_group}"}
    except Exception as e:
        return {"__error__": str(e)}
    return out


def main():
    parser = argparse.ArgumentParser(description="Check SSM Lambda names for Interactor routing")
    parser.add_argument("env", nargs="?", default="prod", help="Target environment (dev/prod)")
    parser.add_argument(
        "--logs",
        "--log",
        action="store_true",
        help="Also scan Interactor CloudWatch logs for SSM warning lines",
    )
    parser.add_argument(
        "--since-minutes",
        type=int,
        default=60,
        help="Log lookback window when --logs (default: 60)",
    )
    args = parser.parse_args()

    load_env(args.env)
    region = os.getenv("AWS_REGION", "ap-northeast-1")
    ssm_path = os.getenv("SSM_PARAMETER_PATH", "/factorio/").strip()
    if not ssm_path.endswith("/"):
        ssm_path += "/"

    print(f"📂 SSM path: {ssm_path}")
    print(f"🌐 Region: {region}\n")

    try:
        params = fetch_ssm_config(ssm_path, region)
    except Exception as e:
        print(f"❌ Failed to read SSM: {e}")
        sys.exit(1)

    exit_code = 0
    expected_suffix = os.getenv("EXPECTED_LAMBDA_NAME_SUFFIX", "").strip()
    if not expected_suffix and args.env == "dev":
        expected_suffix = "-dev"

    for key in REQUIRED_SSM_KEYS:
        val = params.get(key)
        if not val:
            print(f"❌ {key}: MISSING (Interactor may fall back to default name without suffix)")
            exit_code = 1
            continue
        line = f"✅ {key}: {val}"
        if expected_suffix and not val.endswith(expected_suffix):
            print(f"⚠️  {key}: {val}  (expected suffix «{expected_suffix}» for env={args.env})")
            exit_code = 1
        else:
            print(line)

    # .env との一致（任意の整合チェック）
    pairs = [
        ("executor_lambda_name", "EXECUTOR_LAMBDA_NAME"),
        ("worker_lambda_name", "WORKER_LAMBDA_NAME"),
        ("notifier_lambda_name", "NOTIFIER_LAMBDA_NAME"),
    ]
    print()
    for ssm_k, env_k in pairs:
        ssm_v = params.get(ssm_k)
        env_v = (os.getenv(env_k) or "").strip()
        if not ssm_v or not env_v:
            continue
        if ssm_v == env_v:
            print(f"✅ {env_k} matches SSM {ssm_k}")
        else:
            print(f"⚠️  {env_k} ({env_v}) != SSM {ssm_k} ({ssm_v}) — register.py / .env 再同期を確認")
            exit_code = 1

    if not args.logs:
        print("\n💡 CloudWatch も確認する場合: 同じコマンドに --logs を付ける")
        sys.exit(exit_code)

    interactor_name = os.getenv("INTERACTOR_LAMBDA_NAME")
    if not interactor_name:
        print("\n⚠️ INTERACTOR_LAMBDA_NAME が未設定のためログ検索をスキップします。")
        sys.exit(exit_code)

    log_group = f"/aws/lambda/{interactor_name}"
    since_ms = int((time.time() - args.since_minutes * 60) * 1000)
    patterns = ("Critical SSM parameters missing", "Admin config updated from SSM")

    print(f"\n📜 CloudWatch: {log_group} (last {args.since_minutes} min)")
    result = check_logs(log_group, region, since_ms, patterns)

    if "__error__" in result:
        err = result["__error__"]
        print(f"❌ Logs: {err}")
        if "AccessDenied" in err or "not authorized" in err.lower():
            print(
                "\n💡 Regist ユーザーに CloudWatch Logs の読み取りが無い可能性があります。"
                " `aws/IAM/FactorioRegistPolicy/policy.json.example` の "
                "`AllowCloudWatchLogsReadFactorioLambda` を反映するか、"
                "コンソールで当該ロググループを確認してください。"
            )
        sys.exit(1)

    warn_events = result.get("Critical SSM parameters missing", [])
    if warn_events:
        print(f"❌ Found {len(warn_events)} line(s) matching «Critical SSM parameters missing»")
        for ev in warn_events[:3]:
            ts = ev.get("timestamp", 0)
            msg = ev.get("message", "").strip()[:500]
            print(f"   - {ts}: {msg}")
        exit_code = 1
    else:
        print("✅ No «Critical SSM parameters missing» in the window")

    ok_events = result.get("Admin config updated from SSM", [])
    print(f"ℹ️  «Admin config updated from SSM» hits: {len(ok_events)} (informational)")

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
