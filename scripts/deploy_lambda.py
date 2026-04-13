import os
import boto3
import zipfile
import io
import hashlib
import base64
import subprocess
from dotenv import load_dotenv
import sys
from datetime import datetime

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

def cleanup_old_lambda_versions(client, function_name, keep=3):
    """古い関数のバージョンを削除（$LATESTは除外）"""
    try:
        versions = []
        paginator = client.get_paginator('list_versions_by_function')
        for page in paginator.paginate(FunctionName=function_name):
            versions.extend(page['Versions'])

        # 数値バージョンのみ抽出して降順（新しい順）にソート
        numeric_versions = [v['Version'] for v in versions if v['Version'] != '$LATEST']
        numeric_versions.sort(key=lambda x: int(x), reverse=True)

        if len(numeric_versions) > keep:
            for v_num in numeric_versions[keep:]:
                print(f"  🗑️  Deleting old function version: {function_name} v{v_num}")
                client.delete_function(FunctionName=function_name, Qualifier=v_num)
    except Exception as e:
        print(f"  ⚠️  Failed to cleanup old versions for {function_name}: {e}")

def update_lambda_alias(client, function_name, version, alias_name="LIVE"):
    """エイリアスを作成または更新して特定のバージョンを指すようにする"""
    try:
        # エイリアスの存在確認
        client.get_alias(FunctionName=function_name, Name=alias_name)
        # 存在すれば更新
        client.update_alias(FunctionName=function_name, Name=alias_name, FunctionVersion=version)
        print(f"  🚩 Alias '{alias_name}' updated to version {version}")
    except client.exceptions.ResourceNotFoundException:
        # 存在しなければ作成
        client.create_alias(FunctionName=function_name, Name=alias_name, FunctionVersion=version, 
                            Description=f"Points to the latest stable deployment")
        print(f"  🚩 Alias '{alias_name}' created pointing to version {version}")

def show_deployment_summary(lambda_client, lambda_mapping):
    """デプロイ後のリソースサマリーを表示する"""
    print(f"\n{'='*65}")
    print(f"📊 Deployment Summary")
    print(f"{'='*65}")
    print(f"{'Lambda Function':<25} | {'Memory':<8} | {'Timeout':<8} | {'Last Modified':<20} | {'Description'}")
    print(f"{'-'*25}-|-{'-'*8}-|-{'-'*8}-|{'-'*20}-|{'-'*30}")

    for item in lambda_mapping:
        name = os.getenv(item["env"])
        if not name: continue
        try:
            r = lambda_client.get_function_configuration(FunctionName=name)
            mem = f"{r['MemorySize']}MB"
            tm = f"{r['Timeout']}s"
            mod = r['LastModified'].split('.')[0].replace('T', ' ')
            desc = r.get('Description', '-')
            print(f"{name:<25} | {mem:<8} | {tm:<8} | {mod:<20} | {desc}")
        except Exception:
            print(f"{name:<25} | {'N/A':<8} | {'N/A':<8} | {'Not Found':<20} | -")

def deploy_lambda_functions():
    # スクリプトの場所を基準にプロジェクトルートを取得
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    has_error = False

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

        # 本番環境の場合のみ、さらなる確認を求める
        if env_arg == "prod":
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
        if not os.path.exists(func_dir):
            print(f"⚠️  Skipping {function_name}: {func_dir} not found.")
            continue

        print(f"📦 Packaging and deploying {function_name}...")

        try:
            # 前のステップ（Layer更新等）による競合を避けるために待機
            wait_for_lambda_ready(lambda_client, function_name)
            
            # メモリ内でZIPファイルを作成
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
                all_files = []
                exclude_dirs = {'__pycache__', '.pytest_cache'}
                exclude_files = {'.DS_Store', 'archive.zip'}

                for root, dirs, files in os.walk(func_dir):
                    # 不要なディレクトリをスキップ
                    dirs[:] = [d for d in dirs if d not in exclude_dirs]
                    for file in files:
                        if file not in exclude_files and not file.endswith(('.pyc', '.pyo')):
                            all_files.append(os.path.join(root, file))
                
                # ファイルリストをソートして順序を固定
                all_files.sort()

                for full_path in all_files:
                    # ZIP内の相対パスを取得し、Windows環境でもスラッシュに統一
                    rel_path = os.path.relpath(full_path, func_dir).replace('\\', '/')
                    with open(full_path, "rb") as f:
                        file_content = f.read()
                    
                    # タイムスタンプを固定してハッシュの同一性を確保
                    info = zipfile.ZipInfo(rel_path)
                    info.date_time = (2026, 4, 11, 0, 0, 0)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.create_system = 3  # Unix
                    # パーミッションを固定 (Unix 644相当)
                    info.external_attr = 0o644 << 16
                    zf.writestr(info, file_content)
            
            zip_content = zip_buffer.getvalue()
            
            # 現在のコードのハッシュを確認
            local_sha256 = base64.b64encode(hashlib.sha256(zip_content).digest()).decode()
            remote_config = lambda_client.get_function_configuration(FunctionName=function_name)
            
            if remote_config.get('CodeSha256') == local_sha256:
                print(f"✨ Skipping {function_name}: No changes detected.")
                continue

            # 説明文に Git リビジョンを記録
            deploy_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            lambda_client.update_function_configuration(
                FunctionName=function_name,
                Description=f"Deployed from Git: {git_rev} at {deploy_time}"
            )

            # 説明文更新の完了を待機 (コード更新との競合防止)
            wait_for_lambda_ready(lambda_client, function_name)

            # Lambda のコードを更新
            response = lambda_client.update_function_code(
                FunctionName=function_name,
                ZipFile=zip_content,
                Publish=True  # 新しいバージョンを発行
            )
            
            new_version = response.get('Version')
            print(f"✅ Successfully deployed {function_name} (Version: {new_version})")

            # エイリアスを最新バージョンに更新
            update_lambda_alias(lambda_client, function_name, new_version)

            # デプロイ成功後に古いバージョンをクリーンアップ
            cleanup_old_lambda_versions(lambda_client, function_name)

        except Exception as e:
            print(f"❌ Failed to deploy {function_name}: {e}")
            has_error = True

    if not has_error:
        show_deployment_summary(lambda_client, lambda_mapping)

    print("\n--- Deployment process finished ---")
    if has_error:
        print("❌ One or more Lambda deployments failed.")
        sys.exit(1)

if __name__ == "__main__":
    # スクリプトの場所を基準にプロジェクトルートを取得
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # 認証チェックの前に .env を読み込む
    load_dotenv(os.path.join(BASE_DIR, ".env"))

    # 実行前に boto3 の認証情報があるか確認するメッセージ
    if not boto3.Session().get_credentials():
        print("⚠️  Warning: AWS credentials not found. Boto3 might fail to authenticate if not using a specialized config.")
    
    deploy_lambda_functions()