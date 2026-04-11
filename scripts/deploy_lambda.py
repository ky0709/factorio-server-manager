import os
import boto3
import zipfile
import io
import hashlib
import base64
from dotenv import load_dotenv

def deploy_lambda_functions():
    # スクリプトの場所を基準にプロジェクトルートを取得
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    aws_region = os.getenv('AWS_REGION', 'ap-northeast-1')
    lambda_client = boto3.client('lambda', region_name=aws_region)

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