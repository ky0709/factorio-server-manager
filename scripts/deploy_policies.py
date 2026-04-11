import os
import boto3
import json
from dotenv import load_dotenv

def deploy_policies():
    # スクリプトの場所を基準にプロジェクトルートを取得
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    account_id = os.getenv('AWS_ACCOUNT_ID', '').strip()
    if not account_id:
        print("❌ Error: AWS_ACCOUNT_ID not found in .env")
        return

    iam_client = boto3.client('iam')

    # ローカルファイルとIAMポリシー名のマッピング
    policy_mapping = [
        {"file": "aws/IAM/FactorioInteractPolicy/policy.json", "name": os.getenv('INTERACT_POLICY_NAME', 'FactorioInteractPolicy')},
        {"file": "aws/IAM/FactorioExecutePolicy/policy.json", "name": os.getenv('EXECUTE_POLICY_NAME', 'FactorioExecutePolicy')},
        {"file": "aws/IAM/FactorioNotifyPolicy/policy.json", "name": os.getenv('NOTIFY_POLICY_NAME', 'FactorioNotifyPolicy')},
        {"file": "aws/IAM/FactorioServerPolicy/policy.json", "name": os.getenv('SERVER_POLICY_NAME', 'FactorioServerPolicy')},
        {"file": "aws/IAM/FactorioRegistPolicy/policy.json", "name": os.getenv('REGIST_POLICY_NAME', 'FactorioRegistPolicy')},
        {"file": "aws/IAM/FactorioWorkPolicy/policy.json", "name": os.getenv('WORK_POLICY_NAME', 'FactorioWorkPolicy')}
    ]

    print("--- Starting IAM Policy Deployment ---")

    for item in policy_mapping:
        policy_name = item["name"].strip()
        policy_arn = f"arn:aws:iam::{account_id}:policy/{policy_name}"
        file_path = os.path.join(BASE_DIR, item["file"])

        if not os.path.exists(file_path):
            print(f"⚠️  Skipping {policy_name}: {file_path} not found. Run setup_config.py first.")
            continue

        print(f"📄 Checking and deploying {policy_name}...")

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                local_policy_dict = json.load(f)
            
            # 1. 現在のデフォルトバージョンの内容を取得
            policy_res = iam_client.get_policy(PolicyArn=policy_arn)
            default_version_id = policy_res['Policy']['DefaultVersionId']
            
            version_res = iam_client.get_policy_version(
                PolicyArn=policy_arn,
                VersionId=default_version_id
            )
            remote_policy_dict = version_res['PolicyVersion']['Document']

            # 2. JSONの中身を比較（キーをソートして比較することで、フォーマットの差異を無視）
            local_json = json.dumps(local_policy_dict, sort_keys=True)
            remote_json = json.dumps(remote_policy_dict, sort_keys=True)

            if local_json == remote_json:
                print(f"✨ Skipping {policy_name}: No changes detected.")
                continue

            # 3. IAMポリシーの5バージョン制限チェック
            versions = iam_client.list_policy_versions(PolicyArn=policy_arn)['Versions']
            if len(versions) >= 5:
                # デフォルト以外の最も古いバージョンを削除
                oldest_version = sorted(
                    [v for v in versions if not v['IsDefaultVersion']], 
                    key=lambda x: x['CreateDate']
                )[0]
                print(f"🗑️  Deleting old version {oldest_version['VersionId']} to make room...")
                iam_client.delete_policy_version(PolicyArn=policy_arn, VersionId=oldest_version['VersionId'])

            # 4. 新しいバージョンを作成してデフォルトに設定
            iam_client.create_policy_version(
                PolicyArn=policy_arn,
                PolicyDocument=local_json,
                SetAsDefault=True
            )
            print(f"✅ Successfully updated {policy_name} to a new version.")

        except iam_client.exceptions.NoSuchEntityException:
            print(f"❌ Policy {policy_name} does not exist at {policy_arn}. Please create it manually first.")
        except Exception as e:
            print(f"❌ Failed to deploy {policy_name}: {e}")

    print("\n--- Deployment process finished ---")

if __name__ == "__main__":
    # スクリプトの場所を基準にプロジェクトルートを取得
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # 認証チェックの前に .env を読み込む
    load_dotenv(os.path.join(BASE_DIR, ".env"))

    if not boto3.Session().get_credentials():
        print("⚠️  Warning: AWS credentials not found. Boto3 might fail to authenticate.")
    deploy_policies()