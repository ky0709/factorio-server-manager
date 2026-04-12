import os
import requests
import boto3
from botocore.exceptions import NoCredentialsError, PartialCredentialsError, ClientError
from dotenv import load_dotenv
import sys
import time

# スクリプトの場所を基準にプロジェクトルートを取得
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 引数に基づいて環境ファイルを切り替え (例: python register.py dev)
env_arg = sys.argv[1] if len(sys.argv) > 1 else "prod"
env_file = ".env" if env_arg == "prod" else f".env.{env_arg}"
env_path = os.path.join(BASE_DIR, env_file)

if os.path.exists(env_path):
    print(f"📖 Loading environment: {env_file}")
    load_dotenv(env_path)
else:
    print(f"⚠️  Environment file {env_file} not found, falling back to default .env")
    load_dotenv(os.path.join(BASE_DIR, ".env"))

# 実行確認 (AUTO_CONFIRM が '1' の場合はスキップ)
if os.getenv('AUTO_CONFIRM') != '1':
    confirm = input(f"Proceed with Discord registration and SSM sync for '{env_file if env_arg else '.env (PROD)'}'? (y/N): ")
    if confirm.lower() != 'y':
        print("🛑 Operation cancelled.")
        sys.exit(1)

    # 本番環境（引数なし）の場合のみ、さらなる確認を求める
    if not env_arg:
        print("\n🚨 ATTENTION: You are about to sync secrets to the PRODUCTION SSM Parameter Store.")
        prod_confirm = input("To proceed, please type 'DEPLOY-PROD': ")
        if prod_confirm != 'DEPLOY-PROD':
            print("🛑 Production sync aborted.")
            sys.exit(1)

# 環境変数から値を取得
BOT_TOKEN = os.getenv('DISCORD_TOKEN')
APP_ID = os.getenv('APP_ID')
GUILD_ID = os.getenv('GUILD_ID')
AWS_REGION = os.getenv('AWS_REGION', 'ap-northeast-1')
# 注: 認証情報は AWS_PROFILE 環境変数または標準の検索チェーンを通じて自動的に取得されます

# SSMパスのベースを動的に決定 (例: /D_factorio/)
SSM_BASE = os.getenv('SSM_PARAMETER_PATH', '/factorio/')
print(f"📌 SSM Base Path: {SSM_BASE}")

# 同期対象の機密情報
SECRETS_TO_SYNC = {
    'DISCORD_WEBHOOK_URL': f'{SSM_BASE}DISCORD_WEBHOOK_URL',
    'DISCORD_LOG_WEBHOOK_URL': f'{SSM_BASE}DISCORD_LOG_WEBHOOK_URL',
    'RCON_PASSWORD': f'{SSM_BASE}RCON_PASSWORD',
    'ADMIN_USER_IDS': f'{SSM_BASE}ADMIN_USER_IDS',
    'ADMIN_ROLE_IDS': f'{SSM_BASE}ADMIN_ROLE_IDS',
    'RESTRICTED_COMMAND_STRINGS': f'{SSM_BASE}RESTRICTED_COMMAND_STRINGS',
    'DISCORD_PUBLIC_KEY': f'{SSM_BASE}DISCORD_PUBLIC_KEY',
    'EXECUTOR_LAMBDA_NAME': f'{SSM_BASE}EXECUTOR_LAMBDA_NAME',
    'NOTIFIER_LAMBDA_NAME': f'{SSM_BASE}NOTIFIER_LAMBDA_NAME',
    'INTERACTOR_LAMBDA_NAME': f'{SSM_BASE}INTERACTOR_LAMBDA_NAME',
    'AWS_REGION': f'{SSM_BASE}REGION',
    'INSTANCE_ID': f'{SSM_BASE}INSTANCE_ID',
    'EPHEMERAL_COMMAND_STRINGS': f'{SSM_BASE}EPHEMERAL_COMMAND_STRINGS',
    'DYNAMODB_TABLE_NAME': f'{SSM_BASE}DYNAMODB_TABLE_NAME',
    'S3_BUCKET_NAME': f'{SSM_BASE}S3_BUCKET_NAME',
    'WORKER_LAMBDA_NAME': f'{SSM_BASE}WORKER_LAMBDA_NAME',
    'COMMAND_ROUTING': f'{SSM_BASE}COMMAND_ROUTING',
    'RCON_PORT': f'{SSM_BASE}RCON_PORT',
    'SAVE_FILE_KEY': f'{SSM_BASE}SAVE_FILE_KEY',
    'GAME_PASSWORD': f'{SSM_BASE}GAME_PASSWORD',
    'FACTORIO_GAME_PORT': f'{SSM_BASE}FACTORIO_GAME_PORT',
    'RCON_COMMAND_TIMEOUT_SECONDS': f'{SSM_BASE}RCON_COMMAND_TIMEOUT_SECONDS',
    'RCON_UNRESPONSIVE_THRESHOLD': f'{SSM_BASE}RCON_UNRESPONSIVE_THRESHOLD',
    'ZERO_PLAYER_THRESHOLD': f'{SSM_BASE}ZERO_PLAYER_THRESHOLD',
    'S3_SYNC_WAIT_THRESHOLD_SECONDS': f'{SSM_BASE}S3_SYNC_WAIT_THRESHOLD_SECONDS',
    'RCON_READY_CHECK_INTERVAL_SECONDS': f'{SSM_BASE}RCON_READY_CHECK_INTERVAL_SECONDS',
    'RCON_READY_CHECK_MAX_ATTEMPTS': f'{SSM_BASE}RCON_READY_CHECK_MAX_ATTEMPTS',
}

def register_commands():
    print("--- Registering Discord Commands ---")
    url = f"https://discord.com/api/v10/applications/{APP_ID}/guilds/{GUILD_ID}/commands"

    # .env から制限対象コマンドのリストを取得
    restricted_raw = os.getenv('RESTRICTED_COMMAND_STRINGS', '')
    restricted_set = {s.strip() for s in restricted_raw.split(',') if s.strip()}

    commands = [
    {
        "name": "start",
        "description": "Start the Factorio server",
        "description_localizations": {
            "ja": "Factorioサーバーを起動します"
        }
    },
    {
        "name": "stop",
        "description": "Save and stop the Factorio server",
        "description_localizations": {
            "ja": "セーブを実行し、Factorioサーバーを停止します"
        }
    },
    {
        "name": "status",
        "description": "Check server status, players, and last save time",
        "description_localizations": {
            "ja": "サーバー状態、プレイヤー、最終セーブ日時等を確認します"
        }
    },
    {
        "name": "pass",
        "description": "Display the game password",
        "description_localizations": {
            "ja": "ゲームのパスワードを表示します"
        }
    },
    {
        "name": "license",
        "description": "Show software license information",
        "description_localizations": {
            "ja": "本ソフトウェアのライセンス情報を表示します"
        }
    },
    {
        "name": "save",
        "description": "Save the current game state",
        "description_localizations": {
            "ja": "現在のゲーム状態をセーブします"
        }
    },
    {
        "name": "restore",
        "description": "Restore save data from a previous version",
        "description_localizations": {
            "ja": "セーブデータを過去のバージョンから復元します"
        },
        "options": [
            {
                "name": "list",
                "description": "Display a list of save data history",
                "type": 1,
                "description_localizations": {
                    "ja": "セーブデータの履歴一覧を表示します"
                },
                "options": [
                    {
                        "name": "date",
                        "description": "Date to search (YYYYMMDD). Defaults to today.",
                        "type": 3,
                        "required": False,
                        "description_localizations": {
                            "ja": "検索する日付 (YYYYMMDD)。指定なしで本日分"
                        }
                    },
                    {
                        "name": "count",
                        "description": "Show the latest specified number of items (Max 20, takes precedence over date)",
                        "type": 4,
                        "required": False,
                        "description_localizations": {
                            "ja": "最新の指定件数を表示 (最大20件, 日付指定より優先)"
                        }
                    }
                ]
            },
            {
                "name": "select",
                "description": "Restore save data using a specific version ID",
                "type": 1,
                "description_localizations": {
                    "ja": "指定したバージョンIDのセーブデータを復元します"
                },
                "options": [
                    {
                        "name": "version_id",
                        "description": "The version ID to restore",
                        "type": 3,
                        "required": True,
                        "description_localizations": {
                            "ja": "復元したいバージョンID"
                        }
                    }
                ]
            }
        ]
    }
]

    # 権限制限があるコマンドの説明文に注釈を自動追記
    for cmd in commands:
        # 親コマンドのチェック (例: start, stop)
        if cmd['name'] in restricted_set:
            cmd['description'] = f"[Restricted] {cmd['description']}"
            if 'description_localizations' in cmd and 'ja' in cmd['description_localizations']:
                cmd['description_localizations']['ja'] = f"[管理者限定] {cmd['description_localizations']['ja']}"

        # サブコマンドのチェック (例: restore:select)
        if 'options' in cmd:
            for opt in cmd['options']:
                # type 1 は SUB_COMMAND
                if opt.get('type') == 1:
                    full_path = f"{cmd['name']}:{opt['name']}"
                    if full_path in restricted_set:
                        opt['description'] = f"[Restricted] {opt['description']}"
                        if 'description_localizations' in opt and 'ja' in opt['description_localizations']:
                            opt['description_localizations']['ja'] = f"[管理者限定] {opt['description_localizations']['ja']}"


    headers = {
        "Authorization": f"Bot {BOT_TOKEN}",
        "Content-Type": "application/json"
    }

    for cmd in commands:
        while True:
            response = requests.post(url, headers=headers, json=cmd, timeout=10)
            if response.status_code in [200, 201]:
                print(f"✅ Command '{cmd['name']}': Success!")
                break
            elif response.status_code == 429:
                # レートリミット発生時、Discordからの指示に従って待機
                retry_after = response.json().get('retry_after', 1)
                print(f"⏳ Rate limited. Retrying command '{cmd['name']}' in {retry_after}s...")
                time.sleep(retry_after + 0.1)
                continue
            else:
                print(f"❌ Command '{cmd['name']}': Failed ({response.status_code})")
                print(response.text)
                break
        
        # 次のコマンド登録までに標準的な待機時間を置く
        time.sleep(0.5)

def sync_secrets_to_ssm():
    print("\n--- Syncing Secrets to AWS SSM Parameter Store ---")
    try:
        ssm = boto3.client(
            'ssm',
            region_name=AWS_REGION
        )
        
        lambda_client = boto3.client(
            'lambda',
            region_name=AWS_REGION
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
                Tier='Standard',
                Overwrite=True
            )
            print(f"✅ Successfully synced {env_key}")

        # --- Lambda キャッシュのリフレッシュ ---
        print("\n--- Refreshing Lambda Caches ---")
        target_lambdas = [
            os.getenv('EXECUTOR_LAMBDA_NAME'),
            os.getenv('WORKER_LAMBDA_NAME'),
            os.getenv('NOTIFIER_LAMBDA_NAME'),
            os.getenv('INTERACTOR_LAMBDA_NAME')
        ]
        sync_time = str(int(time.time()))

        for lb in target_lambdas:
            if not lb: continue
            try:
                # 環境変数を一つ更新することで、全インスタンスを強制再起動させる
                lambda_client.update_function_configuration(
                    FunctionName=lb,
                    Environment={'Variables': {
                        **{k: v for k, v in os.environ.items() if k in [
                            'INSTANCE_ID', 
                            'DYNAMODB_TABLE_NAME', 
                            'S3_BUCKET_NAME', 
                            'SAVE_FILE_KEY',
                            'DISCORD_PUBLIC_KEY' # Interactorの高速化のために追加
                        ]}, 
                        'LAST_SSM_SYNC': sync_time,
                        'SSM_PARAMETER_PATH': SSM_BASE
                    }}
                )
                print(f"♻️  Forced refresh for {lb}")
            except Exception as e:
                print(f"⚠️  Could not refresh {lb}: {e}")

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