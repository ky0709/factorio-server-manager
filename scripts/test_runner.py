import boto3
import json
import os
from dotenv import load_dotenv
import sys
import re
from datetime import datetime

# プロジェクトルートの.envを読み込み
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 環境選択 (例: python test_runner.py dev)
env_arg = sys.argv[1] if len(sys.argv) > 1 else "prod"
env_file = ".env" if env_arg == "prod" else f".env.{env_arg}"
env_path = os.path.join(BASE_DIR, env_file)

if os.path.exists(env_path):
    print(f"📖 Loading environment: {env_file}")
    load_dotenv(env_path)
else:
    load_dotenv(os.path.join(BASE_DIR, ".env"))

lambda_client = boto3.client('lambda', region_name=os.getenv('AWS_REGION', 'ap-northeast-1'))
ec2_client = boto3.client('ec2', region_name=os.getenv('AWS_REGION', 'ap-northeast-1'))

def invoke_lambda(function_name, payload):
    print(f"🚀 Invoking {function_name}...")
    try:
        response = lambda_client.invoke(
            FunctionName=function_name,
            InvocationType='RequestResponse',  # 結果を確認するため同期実行
            Payload=json.dumps(payload)
        )
        result = json.loads(response['Payload'].read().decode('utf-8'))
        print(f"✅ Response from {function_name}: {json.dumps(result, indent=2, ensure_ascii=False)}")
        return result
    except Exception as e:
        print(f"❌ Failed to invoke {function_name}: {e}")
        return None

def wait_for_ec2_state(instance_id, state):
    print(f"⏳ Waiting for instance {instance_id} to reach state: {state}...")
    if state == 'stopped':
        waiter = ec2_client.get_waiter('instance_stopped')
    elif state == 'running':
        waiter = ec2_client.get_waiter('instance_running')
    else:
        return
    waiter.wait(InstanceIds=[instance_id], WaiterConfig={'Delay': 15, 'MaxAttempts': 40})
    print(f"✅ Instance is now {state}.")

def run_flow_test():
    print("=== Factorio Server Manager Integration Test Flow ===\n")
    test_results = []
    captured_version_id = None

    # 1. Notifier 単体テスト (Webhookモード)
    print("[Test 1] Notifier Webhook Mode")
    notify_payload = {
        "mode": "webhook",
        "content": "🛠️ これはテスト自動化スクリプトからのシステム通知テストです。"
    }
    res1 = invoke_lambda(os.getenv('NOTIFIER_LAMBDA_NAME', 'Factorio_Notifier'), notify_payload)
    test_results.append({"name": "Notifier Webhook", "ok": res1 and res1.get('status') == 'ok'})

    # 2. Notifier 単体テスト (Followupモード)
    # ※実際のTokenがないためDiscord側でエラー(404)になりますが、Lambdaが正常終了すればOK
    print("\n[Test 2] Notifier Followup Mode (Internal Logic Check)")
    followup_payload = {
        "mode": "followup",
        "content": "これはコマンド応答のテストです（Tokenが無効なためDiscord側で404になる可能性がありますが、コード疎通を確認します）",
        "application_id": os.getenv('APP_ID'),
        "token": "dummy_token_for_testing"
    }
    res2 = invoke_lambda(os.getenv('NOTIFIER_LAMBDA_NAME', 'Factorio_Notifier'), followup_payload)
    test_results.append({"name": "Notifier Followup (Logic)", "ok": res2 and res2.get('status') == 'ok'})

    # 3. Executor 単体テスト (Status確認)
    # SSMからの設定取得とEC2へのアクセスを確認
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
        assert "稼働中" in status_result['content'] or "停止中" in status_result['content']
        assert "最終セーブ" in status_result['content']
        # S3同期中状態が含まれている可能性も考慮しつつMB表記を確認
        assert "MB" in status_result['content']
        test_results.append({"name": "Executor Status", "ok": True})
        print("  ✅ Status content check passed.")
    else:
        test_results.append({"name": "Executor Status", "ok": False})
        print("  ❌ Status content check failed: No content returned or unexpected format.")

    # 4. Executor 単体テスト (Start Embed確認)
    print("\n[Test 4] Executor Start Embed Check")
    # Note: This requires the server to be stopped for the start action to proceed.
    # Mocking EC2 state and RCON is complex for this runner.
    # This test primarily checks the structure of the embed if it were to be sent.
    start_payload = {
        "action": "start",
        "application_id": os.getenv('APP_ID'),
        "token": "dummy_token_for_start_test",
        "test_mode": True # Enable test mode to get message content
    }
    start_result = invoke_lambda(os.getenv('EXECUTOR_LAMBDA_NAME', 'Factorio_Executor'), start_payload)
    if start_result:
        if start_result.get('embeds'):
            embed = start_result['embeds'][0]
            print(f"  - Start embed title: {embed.get('title')}")
            print(f"  - Start embed description: {embed.get('description')}")
            assert "Factorio サーバー起動完了" in embed.get('title') or "Factorio Server Ready" in embed.get('title')
            assert "接続先" in embed.get('description') or "Address" in embed.get('description')
            test_results.append({"name": "Executor Start (Embed)", "ok": True})
            print("  ✅ Start embed check passed (Server was stopped).")
        elif start_result.get('content'):
            print(f"  - Start result: {start_result['content']}")
            assert "既に起動" in start_result['content'] or "already running" in start_result['content']
            test_results.append({"name": "Executor Start (Skip)", "ok": True})
            print("  ✅ Start check passed (Server already running).")
        else:
            test_results.append({"name": "Executor Start", "ok": False})
            print("  ❌ Start check failed: Unexpected response format.")
    else:
        print("  ❌ Start check failed: No response.")

    # 5. Executor 単体テスト (Save確認)
    print("\n[Test 5] Executor Save Check")
    save_payload = {"action": "save", "test_mode": True}
    save_result = invoke_lambda(os.getenv('EXECUTOR_LAMBDA_NAME'), save_payload)
    if save_result and save_result.get('content'):
        assert "💾" in save_result['content']
        test_results.append({"name": "Executor Save", "ok": True})
        print("  ✅ Save command content check passed.")
    else:
        test_results.append({"name": "Executor Save", "ok": False})

    # 6. Executor 停止テスト (Action実行)
    print("\n[Test 6] Executor Stop Action")
    stop_payload = {"action": "stop", "test_mode": True}
    stop_result = invoke_lambda(os.getenv('EXECUTOR_LAMBDA_NAME'), stop_payload)
    if stop_result:
        # 停止シーケンスが開始されたことを確認
        test_results.append({"name": "Executor Stop (Initiate)", "ok": True})
        
        # 実際に停止するまで待機 (Restore Select のテストに必要)
        try:
            wait_for_ec2_state(os.getenv('INSTANCE_ID'), 'stopped')
            test_results.append({"name": "EC2 Stop Wait", "ok": True})
        except Exception as e:
            print(f"❌ Stop wait failed: {e}")
            test_results.append({"name": "EC2 Stop Wait", "ok": False})
    else:
        test_results.append({"name": "Executor Stop (Initiate)", "ok": False})


    # 7. Worker 連携テスト (Auto-Check 停止トリガーのモックテスト)
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

    # 8. Worker 単体テスト (Restore List確認)
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
        assert "履歴" in restore_list_result['content']
        assert "MB" in restore_list_result['content']
        assert "ID:" in restore_list_result['content']
        # IDを抽出して次のテストで使用
        match = re.search(r"ID: `([^`]+)`", restore_list_result['content'])
        if match: captured_version_id = match.group(1)

        test_results.append({"name": "Worker Restore List", "ok": True})
        print("  ✅ Restore List content check passed.")
    else:
        test_results.append({"name": "Worker Restore List", "ok": False})
        print("  ❌ Restore List content check failed: No content returned or unexpected format.")

    # 9. Executor 単体テスト (Pass確認)
    print("\n[Test 9] Executor Pass Check")
    pass_payload = {"action": "pass", "test_mode": True}
    pass_result = invoke_lambda(os.getenv('EXECUTOR_LAMBDA_NAME'), pass_payload)
    if pass_result and pass_result.get('content'):
        assert "🔑" in pass_result['content']
        test_results.append({"name": "Executor Pass", "ok": True})
        print("  ✅ Pass command content check passed.")
    else:
        test_results.append({"name": "Executor Pass", "ok": False})

    # 10. Executor 単体テスト (License確認)
    print("\n[Test 10] Executor License Check")
    license_payload = {"action": "license", "test_mode": True}
    license_result = invoke_lambda(os.getenv('EXECUTOR_LAMBDA_NAME'), license_payload)
    if license_result and license_result.get('embeds'):
        assert "MIT License" in license_result['embeds'][0].get('title', '')
        test_results.append({"name": "Executor License", "ok": True})
        print("  ✅ License embed check passed.")
    else:
        test_results.append({"name": "Executor License", "ok": False})

    # 11. Worker 単体テスト (Restore Select 実行テスト)
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

    # 12. 最終 Status 確認 (停止中であること)
    print("\n[Test 12] Final Status Check (Should be Stopped)")
    final_status_payload = {"action": "status", "test_mode": True}
    final_res = invoke_lambda(os.getenv('EXECUTOR_LAMBDA_NAME'), final_status_payload)
    if final_res and final_res.get('content') and "停止中" in final_res['content']:
        test_results.append({"name": "Final State (Stopped)", "ok": True})
        print("  ✅ Final state is confirmed as Stopped.")
    else:
        test_results.append({"name": "Final State (Stopped)", "ok": False})

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

    # --- テストレポートの一括送信 ---
    print("\n🚀 Sending test report to log chat...")
    timestamp = datetime.now().strftime('%Y/%m/%d %H:%M:%S')
    summary_lines = [f"{'✅' if r['ok'] else '❌'} {r['name']}" for r in test_results]
    
    total_tests = len(test_results)
    success_count = len([r for r in test_results if r['ok']])
    
    report_payload = {
        "mode": "log",
        "content": (
            f"📋 **Integration Test Report**\n"
            f"Time: `{timestamp}`\n"
            f"Score: `{success_count}/{total_tests}`\n\n"
            + "\n".join(summary_lines)
        )
    }
    invoke_lambda(os.getenv('NOTIFIER_LAMBDA_NAME', 'Factorio_Notifier'), report_payload)

    print("\n=== Test Flow Completed ===")
    print("注意: InteractorはDiscord署名検証が必要なため、Discord画面上からのテストを推奨します。")

    # 失敗が1つでもある場合は、パイプラインを止めるために非ゼロで終了
    if success_count < total_tests:
        print(f"\n❌ Some tests failed ({success_count}/{total_tests}).")
        sys.exit(1)

if __name__ == "__main__":
    # .envに必要な情報があるか確認
    required_env = ['APP_ID', 'AWS_REGION']
    missing = [env for env in required_env if not os.getenv(env)]
    
    if missing:
        print(f"❌ Missing .env variables: {', '.join(missing)}")
    else:
        # 実行確認 (AUTO_CONFIRM が '1' の場合はスキップ)
        if os.getenv('AUTO_CONFIRM') != '1':
            confirm = input(f"Proceed with Integration Test for '{env_file if env_arg else '.env (PROD)'}'? (y/N): ")
            if confirm.lower() != 'y':
                print("🛑 Operation cancelled.")
                sys.exit(1)

            # 本番環境（引数なし）の場合のみ、さらなる確認を求める
            if not env_arg:
                print("\n🚨 ATTENTION: You are about to run tests against the PRODUCTION environment.")
                prod_confirm = input("To proceed, please type 'DEPLOY-PROD': ")
                if prod_confirm != 'DEPLOY-PROD':
                    print("🛑 Production test aborted.")
                    sys.exit(1)

        run_flow_test()