import os
import sys
from dotenv import load_dotenv

def setup_configs():
    # スクリプトの場所を基準にプロジェクトルートを取得
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # 環境選択 (例: python setup_config.py dev)
    env_arg = sys.argv[1] if len(sys.argv) > 1 else ""
    env_file = f".env.{env_arg}" if env_arg else ".env"
    env_path = os.path.join(BASE_DIR, env_file)
    
    if os.path.exists(env_path):
        print(f"📖 Loading environment: {env_file}")
        load_dotenv(env_path)
    else:
        print(f"⚠️  Environment file {env_file} not found, falling back to default .env")
        load_dotenv(os.path.join(BASE_DIR, ".env"))

    # 環境変数名とプレースホルダーの対応定義
    env_mapping = {
        'AWS_REGION': "<REGION>",
        'AWS_ACCOUNT_ID': "<ACCOUNT_ID>",
        'SSM_PARAMETER_PATH': "<SSM_PARAMETER_PATH>",
        'INSTANCE_ID': "<INSTANCE_ID>",
        'S3_BUCKET_NAME': "<S3_BUCKET_NAME>",
        'SAVE_FILE_KEY': "<SAVE_FILE_KEY>",
        'EXECUTOR_LAMBDA_NAME': "<EXECUTOR_LAMBDA_NAME>",
        'WORKER_LAMBDA_NAME': "<WORKER_LAMBDA_NAME>",
        'NOTIFIER_LAMBDA_NAME': "<NOTIFIER_LAMBDA_NAME>",
        'INTERACTOR_LAMBDA_NAME': "<INTERACTOR_LAMBDA_NAME>",
        'INTERACT_POLICY_NAME': "<INTERACT_POLICY_NAME>",
        'EXECUTE_POLICY_NAME': "<EXECUTE_POLICY_NAME>",
        'NOTIFY_POLICY_NAME': "<NOTIFY_POLICY_NAME>",
        'SERVER_POLICY_NAME': "<SERVER_POLICY_NAME>",
        'REGIST_POLICY_NAME': "<REGIST_POLICY_NAME>",
        'WORK_POLICY_NAME': "<WORK_POLICY_NAME>",
    }

    replacements = {}
    missing_vars = []

    for env_key, placeholder in env_mapping.items():
        # INTERACTOR_LAMBDA_NAME はデフォルト値を持つため、None にならない
        default = 'Factorio_Interactor' if env_key == 'INTERACTOR_LAMBDA_NAME' else None
        val = os.getenv(env_key, default)

        if val is None:
            missing_vars.append(env_key)
        else:
            replacements[placeholder] = val.strip()

    if missing_vars:
        print("❌ Error: The following environment variables are missing in .env:")
        for var in missing_vars:
            print(f"   - {var}")
        print("\nAborting setup. Please define these variables in your .env file.")
        sys.exit(1)

    # テンプレートファイルと出力先の対応
    targets = [
        os.path.join(BASE_DIR, "aws/IAM/FactorioInteractPolicy/policy.json"),
        os.path.join(BASE_DIR, "aws/IAM/FactorioExecutePolicy/policy.json"),
        os.path.join(BASE_DIR, "aws/IAM/FactorioNotifyPolicy/policy.json"),
        os.path.join(BASE_DIR, "aws/IAM/FactorioServerPolicy/policy.json"),
        os.path.join(BASE_DIR, "aws/IAM/FactorioRegistPolicy/policy.json"),
        os.path.join(BASE_DIR, "aws/IAM/FactorioWorkPolicy/policy.json")
    ]

    print("--- Generating IAM Policies from Templates ---")

    for target in targets:
        template_path = target + ".example"
        
        if not os.path.exists(template_path):
            print(f"⚠️  Template not found: {template_path}")
            continue

        with open(template_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # プレースホルダーを実際の値で置換
        for placeholder, value in replacements.items():
            if value:
                content = content.replace(placeholder, value)
        
        # 置換後の内容を policy.json として出力
        with open(target, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"✅ Generated: {target}")

if __name__ == "__main__":
    setup_configs()