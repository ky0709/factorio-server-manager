import os
import boto3
import zipfile
import io
import hashlib
import base64
import subprocess
from dotenv import load_dotenv
import sys

def get_git_revision():
    """Gitのコミットハッシュを取得"""
    try:
        return subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD']).decode('ascii').strip()
    except:
        return "unknown"

def wait_for_lambda_ready(client, function_name):
    """Lambda関数が更新可能な状態になるまで待機"""
    try:
        waiter = client.get_waiter('function_updated_v2')
        waiter.wait(FunctionName=function_name, WaiterConfig={'Delay': 2, 'MaxAttempts': 30})
    except Exception as e:
        print(f"⚠️  Wait for {function_name} failed (continuing anyway): {e}")

def get_git_branch():
    """現在のGitブランチ名を取得"""
    try:
        return subprocess.check_output(['git', 'rev-parse', '--abbrev-ref', 'HEAD']).decode('ascii').strip()
    except:
        return "unknown"

def deploy_lambda_functions():
    # スクリプトの場所を基準にプロジェクトルートを取得
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # 環境選択
    env_arg = sys.argv[1] if len(sys.argv) > 1 else "prod"
    env_file = ".env" if env_arg == "prod" else f".env.{env_arg}"
    env_path = os.path.join(BASE_DIR, env_file)
    
    if os.path.exists(env_path):
        print(f"📖 Loading environment: {env_file}")
        load_dotenv(env_path)
    else:
        load_dotenv(os.path.join(BASE_DIR, ".env"))

    aws_region = os.getenv('AWS_REGION', 'ap-northeast-1')
    lambda_client = boto3.client('lambda', region_name=aws_region)
    git_rev = get_git_revision()
    git_branch = get_git_branch()

    print(f"🌿 Current Branch: {git_branch}")
    print(f"📌 Target Region: {aws_region}")
    
    # 実行確認 (AUTO_CONFIRM が '1' の場合はスキップ)
    if os.getenv('AUTO_CONFIRM') != '1':
        confirm = input(f"Proceed with deployment to {aws_region}? (y/N): ")
        if confirm.lower() != 'y':
            print("🛑 Deployment cancelled.")
            sys.exit(1)

        # 本番環境（引数なし）の場合のみ、さらなる確認を求める
        if not env_arg:
            print("\n🚨 ATTENTION: You are about to deploy to the PRODUCTION environment.")
            print("This will overwrite the live Lambda functions used in the main environment.")
            prod_confirm = input("To proceed, please type 'DEPLOY-PROD': ")
            if prod_confirm != 'DEPLOY-PROD':
                print("🛑 Production deployment aborted.")
                sys.exit(1)

    # ローカルフォルダ名と環境変数名のマッピング
    lambda_mapping = [
        {"dir": "Factorio_Executor", "env": "EXECUTOR_LAMBDA_NAME"},
        {"dir": "Factorio_Worker", "env": "WORKER_LAMBDA_NAME"},
        {"dir": "Factorio_Notifier", "env": "NOTIFIER_LAMBDA_NAME"},
        {"dir": "Factorio_Interactor", "env": "INTERACTOR_LAMBDA_NAME"}
    ]

    print("--- Starting Lambda Deployment ---")

    for item in lambda_mapping:
        function_name = os.getenv(item["env"])
        if not function_name:
            print(f"⚠️  Skipping {item['dir']}: Environment variable {item['env']} not set.")
            continue

        # lambda_function.py のパス
        func_dir = os.path.join(BASE_DIR, "aws", "Lambda", item["dir"])
        file_path = os.path.join(func_dir, "lambda_function.py")

        if not os.path.exists(file_path):
            print(f"⚠️  Skipping {function_name}: {file_path} not found.")
            continue

        print(f"📦 Packaging and deploying {function_name}...")

        try:
            # 前のステップ（Layer更新等）による競合を避けるために待機
            wait_for_lambda_ready(lambda_client, function_name)

            with open(file_path, "rb") as f:
                file_content = f.read()

            # メモリ内でZIPファイルを作成
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
                # タイムスタンプを固定してハッシュの同一性を確保
                info = zipfile.ZipInfo("lambda_function.py")
                info.date_time = (2026, 4, 11, 0, 0, 0)
                zf.writestr(info, file_content)
            
            zip_content = zip_buffer.getvalue()
            
            # 現在のコードのハッシュを確認
            local_sha256 = base64.b64encode(hashlib.sha256(zip_content).digest()).decode()
            remote_config = lambda_client.get_function_configuration(FunctionName=function_name)
            
            if remote_config.get('CodeSha256') == local_sha256:
                print(f"✨ Skipping {function_name}: No changes detected.")
                continue

            # 説明文に Git リビジョンを記録
            lambda_client.update_function_configuration(
                FunctionName=function_name,
                Description=f"Deployed from Git: {git_rev} at 2026-04-11"
            )

            # 説明文更新の完了を待機 (コード更新との競合防止)
            wait_for_lambda_ready(lambda_client, function_name)

            # Lambda のコードを更新
            response = lambda_client.update_function_code(
                FunctionName=function_name,
                ZipFile=zip_content,
                Publish=True  # 新しいバージョンを発行
            )

            print(f"✅ Successfully deployed {function_name} (Version: {response.get('Version')})")

        except Exception as e:
            print(f"❌ Failed to deploy {function_name}: {e}")

    print("\n--- Deployment process finished ---")

if __name__ == "__main__":
    # スクリプトの場所を基準にプロジェクトルートを取得
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # 認証チェックの前に .env を読み込む
    load_dotenv(os.path.join(BASE_DIR, ".env"))

    # 実行前に boto3 の認証情報があるか確認するメッセージ
    if not boto3.Session().get_credentials():
        print("⚠️  Warning: AWS credentials not found. Boto3 might fail to authenticate if not using a specialized config.")
    
    deploy_lambda_functions()