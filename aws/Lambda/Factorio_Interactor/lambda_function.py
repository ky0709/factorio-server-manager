import json
import os
import boto3
import time
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError

from factorio_common.utils import fetch_config_from_ssm
lambda_client = boto3.client('lambda')
ssm = boto3.client('ssm')

# キャッシュ用変数
admin_config = {
    "user_ids": [],
    "role_ids": [],
    "restricted_commands_set": set(),
    "discord_public_key": None,
    "command_routing": {},
    "executor_lambda": None,
    "worker_lambda": None,
    "notifier_lambda": None,
    "ephemeral_commands_set": set(),
    "initialized": False,
    "last_updated": 0
}

# メッセージリソースの一元管理
MESSAGES = {
    'start': {'ja': "🚀 Factorioサーバーを起動しています...", 'en': "🚀 Starting Factorio server..."},
    'stop': {'ja': "🛑 サーバーを停止しています...", 'en': "🛑 Stopping server..."},
    'status': {'ja': "サーバーの現在の状態を確認しています...", 'en': "Checking current server status..."},
    'pass': {'ja': "🔑 パスワードを確認しています...", 'en': "🔑 Checking password..."},
    'license': {'ja': "📄 ライセンス情報を確認しています...", 'en': "📄 Checking license information..."},
    'save': {'ja': "💾 サーバーのセーブを実行しています...", 'en': "💾 Saving server state..."},
    'restore_list': {'ja': "⏳ セーブデータの一覧を取得しています...", 'en': "⏳ Fetching save list..."},
    'restore_select': {'ja': "⏳ セーブデータの復元処理を開始します...", 'en': "⏳ Starting restoration process..."},
    'maintenance': {'ja': "⚠️ 現在、このコマンドはメンテナンス中です。しばらくしてから再度お試しください。", 'en': "⚠️ This command is currently under maintenance. Please try again later."},
    'restore_default': {'ja': "Processing...", 'en': "Processing..."},
    'unauthorized': {'ja': "❌ このコマンドを実行する権限がありません。", 'en': "❌ You do not have permission to run this command."},
    'checking_auth': {'ja': "権限を確認中...", 'en': "Checking permissions..."},
    'default_received': {'ja': "リクエストを受理しました。", 'en': "Request received."}
}

def get_content(command_name, locale, data=None):
    """言語設定に応じた進捗メッセージを取得"""
    lang = 'ja' if locale == 'ja' else 'en'
    
    if command_name == 'restore' and data:
        options = data.get('data', {}).get('options', [])
        sub_cmd_name = options[0].get('name') if options else None
        if sub_cmd_name == 'list':
            return MESSAGES['restore_list'][lang]
        elif sub_cmd_name == 'select':
            return MESSAGES['restore_select'][lang]
        return MESSAGES['restore_default'][lang]
    
    if command_name in MESSAGES:
        return MESSAGES[command_name][lang]
    
    return MESSAGES['default_received'][lang]

def update_admin_config():
    """SSMから最新の管理者情報を取得してキャッシュを更新"""
    try:
        # レイヤーの共通関数を使用して一括取得 (高速)
        params = fetch_config_from_ssm()
        
        # 必須パラメータの存在確認
        required_keys = ['executor_lambda_name', 'worker_lambda_name', 'notifier_lambda_name']
        missing = [k for k in required_keys if not params.get(k)]
        if missing:
            print(f"⚠️  Critical SSM parameters missing: {missing}")

        # パース処理
        admin_config["user_ids"] = [i.strip() for i in params.get('admin_user_ids', '').split(',') if i.strip()]
        admin_config["role_ids"] = [i.strip() for i in params.get('admin_role_ids', '').split(',') if i.strip()]
        
        restricted_raw = params.get('restricted_command_strings', '')
        admin_config["restricted_commands_set"] = {
            ":".join(p.strip() for p in i.split(":"))
            for i in restricted_raw.split(',') if i.strip()
        }
        
        ephemeral_raw = params.get('ephemeral_command_strings', '')
        admin_config["ephemeral_commands_set"] = {
            ":".join(p.strip() for p in i.split(":"))
            for i in ephemeral_raw.split(',') if i.strip()
        }
        
        # 公開鍵の設定 (環境変数を優先)
        admin_config["discord_public_key"] = os.getenv('DISCORD_PUBLIC_KEY') or params.get('public_key')
        if not admin_config["discord_public_key"]:
            print("❌ Error: DISCORD_PUBLIC_KEY is missing from both ENV and SSM.")
        
        # ルーティングマップのパース
        routing_raw = params.get('command_routing', '{}')
        try:
            admin_config["command_routing"] = json.loads(routing_raw)
        except Exception as e:
            print(f"Error parsing COMMAND_ROUTING: {e}")
            admin_config["command_routing"] = {}

        admin_config["executor_lambda"] = params.get('executor_lambda_name', 'Factorio_Executor')
        admin_config["worker_lambda"] = params.get('worker_lambda', 'Factorio_Worker')
        admin_config["notifier_lambda"] = params.get('notifier_lambda', 'Factorio_Notifier')

        admin_config["initialized"] = True
        print("Admin config updated from SSM")
    except Exception as e:
        print(f"Error fetching admin config from SSM: {e}")

def is_authorized(command_path, user_id, user_roles):
    """
    コマンドの実行権限を確認する。
    親階層または完全なパスが制限リストに含まれているかを確認する。
    """
    # 階層を順に結合しながらチェック (例: "restore" -> "restore:daily" -> "restore:daily:list")
    current_path = ""
    is_restricted = False
    for segment in command_path:
        current_path = f"{current_path}:{segment}" if current_path else segment
        if current_path in admin_config["restricted_commands_set"]:
            is_restricted = True
            break

    if not is_restricted:
        return True

    # 制限が有効かどうか（いずれかのリストにIDが入っているか）
    restriction_enabled = len(admin_config["user_ids"]) > 0 or len(admin_config["role_ids"]) > 0
    if not restriction_enabled:
        return True

    # ユーザーIDまたはロールIDのいずれかが一致するかチェック
    is_admin = (user_id in admin_config["user_ids"]) or \
               any(role_id in admin_config["role_ids"] for role_id in user_roles)

    return is_admin

def lambda_handler(event, context):
    # 1. 署名検証
    headers = event.get('headers', {})
    signature = headers.get('x-signature-ed25519')
    timestamp = headers.get('x-signature-timestamp')
    body = event.get('body', '')

    # 署名検証用の公開鍵を取得（SSM へのアクセスを避けるためキャッシュまたは環境変数から）
    public_key = admin_config["discord_public_key"] or os.getenv('DISCORD_PUBLIC_KEY')
    if not public_key:
        # 初回起動かつ環境変数がない場合のみ SSM を見に行く (フォールバック)
        update_admin_config()
        public_key = admin_config["discord_public_key"]

    if not public_key:
        print("❌ Error: Discord Public Key is missing from SSM.")
        return {'statusCode': 500, 'body': 'Internal configuration error'}
    try:
        verify_key = VerifyKey(bytes.fromhex(public_key))
        verify_key.verify(f'{timestamp}{body}'.encode(), bytes.fromhex(signature))
    except Exception as e:
        print(f"❌ Signature verification failed: {e}")
        return {'statusCode': 401, 'body': 'Invalid request signature'}

    data = json.loads(body)
    
    # 2. PING応答
    if data.get('type') == 1:
        return {'statusCode': 200, 'body': json.dumps({'type': 1})}

    # 2.2 設定のロード (PING以外の全てのインタラクションで必要)
    now = time.time()
    if not admin_config["initialized"] or (now - admin_config["last_updated"]) > 300:
        update_admin_config()
        admin_config["last_updated"] = now

    # 3. スラッシュコマンド（Type 2）
    if data.get('type') == 2:
        locale = data.get('locale', 'en-US')
        command_name = data.get('data', {}).get('name')
        user_id = data.get('member', {}).get('user', {}).get('id') or data.get('user', {}).get('id')
        user_roles = data.get('member', {}).get('roles', [])

        # コマンド階層を再帰的に取得
        command_path = [command_name]
        options = data.get('data', {}).get('options', [])
        # サブコマンドの階層を辿る
        while options and options[0].get('type') in [1, 2]:
            command_path.append(options[0].get('name'))
            options = options[0].get('options', [])

        # 本人限定(Ephemeral)設定の判定
        is_ephemeral = False
        current_path = ""
        for segment in command_path:
            current_path = f"{current_path}:{segment}" if current_path else segment
            if current_path in admin_config["ephemeral_commands_set"]:
                is_ephemeral = True
                break

        data['is_ephemeral'] = is_ephemeral

        # 権限チェックの実行
        if not is_authorized(command_path, user_id, user_roles):
            lang = 'ja' if locale == 'ja' else 'en'
            # 権限エラー時は即座に「本人限定メッセージ」を返して終了
            # 3秒制限を回避するため、外部Lambdaを呼び出さず直接レスポンスを返すのが定石
            return {
                'statusCode': 200,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({'type': 4, 'data': {'content': MESSAGES['unauthorized'][lang], 'flags': 64}})
            }
        
        data['action'] = command_name
        data['locale'] = locale # Pass locale to Executor
        
        # ルーティングマップから呼び出し先を取得（デフォルトは executor_lambda）
        # マップの書き方例: {"restore": "worker_lambda", "save": "executor_lambda"}
        # メンテナンス設定例: {"restore": "maintenance"}
        target_key = admin_config["command_routing"].get(command_name, "executor_lambda")

        # メンテナンスモード判定
        if target_key == "maintenance":
            lang = 'ja' if locale == 'ja' else 'en'
            return {
                'statusCode': 200,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({
                    'type': 4,
                    'data': {
                        'content': MESSAGES['maintenance'][lang],
                        'flags': 64  # 本人限定表示
                    }
                })
            }

        target_lambda = admin_config.get(target_key, admin_config["executor_lambda"])

        # 子Lambdaを非同期で呼び出す（全データを引き継ぐ）
        lambda_client.invoke(
            FunctionName=target_lambda,
            InvocationType='Event',
            Payload=json.dumps(data)
        )
        
        content = get_content(command_name, locale, data)

        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json'
            },
            'body': json.dumps({
                'type': 4,
                'data': {
                    'content': content,
                    'flags': 64 if is_ephemeral else 0
                }
            })
        }

    return {'statusCode': 400}