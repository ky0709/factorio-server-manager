import os
import requests
import boto3
from botocore.exceptions import NoCredentialsError, PartialCredentialsError, ClientError
from dotenv import load_dotenv

# .envファイルを読み込む
load_dotenv()

# 環境変数から値を取得
BOT_TOKEN = os.getenv('DISCORD_TOKEN')
APP_ID = os.getenv('APP_ID')
GUILD_ID = os.getenv('GUILD_ID')
AWS_REGION = os.getenv('AWS_REGION', 'ap-northeast-1')
AWS_ACCESS_KEY_ID = os.getenv('AWS_ACCESS_KEY_ID')
AWS_SECRET_ACCESS_KEY = os.getenv('AWS_SECRET_ACCESS_KEY')
AWS_SESSION_TOKEN = os.getenv('AWS_SESSION_TOKEN')

# 同期対象の機密情報
SECRETS_TO_SYNC = {
    'DISCORD_WEBHOOK_URL': '/factorio/DISCORD_WEBHOOK_URL',
    'RCON_PASSWORD': '/factorio/RCON_PASSWORD'
}

def register_commands():
    print("--- Registering Discord Commands ---")
    url = f"https://discord.com/api/v10/applications/{APP_ID}/guilds/{GUILD_ID}/commands"

    commands = [
    {
        "name": "start",
        "description": "Factorioサーバーを起動します"
    },
    {
        "name": "stop",
        "description": "Factorioサーバーを停止します"
    },
    {
        "name": "status",
        "description": "サーバーの現在の起動状態を確認します"
    }
]

    headers = {
        "Authorization": f"Bot {BOT_TOKEN}",
        "Content-Type": "application/json"
    }

    for cmd in commands:
        response = requests.post(url, headers=headers, json=cmd)
        if response.status_code in [200, 201]:
            print(f"✅ Command '{cmd['name']}': Success!")
        else:
            print(f"❌ Command '{cmd['name']}': Failed ({response.status_code})")
            print(response.text)

def sync_secrets_to_ssm():
    print("\n--- Syncing Secrets to AWS SSM Parameter Store ---")
    try:
        ssm = boto3.client(
            'ssm',
            region_name=AWS_REGION,
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
            aws_session_token=AWS_SESSION_TOKEN
        )
        
        for env_key, ssm_path in SECRETS_TO_SYNC.items():
            value = os.getenv(env_key)
            
            if not value:
                if env_key == 'DISCORD_WEBHOOK_URL':
                    print(f"⚠️  Skip: {env_key} is not defined in .env")
                continue

            print(f"🔄 Syncing {env_key} to {ssm_path}...")
            ssm.put_parameter(
                Name=ssm_path,
                Value=value,
                Type='SecureString',
                Overwrite=True
            )
            print(f"✅ Successfully synced {env_key}")

    except (NoCredentialsError, PartialCredentialsError):
        print("❌ Error: AWS credentials not found. Please run 'aws configure'.")
    except ClientError as e:
        if e.response['Error']['Code'] == 'AccessDeniedException':
            print(f"❌ Error: Access denied. Check your IAM permissions for ssm:PutParameter.")
        else:
            print(f"❌ AWS Error: {e}")
    except Exception as e:
        print(f"❌ Unexpected Error: {e}")

if __name__ == "__main__":
    register_commands()
    sync_secrets_to_ssm()