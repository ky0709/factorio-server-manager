import os
import shutil
import boto3
import zipfile
import io
import hashlib
import base64
from dotenv import load_dotenv
import sys

def wait_for_lambda_ready(client, function_name):
    """Lambda関数が更新完了状態になるまで待機"""
    try:
        waiter = client.get_waiter('function_updated_v2')
        waiter.wait(FunctionName=function_name, WaiterConfig={'Delay': 2, 'MaxAttempts': 30})
    except Exception as e:
        print(f"⚠️  Wait for {function_name} timed out: {e}")

def update_lambda_layer():
    # スクリプトの場所を基準にプロジェクトルートを取得
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # 環境選択
    env_arg = sys.argv[1] if len(sys.argv) > 1 else ""
    env_file = f".env.{env_arg}" if env_arg else ".env"
    env_path = os.path.join(BASE_DIR, env_file)
    
    if os.path.exists(env_path):
        print(f"📖 Loading environment: {env_file}")
        load_dotenv(env_path)
    else:
        load_dotenv(os.path.join(BASE_DIR, ".env"))

    # 実行確認 (AUTO_CONFIRM が '1' の場合はスキップ)
    if os.getenv('AUTO_CONFIRM') != '1':
        confirm = input(f"Proceed with Layer update for '{env_file if env_arg else '.env (PROD)'}'? (y/N): ")
        if confirm.lower() != 'y':
            print("🛑 Operation cancelled.")
            sys.exit(1)

        # 本番環境（引数なし）の場合のみ、さらなる確認を求める
        if not env_arg:
            print("\n🚨 ATTENTION: You are about to update the PRODUCTION Lambda Layer.")
            prod_confirm = input("To proceed, please type 'DEPLOY-PROD': ")
            if prod_confirm != 'DEPLOY-PROD':
                print("🛑 Production update aborted.")
                sys.exit(1)

    print("\n--- Packaging and Uploading Lambda Layer ---")
    layer_dir = os.path.join(BASE_DIR, "aws/Lambda/factorio_common_layer")

    # 1. メモリ内でZIPファイルを作成
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        root_path = os.path.join(layer_dir, 'python')
        for root, _, files in os.walk(root_path):
            for file in files:
                full_path = os.path.join(root, file)
                # ZIP内のパスを "python/..." に調整
                rel_path = os.path.relpath(full_path, layer_dir)
                
                with open(full_path, 'rb') as f:
                    file_data = f.read()
                
                # タイムスタンプを固定してハッシュの同一性を確保
                info = zipfile.ZipInfo(rel_path)
                info.date_time = (2026, 4, 11, 0, 0, 0)
                zf.writestr(info, file_data)
    
    zip_content = zip_buffer.getvalue()
    print("📦 Packaged Layer in memory.")

    # 2. ハッシュ比較による変更検知
    try:
        layer_name = os.getenv('COMMON_LAYER_NAME', 'factorio-common-utils')
        lambda_client = boto3.client('lambda', region_name=os.getenv('AWS_REGION', 'ap-northeast-1'))
        
        local_sha256 = base64.b64encode(hashlib.sha256(zip_content).digest()).decode()
        all_versions = lambda_client.list_layer_versions(LayerName=layer_name).get('LayerVersions', [])
        
        if all_versions:
            latest_v = all_versions[0] # list_layer_versions は最新順に返却される
            if latest_v.get('ContentCodeSha256') == local_sha256:
                print(f"✨ Skipping Layer update: No changes detected (Version {latest_v['Version']} is up to date).")
                return

        # 3. AWS Lambda Layer へアップロード
        response = lambda_client.publish_layer_version(
            LayerName=layer_name,
            Content={'ZipFile': zip_content},
            CompatibleRuntimes=['python3.12']
        )
        print(f"🚀 Successfully published Layer Version: {response['Version']}")
        print(f"📌 New Layer ARN: {response['LayerVersionArn']}")

        # --- 古いレイヤーバージョンのクリーンアップ (最新3つを残す) ---
        print("\n--- Cleaning up old Layer Versions (keeping latest 3) ---")
        all_versions = lambda_client.list_layer_versions(LayerName=layer_name).get('LayerVersions', [])
        # バージョン番号の降順でソート
        sorted_versions = sorted(all_versions, key=lambda x: x['Version'], reverse=True)
        
        if len(sorted_versions) > 3:
            for old_v in sorted_versions[3:]:
                v_num = old_v['Version']
                try:
                    lambda_client.delete_layer_version(LayerName=layer_name, VersionNumber=v_num)
                    print(f"🗑️  Deleted old version: {v_num}")
                except Exception as e:
                    print(f"⚠️  Failed to delete version {v_num}: {e}")

        # --- Lambda 関数のレイヤー設定を更新 ---
        new_layer_arn = response['LayerVersionArn']
        target_lambdas = [
            os.getenv('EXECUTOR_LAMBDA_NAME'),
            os.getenv('WORKER_LAMBDA_NAME'),
            os.getenv('INTERACTOR_LAMBDA_NAME')
        ]

        print("\n--- Updating Lambda Functions to use the new Layer ---")
        for lb in target_lambdas:
            if not lb: continue
            try:
                # 現在の設定を取得
                current_config = lambda_client.get_function_configuration(FunctionName=lb)
                current_layers = [l['Arn'] for l in current_config.get('Layers', [])]
                
                # 他のレイヤー（pynacl等）は維持し、factorio-common-utils だけを差し替える
                # 既存のリストから factorio-common-utils を除外
                new_layers = [a for a in current_layers if f'layer:{layer_name}' not in a]
                new_layers.append(new_layer_arn)

                lambda_client.update_function_configuration(
                    FunctionName=lb,
                    Layers=new_layers
                )
                print(f"✅ Updated {lb} to use layer version {response['Version']}")
                
                # 設定更新の完了を待機（次のデプロイステップとの競合防止）
                wait_for_lambda_ready(lambda_client, lb)
            except Exception as e:
                print(f"⚠️  Could not update {lb}: {e}")

        print("\n✅ Layer update and function attachment complete.")
    except Exception as e:
        print(f"❌ Failed to update Lambda Layer: {e}")

if __name__ == "__main__":
    # スクリプトの場所を基準にプロジェクトルートを取得
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # 認証チェックの前に .env を読み込む
    load_dotenv(os.path.join(BASE_DIR, ".env"))

    if not boto3.Session().get_credentials():
        print("⚠️  Warning: AWS credentials not found. Boto3 might fail to authenticate.")

    update_lambda_layer()