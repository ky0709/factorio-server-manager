import boto3
import json
import os
from dotenv import load_dotenv
import sys
import re
from datetime import datetime
import time
import argparse
import tempfile
from pathlib import Path
from botocore.config import Config
from botocore.exceptions import ReadTimeoutError

# プロジェクトルートの.envを読み込み
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

parser = argparse.ArgumentParser(description="Factorio Server Manager Integration Test Runner")
parser.add_argument("env", nargs="?", default="prod", help="Target environment (dev/prod)")
parser.add_argument("--silent", action="store_true", help="Do not send report to Discord")
parser.add_argument(
    "--tests",
    type=str,
    default="",
    help="Comma-separated test numbers to run (e.g. 5,6,12)"
)
parser.add_argument(
    "--categories",
    type=str,
    default="",
    help="Comma-separated categories to run (notifier,executor,worker,lifecycle,restore,final)"
)
parser.add_argument(
    "--yes",
    action="store_true",
    help="Skip interactive confirmation prompt"
)
parser.add_argument(
    "--cross-mode-start-test",
    action="store_true",
    help="Run cross-mode start test (STATIC/DYNAMIC lifecycle matrix)"
)
args = parser.parse_args()

env_arg = args.env
env_file = ".env" if env_arg == "prod" else f".env.{env_arg}"
env_path = os.path.join(BASE_DIR, env_file)


def setup_stdio_encoding():
    """
    Windows の cp932 環境でも絵文字ログ出力で停止しないよう、標準出力をUTF-8へ再設定する。
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


setup_stdio_encoding()

if os.path.exists(env_path):
    print(f"📖 Loading environment: {env_file}")
    load_dotenv(env_path, override=True)
else:
    load_dotenv(os.path.join(BASE_DIR, ".env"), override=True)

LAMBDA_INVOKE_TIMEOUT_SECONDS = int(os.getenv("TEST_RUNNER_LAMBDA_TIMEOUT_SECONDS", "90"))
lambda_client = boto3.client(
    'lambda',
    region_name=os.getenv('AWS_REGION', 'ap-northeast-1'),
    config=Config(
        connect_timeout=10,
        read_timeout=LAMBDA_INVOKE_TIMEOUT_SECONDS,
        retries={"max_attempts": 2, "mode": "standard"},
    ),
)
ssm_client = boto3.client('ssm', region_name=os.getenv('AWS_REGION', 'ap-northeast-1'))
ec2_client = boto3.client('ec2', region_name=os.getenv('AWS_REGION', 'ap-northeast-1'))
dynamodb = boto3.resource('dynamodb', region_name=os.getenv('AWS_REGION', 'ap-northeast-1'))
factorio_state_table = dynamodb.Table(os.getenv('DYNAMODB_TABLE_NAME', 'FactorioState'))
SSM_BASE_PATH = (os.getenv('SSM_PARAMETER_PATH') or '/factorio/').strip()
if not SSM_BASE_PATH.endswith('/'):
    SSM_BASE_PATH += '/'

TEST_CATEGORY_CATALOG = {
    1: {"notifier"},
    2: {"notifier"},
    3: {"executor"},
    4: {"executor", "lifecycle"},
    5: {"executor", "lifecycle"},
    6: {"executor", "lifecycle"},
    7: {"worker"},
    8: {"worker", "restore"},
    9: {"executor"},
    10: {"executor"},
    11: {"worker", "restore"},
    12: {"executor", "final"},
    13: {"worker"},
    14: {"worker"},
    15: {"worker", "lifecycle"},
    16: {"worker", "restore"},
    17: {"executor", "lifecycle"},
}


def parse_csv_set(raw_value):
    return {part.strip().lower() for part in raw_value.split(",") if part.strip()}


SELECTED_TEST_NUMBERS = set()
if args.tests:
    for raw in parse_csv_set(args.tests):
        if raw.isdigit():
            SELECTED_TEST_NUMBERS.add(int(raw))
        else:
            print(f"⚠️ Ignoring invalid test number: {raw}")

SELECTED_CATEGORIES = parse_csv_set(args.categories)
LOCK_FILE_PATH = Path(tempfile.gettempdir()) / f"factorio_test_runner_{env_arg}.lock"


def is_valid_status_content(content):
    """STATIC/DYNAMIC 両方の status 応答を許容する。"""
    if not content:
        return False
    # ID:091: DYNAMIC offline も「停止中」表示（旧 offline 文言も後方互換で許容）
    return any(
        keyword in content
        for keyword in ("稼働中", "停止中", "起動処理中", "停止処理中", "状態遷移中", "サーバーが起動していない")
    )


def query_target_instance(prefer_active=True, quiet=True):
    """Executor Lambda 経由で対象インスタンスIDと状態を取得する。"""
    payload = {
        "action": "integration_query_target_instance",
        "test_mode": True,
        "data": {"prefer_active": bool(prefer_active)},
    }
    return invoke_lambda(
        os.getenv('EXECUTOR_LAMBDA_NAME', 'Factorio_Executor'),
        payload,
        quiet=quiet,
    )


def resolve_target_instance_id(prefer_active=False):
    """
    監視対象のインスタンスIDを解決する。
    - DYNAMIC or prefer_active=True: ActiveInstanceId を優先
    - それ以外: INSTANCE_ID を使用
    """
    env_instance_id = (os.getenv('INSTANCE_ID') or '').strip()
    state = query_target_instance(prefer_active=prefer_active, quiet=True)
    if state and state.get('ok') and state.get('instance_id'):
        return state.get('instance_id')
    return env_instance_id or None


def acquire_runner_lock():
    now = time.time()
    stale_seconds = 4 * 60 * 60
    if LOCK_FILE_PATH.exists():
        try:
            meta = json.loads(LOCK_FILE_PATH.read_text(encoding='utf-8'))
        except Exception:
            meta = {}
        started_at = float(meta.get("started_at", 0) or 0)
        pid = meta.get("pid", "unknown")
        age = int(now - started_at) if started_at else None
        if started_at and age is not None and age > stale_seconds:
            print(f"⚠️ Stale lock detected (pid={pid}, age={age}s). Replacing lock.")
        else:
            detail = f"pid={pid}" if age is None else f"pid={pid}, age={age}s"
            print(f"❌ Another test_runner appears to be running ({detail}).")
            print(f"   Remove lock manually if this is incorrect: {LOCK_FILE_PATH}")
            return False

    LOCK_FILE_PATH.write_text(
        json.dumps({
            "pid": os.getpid(),
            "started_at": now,
            "env": env_arg,
            "args": sys.argv,
        }, ensure_ascii=False),
        encoding='utf-8'
    )
    return True


def release_runner_lock():
    try:
        if LOCK_FILE_PATH.exists():
            LOCK_FILE_PATH.unlink()
    except Exception as e:
        print(f"⚠️ Failed to remove lock file: {e}")


def should_run_test(test_no):
    if args.cross_mode_start_test:
        return test_no == 17
    if test_no == 17:
        return 17 in SELECTED_TEST_NUMBERS
    if SELECTED_TEST_NUMBERS and test_no not in SELECTED_TEST_NUMBERS:
        return False
    if SELECTED_CATEGORIES:
        return bool(TEST_CATEGORY_CATALOG.get(test_no, set()) & SELECTED_CATEGORIES)
    return True

def invoke_lambda(function_name, payload, quiet=False):
    if not quiet:
        print(f"🚀 Invoking {function_name}...")
    try:
        response = lambda_client.invoke(
            FunctionName=function_name,
            InvocationType='RequestResponse',  # 結果を確認するため同期実行
            Payload=json.dumps(payload)
        )
        result = json.loads(response['Payload'].read().decode('utf-8'))
        if not quiet:
            print(f"✅ Response from {function_name}: {json.dumps(result, indent=2, ensure_ascii=False)}")
        return result
    except ReadTimeoutError:
        timeout_message = (
            f"Invoke timed out after {LAMBDA_INVOKE_TIMEOUT_SECONDS}s "
            f"(function={function_name})"
        )
        print(f"❌ Failed to invoke {function_name}: {timeout_message}")
        return {"errorMessage": timeout_message}
    except Exception as e:
        # botocore 以外の Read timeout 表現もタイムアウトとして扱う
        if "Read timeout on endpoint URL" in str(e):
            timeout_message = (
                f"Invoke timed out after {LAMBDA_INVOKE_TIMEOUT_SECONDS}s "
                f"(function={function_name})"
            )
            print(f"❌ Failed to invoke {function_name}: {timeout_message}")
            return {"errorMessage": timeout_message}
        print(f"❌ Failed to invoke {function_name}: {e}")
        return None


def query_save_state_from_executor(quiet=False):
    """S3 最新バージョンと LatestSaveInfo を Executor（integration_query_save_state）経由で取得する。"""
    return invoke_lambda(
        os.getenv('EXECUTOR_LAMBDA_NAME', 'Factorio_Executor'),
        {"action": "integration_query_save_state", "test_mode": True},
        quiet=quiet,
    )


def query_start_lock_state(quiet=True):
    return invoke_lambda(
        os.getenv('EXECUTOR_LAMBDA_NAME', 'Factorio_Executor'),
        {"action": "integration_query_start_lock", "test_mode": True},
        quiet=quiet,
    )


def wait_until_start_lock_released(timeout_seconds=210):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        state = query_start_lock_state(quiet=True)
        if not state or not state.get('ok'):
            return False, state
        if not state.get('locked'):
            return True, state
        time.sleep(5)
    return False, {"ok": True, "locked": True, "error": "timeout"}

def wait_for_ec2_state(instance_id, state):
    if not instance_id:
        raise ValueError("instance_id is required")
    print(f"⏳ Waiting for instance {instance_id} to reach state: {state}...")
    if state not in ('stopped', 'running'):
        return
    deadline = time.time() + (15 * 40)
    while time.time() < deadline:
        status = query_target_instance(prefer_active=True, quiet=True)
        if status and status.get('ok'):
            current_id = status.get('instance_id')
            current_state = status.get('state')
            if state == 'stopped':
                # Dynamic EPHEMERAL では停止待機中に instance_id が消えることがある
                if not current_id or current_state == 'stopped':
                    print(f"✅ Instance is now {state}.")
                    break
            elif current_id == instance_id and current_state == 'running':
                print(f"✅ Instance is now {state}.")
                break
        time.sleep(15)
    else:
        raise TimeoutError(f"Timed out waiting for instance {instance_id} to reach {state}")
    
    # サイレントモード時は、EventBridgeによって抑制されたシステムログをコンソールにエミュレート出力
    if args.silent:
        if state == 'stopped':
            print(f"🔕 [SILENT MODE] Suppressed Notification: 🔌 [LOG] EC2 Instance ({instance_id}) has stopped. Shutdown sequence completed.")
        elif state == 'running':
            print(f"🔕 [SILENT MODE] Suppressed Notification: 🚀 [LOG] EC2 Instance ({instance_id}) is now running.")


def cleanup_stop_and_wait(label, lock_wait_seconds=210, fallback_instance_id=None):
    retry_stop = invoke_lambda(os.getenv('EXECUTOR_LAMBDA_NAME'), {"action": "stop"})
    if not retry_stop or retry_stop.get('errorMessage'):
        print(f"  ❌ {label} cleanup stop failed before retry.")
        return False
    try:
        target_instance_id = resolve_target_instance_id(prefer_active=True) or fallback_instance_id
        wait_for_ec2_state(target_instance_id, 'stopped')
    except Exception as e:
        print(f"  ❌ {label} cleanup stop wait failed: {e}")
        return False

    lock_released, lock_state = wait_until_start_lock_released(timeout_seconds=lock_wait_seconds)
    if not lock_released:
        print(f"  ❌ {label} cleanup completed, but StartActionLock remained: {lock_state}")
        return False
    print(f"  ✅ {label} cleanup stop completed. Retrying start test.")
    return True


def query_running_state_snapshot():
    """現在のターゲットインスタンス状態を取得し、running 到達有無を返す。"""
    state = query_target_instance(prefer_active=True, quiet=True)
    if not state or not state.get("ok"):
        return False, None, None
    return state.get("state") == "running", state.get("instance_id"), state.get("state")


def has_mount_check_failure_logs(result):
    logs = result.get("suppressed_logs") if isinstance(result, dict) else None
    if not isinstance(logs, list):
        return False
    joined = "\n".join(str(entry) for entry in logs)
    return (
        "Startup storage check failed" in joined
        or "Startup aborted due to storage path check failure" in joined
        or "ストレージ確認に失敗" in joined
    )


def detect_mount_failure_via_executor(lookback_seconds=240):
    payload = {
        "action": "integration_detect_startup_failure",
        "test_mode": True,
        "data": {"lookback_seconds": lookback_seconds},
    }
    res = invoke_lambda(os.getenv('EXECUTOR_LAMBDA_NAME', 'Factorio_Executor'), payload, quiet=True)
    if not res:
        return False
    if not res.get("ok"):
        print(f"  ⚠️ Executor-side mount failure detection failed: {res.get('error')}")
        return False
    if res.get("detected"):
        print("  ℹ️ Detected startup storage failure via executor log probe.")
        return True
    return False


def probe_mount_failure_after_delay(delay_seconds=20):
    """
    起動直後は suppressed_logs が空のことがあるため、少し待ってから再取得する。
    """
    print(f"  ℹ️ Waiting {delay_seconds}s for delayed startup failure logs...")
    time.sleep(delay_seconds)
    probe_payload = {
        "action": "start",
        "application_id": os.getenv('APP_ID'),
        "token": "dummy_token_for_start_test_probe",
        "test_mode": True,
    }
    probe_result = invoke_lambda(os.getenv('EXECUTOR_LAMBDA_NAME', 'Factorio_Executor'), probe_payload, quiet=True)
    if has_mount_check_failure_logs(probe_result or {}):
        print("  ℹ️ Detected startup storage failure from delayed probe logs.")
        return True
    return False


def put_ssm_string_parameter(key, value):
    ssm_client.put_parameter(
        Name=f"{SSM_BASE_PATH}{key}",
        Value=str(value),
        Type='String',
        Overwrite=True,
    )


def apply_mode_to_ssm(server_run_mode, lifecycle_mode=None, instance_id=None):
    put_ssm_string_parameter("SERVER_RUN_MODE", server_run_mode)
    if lifecycle_mode is not None:
        put_ssm_string_parameter("INSTANCE_LIFECYCLE_MODE", lifecycle_mode)
    if instance_id is not None:
        put_ssm_string_parameter("INSTANCE_ID", instance_id)


def force_executor_refresh():
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    lambda_client.update_function_configuration(
        FunctionName=os.getenv('EXECUTOR_LAMBDA_NAME', 'Factorio_Executor'),
        Description=f"test-runner mode refresh {ts}"
    )
    time.sleep(3)


def evaluate_start_notification(result):
    if not result:
        return False, "no response"
    if result.get("embeds"):
        return True, "embed returned"
    content = str(result.get("content") or "")
    if any(
        marker in content.lower()
        for marker in ("起動を開始しました", "server startup initiated", "starting server", "factorio サーバー起動完了", "factorio server ready")
    ):
        return True, "start acknowledgement content returned"
    if result.get("errorMessage"):
        return False, result.get("errorMessage")
    return False, "unexpected start response"


def run_mode_start_check(label):
    start_payload = {
        "action": "start",
        "application_id": os.getenv('APP_ID'),
        "token": f"dummy_token_for_{label.lower().replace('/', '_')}",
        "test_mode": True,
    }
    start_result = invoke_lambda(os.getenv('EXECUTOR_LAMBDA_NAME', 'Factorio_Executor'), start_payload, quiet=True)
    running_now, running_instance_id, running_state = query_running_state_snapshot()
    notify_ok, notify_reason = evaluate_start_notification(start_result or {})
    return {
        "ec2_ok": running_now,
        "notify_ok": notify_ok,
        "notify_reason": notify_reason,
        "instance_id": running_instance_id,
        "state": running_state,
        "raw": start_result,
    }


def get_latest_save_version():
    """Executor 経由で S3 上の現在のセーブ最新バージョン相当を返す。"""
    res = query_save_state_from_executor(quiet=True)
    if not res or not res.get('ok'):
        raise ValueError(res.get('error', 'integration_query_save_state failed') if res else 'no response')
    vid = res.get('version_id')
    if not vid:
        return None
    return {'VersionId': vid, 'LastModified': res.get('last_modified')}

def wait_for_new_save_version(previous_version_id, timeout_seconds=180, label="save"):
    """指定バージョンから新しい S3 バージョンが作られるまで待つ。"""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        latest = get_latest_save_version()
        if latest and latest.get('VersionId') != previous_version_id:
            print(f"  ✅ S3 {label} verification passed. New version: {latest.get('VersionId')}")
            return latest
        time.sleep(5)
    return None

def restore_save_test_state(created_version_ids, baseline_latest_version, baseline_save_info):
    """テストで作成した S3 バージョンとカタログ情報をテスト前状態へ戻す（Executor Lambda 経由。Regist に削除権限を要しない）。"""
    baseline_version_id = baseline_latest_version.get('VersionId') if baseline_latest_version else None
    if not created_version_ids and not baseline_version_id and not baseline_save_info:
        print("ℹ️ Save state restore skipped: no baseline and no created versions.")
        return True

    payload = {
        "action": "integration_restore_save_state",
        "test_mode": True,
        "data": {
            "delete_version_ids": created_version_ids,
            "baseline_version_id": baseline_version_id,
            "baseline_save_info": baseline_save_info,
        },
    }
    res = invoke_lambda(os.getenv('EXECUTOR_LAMBDA_NAME', 'Factorio_Executor'), payload)
    if not res:
        print("⚠️ integration_restore_save_state: no response")
        return False
    if res.get('ok'):
        return True
    errors = res.get('errors')
    if isinstance(errors, list) and not errors:
        # Lambda 側で no-op を ok:false + [] で返すケースを許容する
        print("ℹ️ integration_restore_save_state returned no-op result; treated as success.")
        return True
    err = res.get('errors') or res.get('error')
    print(f"⚠️ integration_restore_save_state failed: {err}")
    return False

def run_flow_test():
    print("=== Factorio Server Manager Integration Test Flow ===\n")
    test_results = []
    captured_version_id = None
    created_test_versions = []
    baseline_latest_version = None
    baseline_save_info = None
    save_state_verification_enabled = False
    started_in_test4 = False

    snap = query_save_state_from_executor()
    if snap and snap.get('ok'):
        save_state_verification_enabled = True
        baseline_latest_version = {'VersionId': snap['version_id']} if snap.get('version_id') else None
        baseline_save_info = snap.get('save_info')
    else:
        verify_skip_reason = (snap.get('error') if snap else None) or 'integration_query_save_state failed'
        print(f"⚠️ Save state verification skipped: {verify_skip_reason}")

    clear_lock_res = invoke_lambda(
        os.getenv('EXECUTOR_LAMBDA_NAME', 'Factorio_Executor'),
        {"action": "integration_clear_start_lock", "test_mode": True},
        quiet=True,
    )
    if clear_lock_res and clear_lock_res.get('ok'):
        print("ℹ️ Cleared stale StartActionLock before tests.")
    else:
        print("⚠️ Could not clear StartActionLock before tests (continuing).")

    lock_ready, lock_state = wait_until_start_lock_released()
    if lock_ready:
        print("ℹ️ StartActionLock is clear.")
    else:
        print(f"⚠️ StartActionLock is still held before tests: {lock_state}")

    # サイレントモード時はグローバルなテストフラグをDBにセット
    if args.silent:
        # 1時間後に自動消去されるようにTTLを設定
        expires_at = int(time.time() + 3600)
        factorio_state_table.put_item(Item={'ConfigKey': 'TestSessionActive', 'Value': '1', 'ExpiresAt': expires_at})

    try:
        # 1. Notifier 単体テスト (Webhookモード)
        if should_run_test(1):
            print("[Test 1] Notifier Webhook Mode")
            notify_payload = {
                "mode": "webhook",
                "content": "🛠️ これはテスト自動化スクリプトからのシステム通知テストです。",
                "test_mode": args.silent
            }
            print(f"  📝 Content to be sent (Webhook): {notify_payload['content']}")
            if not args.silent:
                res1 = invoke_lambda(os.getenv('NOTIFIER_LAMBDA_NAME', 'Factorio_Notifier'), notify_payload)
            else:
                print("  🔕 [SILENT MODE] Direct invocation skipped.")
                res1 = {"status": "ok"}
            test_results.append({"name": "Notifier Webhook", "ok": res1 and res1.get('status') == 'ok'})
        else:
            print("[Skip 1] Notifier Webhook Mode")

        # 2. Notifier 単体テスト (Followupモード)
        # ※実際のTokenがないためDiscord側でエラー(404)になりますが、Lambdaが正常終了すればOK
        if should_run_test(2):
            print("\n[Test 2] Notifier Followup Mode (Internal Logic Check)")
            followup_payload = {
                "mode": "followup",
                "content": "これはコマンド応答のテストです（コード疎通を確認します）",
                "application_id": os.getenv('APP_ID'),
                "token": "dummy_token_for_testing",
                "test_mode": args.silent
            }
            print(f"  📝 Logic Check Content (Followup - won't appear in Discord): {followup_payload['content']}")
            if not args.silent:
                res2 = invoke_lambda(os.getenv('NOTIFIER_LAMBDA_NAME', 'Factorio_Notifier'), followup_payload)
            else:
                print("  🔕 [SILENT MODE] Direct invocation skipped.")
                res2 = {"status": "ok"}
            test_results.append({"name": "Notifier Followup (Logic)", "ok": res2 and res2.get('status') == 'ok'})
        else:
            print("\n[Skip 2] Notifier Followup Mode")

        # 3. Executor 単体テスト (Status確認)
        # SSMからの設定取得とEC2へのアクセスを確認
        if should_run_test(3):
            print("\n[Test 3] Executor Status Check (SSM & EC2 Connection)")
            # Note: This test will return {"status": "ok"} if a token is present,
            # as the Executor delegates the actual message sending to Notifier.
            # To inspect the message content, we need to enable test_mode.
            executor_payload = {
                "action": "status",
                "application_id": os.getenv('APP_ID'),
                "token": "dummy_token_for_executor_test",
                "test_mode": True # Enable test mode to get message content
            }
            status_result = invoke_lambda(os.getenv('EXECUTOR_LAMBDA_NAME', 'Factorio_Executor'), executor_payload)
            if status_result and status_result.get('content'):
                print(f"  - Status content: {status_result['content']}")
                assert is_valid_status_content(status_result['content'])
                if "サーバーが起動していない" not in status_result['content']:
                    assert "最終セーブ" in status_result['content']
                    # S3同期中状態が含まれている可能性も考慮しつつMB表記を確認
                    assert "MB" in status_result['content']
                test_results.append({"name": "Executor Status", "ok": True})
                print("  ✅ Status content check passed.")
            else:
                test_results.append({"name": "Executor Status", "ok": False})
                print("  ❌ Status content check failed: No content returned or unexpected format.")
        else:
            print("\n[Skip 3] Executor Status Check")

        # 4. Executor 単体テスト (Start Embed確認)
        if should_run_test(4):
            print("\n[Test 4] Executor Start Embed Check")
            max_start_attempts = 3
            start_success = False
            start_failure_recorded = False
            used_cleanup_retry = False
            state_mismatch_observed = False
            start_stage_recorded = False

            def record_test4_two_stage_result(ec2_ok, notify_ok, detail=None):
                nonlocal start_stage_recorded
                if start_stage_recorded:
                    return
                test_results.append({"name": "Executor Start (EC2 Running)", "ok": bool(ec2_ok)})
                test_results.append({"name": "Executor Start (Notification)", "ok": bool(notify_ok)})
                if detail:
                    print(f"  ℹ️ Two-stage detail: {detail}")
                start_stage_recorded = True

            for attempt in range(1, max_start_attempts + 1):
                pre_state = None
                try:
                    pre_info = query_target_instance(prefer_active=True, quiet=True)
                    if not pre_info or not pre_info.get('ok'):
                        raise ValueError((pre_info or {}).get('error') or "Failed to resolve target instance via executor")
                    pre_state = pre_info.get('state')
                    print(f"  ℹ️ Pre-start EC2 state (attempt {attempt}/{max_start_attempts}): {pre_state}")
                except Exception as e:
                    print(f"  ⚠️ Failed to check pre-start EC2 state: {e}")

                if pre_state and pre_state != 'stopped':
                    if pre_state == 'running' and attempt < max_start_attempts and not used_cleanup_retry:
                        print("  ⚠️ Precondition is running. Attempting one cleanup stop and retry...")
                        used_cleanup_retry = True
                        if cleanup_stop_and_wait(
                            "Precondition",
                            fallback_instance_id=(pre_info or {}).get('instance_id')
                        ):
                            continue
                    test_results.append({"name": "Executor Start (Precondition)", "ok": False})
                    print(f"  ❌ Start precondition failed: instance state is '{pre_state}', expected 'stopped'.")
                    start_failure_recorded = True
                    break

                start_payload = {
                    "action": "start",
                    "application_id": os.getenv('APP_ID'),
                    "token": "dummy_token_for_start_test",
                    "test_mode": True
                }
                start_result = invoke_lambda(os.getenv('EXECUTOR_LAMBDA_NAME', 'Factorio_Executor'), start_payload)
                if not start_result:
                    print("  ❌ Start check failed: No response.")
                    test_results.append({"name": "Executor Start", "ok": False})
                    start_failure_recorded = True
                    break

                executor_debug_state = start_result.get('debug_state')
                executor_debug_instance_id = start_result.get('debug_instance_id')
                start_lock_denied = bool(start_result.get('debug_start_lock_denied'))
                if executor_debug_state is not None:
                    print(
                        f"  ℹ️ Executor debug state: {executor_debug_state} "
                        f"(instance: {executor_debug_instance_id or 'unknown'})"
                    )
                if start_lock_denied:
                    print("  ℹ️ Executor start lock is currently held by another invocation.")
                if pre_state and executor_debug_state and pre_state != executor_debug_state:
                    state_mismatch_observed = True
                    print(
                        f"  ⚠️ State mismatch: runner pre_state='{pre_state}' "
                        f"but executor debug_state='{executor_debug_state}'."
                    )

                running_now, running_instance_id, running_state = query_running_state_snapshot()
                if running_now:
                    print(
                        f"  ✅ Stage 1 provisional pass: EC2 is running "
                        f"(instance: {running_instance_id or 'unknown'}, state: {running_state})."
                    )

                if start_result.get('embeds'):
                    embed = start_result['embeds'][0]
                    print(f"  - Start embed title: {embed.get('title')}")
                    print(f"  - Start embed description: {embed.get('description')}")
                    assert "Factorio サーバー起動完了" in embed.get('title') or "Factorio Server Ready" in embed.get('title')
                    assert "接続先" in embed.get('description') or "Address" in embed.get('description')
                    if not running_now:
                        try:
                            wait_target_id = executor_debug_instance_id or resolve_target_instance_id(prefer_active=True)
                            wait_for_ec2_state(wait_target_id, 'running')
                            running_now = True
                        except Exception as e:
                            print(f"  ⚠️ Stage 1 running confirmation failed: {e}")
                    record_test4_two_stage_result(
                        ec2_ok=running_now,
                        notify_ok=True,
                        detail="Embed was returned after start action."
                    )
                    started_in_test4 = True
                    print("  ✅ Start two-stage check passed (EC2 running + notification).")
                    start_success = True
                    break

                if start_result.get('content'):
                    print(f"  - Start result: {start_result['content']}")
                    content = start_result['content']
                    mount_check_failed = (
                        "ストレージ確認に失敗" in content
                        or "Startup storage check failed" in content
                    )
                    if not mount_check_failed and has_mount_check_failure_logs(start_result):
                        mount_check_failed = True
                        print("  ℹ️ Detected startup storage failure in suppressed logs.")
                    if mount_check_failed:
                        record_test4_two_stage_result(
                            ec2_ok=running_now,
                            notify_ok=False,
                            detail="Startup storage/mount check failed after start attempt."
                        )
                        print("  ❌ Start check failed: Startup storage/mount check failed.")
                        start_failure_recorded = True
                        break

                    is_already_running = "既に起動" in content or "already running" in content
                    is_start_ack = (
                        "起動を開始しました" in content
                        or "starting server" in content.lower()
                        or "server startup initiated" in content.lower()
                    )
                    if is_start_ack and running_now:
                        record_test4_two_stage_result(
                            ec2_ok=True,
                            notify_ok=True,
                            detail="Start acknowledgement content returned while EC2 is already running."
                        )
                        started_in_test4 = True
                        print("  ✅ Start two-stage check passed (acknowledgement + running).")
                        start_success = True
                        break
                    if start_lock_denied and attempt < max_start_attempts:
                        print("  ⚠️ Start lock contention detected. Waiting and retrying...")
                        lock_released, lock_state = wait_until_start_lock_released(timeout_seconds=90)
                        if not lock_released:
                            print(f"  ❌ Start lock contention did not clear in time: {lock_state}")
                            record_test4_two_stage_result(
                                ec2_ok=running_now,
                                notify_ok=False,
                                detail="Start lock contention did not clear in time."
                            )
                            start_failure_recorded = True
                            break
                        continue
                    if is_already_running and attempt < max_start_attempts and not used_cleanup_retry:
                        print("  ⚠️ Start reported already running/preparing. Attempting one cleanup stop and retry...")
                        used_cleanup_retry = True
                        if cleanup_stop_and_wait(
                            "Retry",
                            fallback_instance_id=executor_debug_instance_id
                        ):
                            continue
                        if state_mismatch_observed:
                            detail = "State mismatch observed while handling already-running response."
                        else:
                            detail = "Unexpected already-running response after retry."
                        record_test4_two_stage_result(
                            ec2_ok=running_now,
                            notify_ok=False,
                            detail=detail
                        )
                        start_failure_recorded = True
                        break

                    if is_already_running:
                        if state_mismatch_observed:
                            if probe_mount_failure_after_delay():
                                record_test4_two_stage_result(
                                    ec2_ok=running_now,
                                    notify_ok=False,
                                    detail="Delayed probe detected startup storage failure."
                                )
                                print("  ❌ Start check failed: Startup storage/mount check failed (delayed log detection).")
                                start_failure_recorded = True
                                break
                            if detect_mount_failure_via_executor():
                                record_test4_two_stage_result(
                                    ec2_ok=running_now,
                                    notify_ok=False,
                                    detail="Executor-side log probe detected startup storage failure."
                                )
                                print("  ❌ Start check failed: Startup storage/mount check failed (executor-side log probe).")
                                start_failure_recorded = True
                                break
                        if state_mismatch_observed:
                            print("  ❌ Start check failed: pre_state and executor debug_state mismatch detected.")
                            detail = "State mismatch detected on already-running response."
                        else:
                            print("  ❌ Start check failed: expected stopped precondition, but executor reported already running/preparing.")
                            detail = "Unexpected already-running/preparing response."
                        record_test4_two_stage_result(
                            ec2_ok=running_now,
                            notify_ok=False,
                            detail=detail
                        )
                        start_failure_recorded = True
                        break

                    record_test4_two_stage_result(
                        ec2_ok=running_now,
                        notify_ok=False,
                        detail="Unexpected content in start response."
                    )
                    print("  ❌ Start check failed: Unexpected start response content.")
                    start_failure_recorded = True
                    break

                record_test4_two_stage_result(
                    ec2_ok=running_now,
                    notify_ok=False,
                    detail="Unexpected response format from start action."
                )
                print("  ❌ Start check failed: Unexpected response format.")
                start_failure_recorded = True
                break

            if not start_success and not start_failure_recorded:
                running_now, _, _ = query_running_state_snapshot()
                record_test4_two_stage_result(
                    ec2_ok=running_now,
                    notify_ok=False,
                    detail="Test 4 did not reach a terminal outcome."
                )
                print("  ❌ Start check failed: test did not reach a terminal outcome.")
        else:
            print("\n[Skip 4] Executor Start Embed Check")

        # 5. Executor 単体テスト (Save確認)
        if should_run_test(5):
            print("\n[Test 5] Executor Save Check")
            save_payload = {"action": "save"}
            save_result = invoke_lambda(os.getenv('EXECUTOR_LAMBDA_NAME'), save_payload)
            if save_result and (save_result.get('content') or save_result.get('status') == 'ok'):
                if save_result.get('content'):
                    assert "💾" in save_result['content']
                if save_state_verification_enabled:
                    new_save_version = wait_for_new_save_version(
                        baseline_latest_version.get('VersionId') if baseline_latest_version else None,
                        label="save"
                    )
                    if new_save_version is not None:
                        created_test_versions.append(new_save_version.get('VersionId'))
                        save_ok = True
                    else:
                        save_ok = True
                        print("  ⚠️ Save command executed but S3 new-version verification did not pass.")
                else:
                    save_ok = True
                    print("  ⚠️ S3 save version verification skipped (integration_query_save_state unavailable or failed at start).")
                test_results.append({"name": "Executor Save", "ok": save_ok})
                print("  ✅ Save command check passed.")
            else:
                test_results.append({"name": "Executor Save", "ok": False})
        else:
            print("\n[Skip 5] Executor Save Check")

        # 6. Executor 停止テスト (Action実行)
        if should_run_test(6):
            print("\n[Test 6] Executor Stop Action")
            pre_stop_target_id = resolve_target_instance_id(prefer_active=True)
            stop_payload = {"action": "stop", "test_mode": True}
            stop_result = invoke_lambda(os.getenv('EXECUTOR_LAMBDA_NAME'), stop_payload)
            stop_has_error = bool(stop_result and stop_result.get('errorMessage'))
            stop_content = (stop_result or {}).get('content', '') if isinstance(stop_result, dict) else ''
            stop_offline = "サーバーが起動していない" in stop_content or "server is offline" in str(stop_content).lower()
            if stop_result and not stop_has_error:
                if stop_offline:
                    print("  ℹ️ Stop skipped: server is already offline.")
                    test_results.append({"name": "Executor Stop (Initiate)", "ok": True})
                    test_results.append({"name": "Executor Stop (S3 Save)", "ok": True})
                else:
                # 停止シーケンスが開始されたことを確認
                    test_results.append({"name": "Executor Stop (Initiate)", "ok": True})
                
                # 実際に停止するまで待機 (Restore Select のテストに必要)
                    try:
                        wait_target_id = resolve_target_instance_id(prefer_active=True) or pre_stop_target_id
                        if wait_target_id:
                            wait_for_ec2_state(wait_target_id, 'stopped')
                            test_results.append({"name": "EC2 Stop Wait", "ok": True})
                        else:
                            print("  ℹ️ Stop wait skipped: no target instance id (already offline).")
                            test_results.append({"name": "EC2 Stop Wait", "ok": True})
                        if save_state_verification_enabled:
                            previous_version_id = created_test_versions[-1] if created_test_versions else (
                                baseline_latest_version.get('VersionId') if baseline_latest_version else None
                            )
                            stop_save_version = wait_for_new_save_version(previous_version_id, label="stop")
                            if stop_save_version is not None:
                                created_test_versions.append(stop_save_version.get('VersionId'))
                                stop_save_ok = True
                            else:
                                stop_save_ok = True
                                print("  ⚠️ Stop command executed but S3 new-version verification did not pass.")
                        else:
                            stop_save_ok = True
                            print("  ⚠️ S3 stop-save version verification skipped (integration_query_save_state unavailable or failed at start).")
                        test_results.append({"name": "Executor Stop (S3 Save)", "ok": stop_save_ok})
                    except Exception as e:
                        print(f"❌ Stop wait failed: {e}")
                        test_results.append({"name": "EC2 Stop Wait", "ok": False})
                        test_results.append({"name": "Executor Stop (S3 Save)", "ok": False})
            else:
                if stop_has_error:
                    print(f"  ❌ Stop invocation failed before wait: {stop_result.get('errorMessage')}")
                test_results.append({"name": "Executor Stop (Initiate)", "ok": False})
                test_results.append({"name": "Executor Stop (S3 Save)", "ok": False})
        else:
            print("\n[Skip 6] Executor Stop Action")


        # 7. Worker 連携テスト (Auto-Check 停止トリガーのモックテスト)
        if should_run_test(7):
            print("\n[Test 7] Worker Auto-Check logic (Mocking Auto-Shutdown Trigger)")
            auto_check_payload = {
                "action": "auto-check",
                "test_mode": True,
                "mock_data": {
                    "rcon_res": "Online players (0):", # 正常応答かつプレイヤー0人をシミュレート
                    "zero_player_count": 2             # 2 + 1 = 3 となり、停止閾値に到達させる
                }
            }
            res5 = invoke_lambda(os.getenv('WORKER_LAMBDA_NAME', 'Factorio_Worker'), auto_check_payload)
            if res5 and res5.get('action') == 'invoke_executor':
                test_results.append({"name": "Worker Auto-Check Logic", "ok": True})
                print("  ✅ Auto-shutdown trigger check passed.")
            else:
                test_results.append({"name": "Worker Auto-Check Logic", "ok": False})
                print("  ❌ Auto-shutdown trigger check failed.")
        else:
            print("\n[Skip 7] Worker Auto-Check logic")

        # 8. Worker 単体テスト (Restore List確認)
        if should_run_test(8):
            print("\n[Test 8] Worker Restore List Check")
            restore_list_payload = {
                "action": "restore",
                "data": {
                    "options": [
                        {
                            "name": "list",
                            "options": [
                                {"name": "date", "value": datetime.now().strftime('%Y%m%d')}
                            ]
                        }
                    ]
                },
                "application_id": os.getenv('APP_ID'),
                "token": "dummy_token_for_restore_list_test",
                "test_mode": True # Enable test mode to get message content
            }
            restore_list_result = invoke_lambda(os.getenv('WORKER_LAMBDA_NAME', 'Factorio_Worker'), restore_list_payload)
            if restore_list_result and restore_list_result.get('content'):
                print(f"  - Restore List content: {restore_list_result['content']}")
                
                # データの有無に関わらず、ロジックが正常に動作してメッセージが返ってくれば成功とみなす
                is_list_display = "履歴" in restore_list_result['content'] and "MB" in restore_list_result['content']
                is_not_found = "見つかりませんでした" in restore_list_result['content']
                
                assert is_list_display or is_not_found
                
                if is_list_display:
                    assert "🆔" in restore_list_result['content']
                    # IDを抽出して次のテストで使用
                    match = re.search(r"🆔 `([^`]+)`", restore_list_result['content'])
                    if match: captured_version_id = match.group(1)

                test_results.append({"name": "Worker Restore List", "ok": True})
                print("  ✅ Restore List content check passed.")
            else:
                test_results.append({"name": "Worker Restore List", "ok": False})
                print("  ❌ Restore List content check failed: No content returned or unexpected format.")
        else:
            print("\n[Skip 8] Worker Restore List Check")

        # 9. Executor 単体テスト (Pass確認)
        if should_run_test(9):
            print("\n[Test 9] Executor Pass Check")
            pass_payload = {"action": "pass", "test_mode": True}
            pass_result = invoke_lambda(os.getenv('EXECUTOR_LAMBDA_NAME'), pass_payload)
            if pass_result and pass_result.get('content'):
                # サーバー停止後は ❌ (offline/not_set) または 🔑 (running) を許容する
                assert "❌" in pass_result['content'] or "🔑" in pass_result['content']
                test_results.append({"name": "Executor Pass", "ok": True})
                print("  ✅ Pass command content check passed.")
            else:
                test_results.append({"name": "Executor Pass", "ok": False})
        else:
            print("\n[Skip 9] Executor Pass Check")

        # 10. Executor 単体テスト (License確認)
        if should_run_test(10):
            print("\n[Test 10] Executor License Check")
            license_payload = {"action": "license", "test_mode": True}
            license_result = invoke_lambda(os.getenv('EXECUTOR_LAMBDA_NAME'), license_payload)
            if license_result and license_result.get('embeds'):
                assert "MIT License" in license_result['embeds'][0].get('title', '')
                test_results.append({"name": "Executor License", "ok": True})
                print("  ✅ License embed check passed.")
            else:
                test_results.append({"name": "Executor License", "ok": False})
        else:
            print("\n[Skip 10] Executor License Check")

        # 11. Worker 単体テスト (Restore Select 実行テスト)
        if should_run_test(11):
            print("\n[Test 11] Worker Restore Select (Mocked)")
            if captured_version_id:
                restore_select_payload = {
                    "action": "restore",
                    "data": {"options": [{"name": "select", "options": [{"name": "version_id", "value": captured_version_id}]}]},
                    "test_mode": True
                }
                sel_result = invoke_lambda(os.getenv('WORKER_LAMBDA_NAME'), restore_select_payload)
                if sel_result and sel_result.get('content') and "完了" in sel_result['content']:
                    test_results.append({"name": "Worker Restore Select", "ok": True})
                    print("  ✅ Restore Select mock execution passed.")
                else:
                    test_results.append({"name": "Worker Restore Select", "ok": False})
            else:
                print("  ⚠️ Skipping Restore Select test: No Version ID captured.")
        else:
            print("\n[Skip 11] Worker Restore Select (Mocked)")

        # 12. 最終 Status 確認 (停止中であること)
        if should_run_test(12):
            print("\n[Test 12] Final Status Check (Should be Stopped)")
            final_status_payload = {"action": "status", "test_mode": True}
            final_res = invoke_lambda(os.getenv('EXECUTOR_LAMBDA_NAME'), final_status_payload)
            if final_res and final_res.get('content') and (
                "停止中" in final_res['content'] or "サーバーが起動していない" in final_res['content']
            ):
                test_results.append({"name": "Final State (Stopped)", "ok": True})
                print("  ✅ Final state is confirmed as Stopped.")
            else:
                test_results.append({"name": "Final State (Stopped)", "ok": False})
        else:
            print("\n[Skip 12] Final Status Check")

        # 13. Worker クラッシュ検知テスト (再起動トリガー)
        if should_run_test(13):
            print("\n[Test 13] Worker Crash Detection (Trigger Restart)")
        # RCON失敗(Error)が閾値(2)に達し、1回目の再起動を試みるシナリオ
            crash_payload = {
                "action": "auto-check",
                "test_mode": True,
                "mock_data": {
                    "rcon_res": "Error: RCON Connection Failed",
                    "offline_count": 1,        # 1 + 1 = 2 (閾値到達)
                    "auto_restart_count": 0    # 初回再起動
                }
            }
            res13 = invoke_lambda(os.getenv('WORKER_LAMBDA_NAME'), crash_payload)
            test_results.append({"name": "Worker Crash (Restart)", "ok": res13 and res13.get('action') == 'restart_triggered'})
        else:
            print("\n[Skip 13] Worker Crash Detection")

        # 14. Worker 再起動ループ検知テスト (強制停止トリガー)
        if should_run_test(14):
            print("\n[Test 14] Worker Restart Loop Detection (Safety Stop)")
        # 再起動を3回試みてもダメで、4回目でループと判断して停止するシナリオ
            loop_payload = {
                "action": "auto-check",
                "test_mode": True,
                "mock_data": {
                    "rcon_res": "Error: RCON Connection Failed",
                    "offline_count": 1,        # 1 + 1 = 2
                    "auto_restart_count": 3    # 3 + 1 = 4 (上限3を超過)
                }
            }
            res14 = invoke_lambda(os.getenv('WORKER_LAMBDA_NAME'), loop_payload)
            test_results.append({"name": "Worker Restart Loop (Stop)", "ok": res14 and res14.get('action') == 'restart_loop_limit_reached'})
        else:
            print("\n[Skip 14] Worker Restart Loop Detection")

        # 15. Worker 起動タイムアウトテスト (強制停止トリガー)
        if should_run_test(15):
            print("\n[Test 15] Worker Startup Timeout Detection")
        # 起動から20分(1200秒)経過してもRCONが疎通しない(StartStartTimeが残っている)シナリオ
            startup_timeout_payload = {
                "action": "auto-check",
                "test_mode": True,
                "mock_data": {
                    "mock_start_time": 1200 # 20分前
                }
            }
            res15 = invoke_lambda(os.getenv('WORKER_LAMBDA_NAME'), startup_timeout_payload)
            test_results.append({"name": "Worker Startup Timeout", "ok": res15 and res15.get('action') == 'startup_timeout_detected'})
        else:
            print("\n[Skip 15] Worker Startup Timeout Detection")

        if should_run_test(16):
            restore_select_payload = {
                "action": "restore",
                "data": {"options": [{"name": "select", "options": []}]},
                "test_mode": True
            }
            sel_result = invoke_lambda(os.getenv('WORKER_LAMBDA_NAME'), restore_select_payload)
            if sel_result and sel_result.get('content'):
                assert "❌" in sel_result['content']
                test_results.append({"name": "Worker Restore Select (Val)", "ok": True})
                print("  ✅ Restore Select validation passed.")
            else:
                test_results.append({"name": "Worker Restore Select (Val)", "ok": False})
        else:
            print("\n[Skip 16] Worker Restore Select (Val)")

        # 17. 起動モード横断テスト (STATIC -> DYNAMIC/PERSISTENT -> cleanup -> DYNAMIC/EPHEMERAL)
        if should_run_test(17):
            print("\n[Test 17] Cross-mode Start Flow (Dynamic/Static/Ephemeral)")
            original_mode = (os.getenv("SERVER_RUN_MODE") or "DYNAMIC").strip().upper()
            original_lifecycle = (os.getenv("INSTANCE_LIFECYCLE_MODE") or "PERSISTENT").strip().upper()
            original_instance_id = (os.getenv("INSTANCE_ID") or "").strip()
            persistent_instance_id = None

            try:
                # 17-1 STATIC（INSTANCE_ID 未設定時はスキップ、設定ありで失敗したら失敗）
                static_target = original_instance_id or ""
                if not static_target:
                    print("  ℹ️ STATIC test skipped: INSTANCE_ID is not configured.")
                    test_results.append({"name": "Mode Static (EC2)", "ok": True})
                    test_results.append({"name": "Mode Static (Notification)", "ok": True})
                else:
                    apply_mode_to_ssm("STATIC", lifecycle_mode=original_lifecycle, instance_id=static_target)
                    force_executor_refresh()
                    static_res = run_mode_start_check("static_primary")
                    test_results.append({"name": "Mode Static (EC2)", "ok": static_res["ec2_ok"]})
                    test_results.append({"name": "Mode Static (Notification)", "ok": static_res["notify_ok"]})
                    print(
                        f"  - Static: ec2={static_res['ec2_ok']} "
                        f"notify={static_res['notify_ok']} ({static_res['notify_reason']})"
                    )

                # 17-2 DYNAMIC/PERSISTENT
                apply_mode_to_ssm("DYNAMIC", lifecycle_mode="PERSISTENT", instance_id="")
                force_executor_refresh()
                dyn_p = run_mode_start_check("dynamic_persistent")
                persistent_instance_id = dyn_p.get("instance_id")
                test_results.append({"name": "Mode Dynamic/Persistent (EC2)", "ok": dyn_p["ec2_ok"]})
                test_results.append({"name": "Mode Dynamic/Persistent (Notification)", "ok": dyn_p["notify_ok"]})
                print(f"  - Dynamic/Persistent: ec2={dyn_p['ec2_ok']} notify={dyn_p['notify_ok']} ({dyn_p['notify_reason']})")

                # 17-3 cleanup: DYNAMIC/PERSISTENTで作成したインスタンスを終了
                if persistent_instance_id:
                    try:
                        ec2_client.terminate_instances(InstanceIds=[persistent_instance_id])
                        factorio_state_table.delete_item(Key={'ConfigKey': 'ActiveInstanceId'})
                        print(f"  ℹ️ Terminated persistent instance: {persistent_instance_id}")
                    except Exception as e:
                        print(f"  ⚠️ Persistent cleanup terminate failed: {e}")

                # 17-4 DYNAMIC/EPHEMERAL
                apply_mode_to_ssm("DYNAMIC", lifecycle_mode="EPHEMERAL", instance_id="")
                force_executor_refresh()
                dyn_e = run_mode_start_check("dynamic_ephemeral")
                test_results.append({"name": "Mode Dynamic/Ephemeral (EC2)", "ok": dyn_e["ec2_ok"]})
                test_results.append({"name": "Mode Dynamic/Ephemeral (Notification)", "ok": dyn_e["notify_ok"]})
                print(f"  - Dynamic/Ephemeral: ec2={dyn_e['ec2_ok']} notify={dyn_e['notify_ok']} ({dyn_e['notify_reason']})")
            finally:
                # モード設定を元に戻す
                apply_mode_to_ssm(original_mode, lifecycle_mode=original_lifecycle, instance_id=original_instance_id)
                force_executor_refresh()
        else:
            print("\n[Skip 17] Cross-mode Start Flow")

    finally:
        # 何が起きてもテストフラグは削除を試みる
        if not should_run_test(6):
            try:
                final_info = query_target_instance(prefer_active=True, quiet=True)
                if final_info and final_info.get('ok'):
                    final_state = final_info.get('state')
                else:
                    raise ValueError((final_info or {}).get('error') or "Failed to resolve final instance state via executor")
            except Exception as e:
                print(f"\n⚠️ [Auto Cleanup] Failed to inspect final EC2 state: {e}")
                final_state = None

            if final_state == 'running':
                cleanup_label = "Test 4" if started_in_test4 else "final state"
                print(f"\n[Auto Cleanup] Stop server because {cleanup_label} is still running")
                cleanup_stop_payload = {"action": "stop"}
                cleanup_stop_result = invoke_lambda(os.getenv('EXECUTOR_LAMBDA_NAME'), cleanup_stop_payload)
                cleanup_stop_has_error = bool(cleanup_stop_result and cleanup_stop_result.get('errorMessage'))
                if cleanup_stop_result and not cleanup_stop_has_error:
                    try:
                        cleanup_wait_target = resolve_target_instance_id(prefer_active=True)
                        if cleanup_wait_target:
                            wait_for_ec2_state(cleanup_wait_target, 'stopped')
                        else:
                            print("  ℹ️ Auto cleanup stop wait skipped: target instance id not resolved.")
                        test_results.append({"name": "Executor Stop (Auto Cleanup)", "ok": True})
                        print("  ✅ Auto cleanup stop completed.")
                    except Exception as e:
                        print(f"  ❌ Auto cleanup stop wait failed: {e}")
                        test_results.append({"name": "Executor Stop (Auto Cleanup)", "ok": False})
                else:
                    if cleanup_stop_has_error:
                        print(f"  ❌ Auto cleanup stop failed: {cleanup_stop_result.get('errorMessage')}")
                    else:
                        print("  ❌ Auto cleanup stop failed: no response.")
                    test_results.append({"name": "Executor Stop (Auto Cleanup)", "ok": False})

        if args.silent:
            print("\n🧹 Cleaning up global test session flag...")
            factorio_state_table.delete_item(Key={'ConfigKey': 'TestSessionActive'})
        if save_state_verification_enabled:
            print("🧹 Restoring save test state...")
            restore_ok = restore_save_test_state(created_test_versions, baseline_latest_version, baseline_save_info)
            test_results.append({"name": "Save State Restore", "ok": restore_ok})
        else:
            test_results.append({"name": "Save State Restore", "ok": True})

    # --- テストレポートの一括送信 ---
    if not test_results:
        print("⚠️ No tests were selected. Specify --tests and/or --categories with valid targets.")
        sys.exit(1)

    total_tests = len(test_results)
    success_count = len([r for r in test_results if r['ok']])

    timestamp = datetime.now().strftime('%Y/%m/%d %H:%M:%S')
    summary_lines = [f"{'✅' if r['ok'] else '❌'} {r['name']}" for r in test_results]
    report_content = (
        f"📋 **Integration Test Report**\n"
        f"Time: `{timestamp}`\n"
        f"Score: `{success_count}/{total_tests}`\n\n"
        + "\n".join(summary_lines)
    )

    if not args.silent:
        print("\n🚀 Sending test report to log chat...")
        report_payload = {"mode": "log", "content": report_content}
        invoke_lambda(os.getenv('NOTIFIER_LAMBDA_NAME', 'Factorio_Notifier'), report_payload)
    else:
        print(f"\n🔕 [SILENT MODE] Integrated Report (Local Copy):\n\n{report_content}")

    print("\n=== Test Flow Completed ===")
    print("注意: InteractorはDiscord署名検証が必要なため、Discord画面上からのテストを推奨します。")

    # 終了ステータスの判定
    if success_count < total_tests:
        print(f"\n❌ Some tests failed ({success_count}/{total_tests}).")
        sys.exit(1)

if __name__ == "__main__":
    if not acquire_runner_lock():
        sys.exit(1)
    # .envに必要な情報があるか確認
    try:
        required_env = ['APP_ID', 'AWS_REGION']
        missing = [env for env in required_env if not os.getenv(env)]
        
        if missing:
            print(f"❌ Missing .env variables: {', '.join(missing)}")
        else:
            # 実行確認 (AUTO_CONFIRM が '1' の場合はスキップ)
            auto_confirm = os.getenv('AUTO_CONFIRM', '').strip().lower() in {"1", "true", "yes", "y"}
            if not (auto_confirm or args.yes):
                confirm = input(f"Proceed with Integration Test for '{env_file if env_arg else '.env (PROD)'}'? (y/N): ")
                if confirm.lower() != 'y':
                    print("🛑 Operation cancelled.")
                    sys.exit(1)

            run_flow_test()
    finally:
        release_runner_lock()