import json
import os
import boto3
import socket
import struct
from datetime import timezone, timedelta

JST = timezone(timedelta(hours=9))

# 全 Lambda 共通のテキストリソース
GLOBAL_TEXT_RESOURCES = {
    "common": {
        "unknown_cmd": {"ja": "❌ 不明なコマンドです。", "en": "❌ Unknown command."},
        "server_offline": {"ja": "❌ サーバーが起動していないため、この操作は実行できません。", "en": "❌ Server is not running."},
    },
    "status": {
        "running": {
            "ja": "✅ **稼働中**\n### 📍 接続先: `{ip}:{port}`\n- オンライン: `{players}`名 (`{names}`)\n- 最終セーブ: `{save_time}` (`{size}`MB)",
            "en": "✅ **Running**\n### 📍 Address: `{ip}:{port}`\n- Online: `{players}` (`{names}`)\n- Last Save: `{save_time}` (`{size}`MB)"
        },
        "stopped": {
            "ja": "🔴 **停止中**\n- 最終セーブ: `{save_time}` (`{size}`MB)",
            "en": "🔴 **Stopped**\n- Last Save: `{save_time}` (`{size}`MB)"
        },
        "transition": {"ja": "⏳ **状態遷移中** (`{state}`)", "en": "⏳ **Transitioning** (`{state}`)"},
        "starting": {
            "ja": "⏳ **起動処理中** (経過時間: `{elapsed}`秒)\n### 📍 接続先 (準備中): `{ip}:{port}`\n- 最終セーブ: `{save_time}` (`{size}`MB)",
            "en": "⏳ **Starting Process** (Elapsed: `{elapsed}`s)\n### 📍 Address (Preparing): `{ip}:{port}`\n- Last Save: `{save_time}` (`{size}`MB)"
        },
        "stopping": {
            "ja": "⏳ **停止処理中** (経過時間: `{elapsed}`秒)\n- 最終セーブ: `{save_time}` (`{size}`MB)",
            "en": "⏳ **Stopping Process** (Elapsed: `{elapsed}`s)\n- Last Save: `{save_time}` (`{size}`MB)"
        },
        "syncing": {
            "ja": "⏳ (S3同期中...) ",
            "en": "⏳ (S3 Syncing...) "
        }
    },
    "start": {
        "success": {"ja": "🚀 サーバーの起動を開始しました。", "en": "🚀 Starting server..."},
        "already": {"ja": "⚠️ サーバーは既に起動しているか、準備中です。", "en": "⚠️ Server is already running or pending."},
        "completed": {
            "ja": "### 📍 接続先: `{ip}:{port}`\n### 🔑 パスワード: `{pwd}`",
            "en": "### 📍 Address: `{ip}:{port}`\n### 🔑 Password: `{pwd}`"
        },
        "completed_title": {
            "ja": "✨ Factorio サーバー起動完了",
            "en": "✨ Factorio Server Ready"
        }
    },
    "stop": {
        "success": {"ja": "🔌 サーバーの停止を開始しました (セーブ・クリーンアップ実行中)。", "en": "🔌 Stopping server (Saving and cleaning up...)"},
        "already": {"ja": "⚠️ サーバーは既に停止しているか、停止処理中です。", "en": "⚠️ Server is already stopped or stopping."},
        "process_stopped": {"ja": "⏹️ Factorioプロセスを正常に終了しました。", "en": "⏹️ Factorio process stopped successfully."},
        "completed": {"ja": "✅ サーバーの全停止工程が完了しました。", "en": "✅ Server shutdown sequence completed."}
    },
    "save": {
        "success": {"ja": "💾 セーブコマンドを送信しました。S3への反映には数分かかる場合があります。", "en": "💾 Save command sent. S3 sync may take a few minutes."},
    },
    "pass": {
        "not_set": {"ja": "❌ パスワードは設定されていません。", "en": "❌ Password is not set."},
        "display": {"ja": "### 🔑 パスワード: `{pwd}`", "en": "### 🔑 Password: `{pwd}`"},
        "starting": {
            "ja": "⏳ **サーバー起動中です。まもなく利用可能になります。**\n### 🔑 パスワード: `{pwd}`",
            "en": "⏳ **Server is starting. It will be available shortly.**\n### 🔑 Password: `{pwd}`"
        }
    },
    "license": {
        "content": {
            "ja": "### 📜 License Information\n本ソフトウェアは **MIT License** の下で公開されています。\n\n**■ 許諾事項**\nどなたでも無償で本ソフトウェアの使用、複写、変更、結合、掲載、頒布、サブライセンス、および販売を行うことができます。\n\n**■ 利用条件**\nすべての複製または重要な部分に、後述の著作権表示および本許諾表示を記載する必要があります。\n\n**■ 免責事項**\n本ソフトウェアは「現状のまま」提供されます。作者は、ソフトウェアの使用に起因する損害やその他の責任について一切の義務を負いません。\n\n---\n**Copyright (c) 2026 ky0709**\n**GitHub:** https://github.com/ky0709/factorio-server-manager",
            "en": "### 📜 License Information\nThis software is published under the **MIT License**.\n\n**■ Permissions**\nPermission is hereby granted, free of charge, to any person obtaining a copy of this software to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the software.\n\n**■ Conditions**\nThe above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.\n\n**■ Disclaimer**\nTHE SOFTWARE IS PROVIDED \"AS IS\", WITHOUT WARRANTY OF ANY KIND. THE AUTHORS OR COPYRIGHT HOLDERS SHALL NOT BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY.\n\n---\n**Copyright (c) 2026 ky0709**\n**GitHub:** https://github.com/ky0709/factorio-server-manager"
        }
    },
    "restore": {
        "pending_warning": {"ja": "⚠️ **注意: {time} に実行された最新のセーブはまだ S3 に反映されていない可能性があります。**\n", "en": "⚠️ **Note: Latest save at {time} may not be in S3 yet.**\n"},
        "not_found": {"ja": "📁 {date} のセーブデータが見つかりませんでした。", "en": "📁 No save found for {date}."},
        "list_header": {"ja": "📁 **{date} の履歴 {count_info}**\n", "en": "📁 **History for {date} {count_info}**\n"},
        "list_footer": {"ja": "\n\n`/restore select version_id: <ID>` で復元可能です。", "en": "\n\nRestore via `/restore select version_id: <ID>`."},
        "stop_required": {"ja": "❌ 復元前にサーバーを停止してください。", "en": "❌ Stop the server before restoring."},
        "complete": {"ja": "✅ セーブデータの復元が完了しました。\n作成日時: {date}\n対象バージョン: `{id}`", "en": "✅ Restore complete.\nCreated at: {date}\nVersion ID: `{id}`"},
        "failed": {"ja": "❌ 復元失敗: {err}", "en": "❌ Restore failed: {err}"},
        "syncing": {
            "ja": "⏳ (S3同期中...) ",
            "en": "⏳ (S3 Syncing...) "
        },
        "truncated": {"ja": "\n... (履歴が多いため、一部を省略しました)", "en": "\n... (Some items were omitted due to length limits)"}
    }
    ,
    "worker_specific": { # Worker固有のメッセージをここにまとめる (例: auto_shutdown, rcon_unresponsive)
        "auto_shutdown": {"ja": "⌛ 無人状態が一定時間続いたため、サーバーを停止しました。", "en": "⌛ Stopping server as it has been unattended for a certain period of time."},
        "rcon_unresponsive": {"ja": "⚠️ [ALERT] Factorioプロセスが停止またはハングしている可能性があります。自動再起動を試みます...", "en": "⚠️ [ALERT] Factorio process may be down or hanging. Attempting auto-restart..."},
        "startup_failed": {"ja": "🚨 [ALERT] サーバー起動開始から一定時間経過しましたが、Factorioプロセスが開始されませんでした。コスト保護のためサーバーを停止します。", "en": "🚨 [ALERT] Factorio process failed to start within the timeout period. Stopping server for cost protection."},
        "shutdown_failed": {"ja": "🚨 [ALERT] サーバー停止処理の開始から一定時間経過しましたが、EC2インスタンスが停止しません。手動での確認を推奨します。", "en": "🚨 [ALERT] EC2 instance failed to stop within the timeout period after the process was shut down. Manual check is recommended."},
        "restart_limit_reached": {"ja": "🚫 [CRITICAL] 自動再起動を繰り返しましたが復旧しませんでした。無限ループ防止のため、サーバーを停止します。手動での確認が必要です。", "en": "🚫 [CRITICAL] Multiple auto-restart attempts failed. Stopping server to prevent infinite loop. Manual investigation required."}
    }
}



# クライアントのキャッシュ用
_clients = {}

def get_client(service, is_resource=False):
    """必要になるまで初期化を遅らせる (Lazy Initialization)"""
    key = f"{service}_res" if is_resource else service
    if key not in _clients:
        if is_resource:
            _clients[key] = boto3.resource(service)
        else:
            _clients[key] = boto3.client(service)
    return _clients[key]

def fetch_config_from_ssm(path=None):
    """SSMから設定を一括取得して辞書で返す"""
    if path is None:
        # 環境変数から取得。未設定ならデフォルトの /factorio/ を使用
        path = os.getenv('SSM_PARAMETER_PATH', '/factorio/')
    ssm = get_client('ssm')
    config = {}
    try:
        # パス配下のパラメータを取得
        paginator = ssm.get_paginator('get_parameters_by_path')
        for page in paginator.paginate(Path=path, WithDecryption=True):
            for p in page['Parameters']:
                key = p['Name'].split('/')[-1].lower()
                # 値の前後にある空白や引用符を削除して格納
                config[key] = p['Value'].strip("'\" ")
        return config
    except Exception as e:
        print(f"Error fetching SSM parameters: {e}")
        return {}

def run_rcon_command(ip, port, password, command):
    """FactorioサーバーにRCONコマンドを送信する"""
    try:
        with socket.create_connection((ip, int(port)), timeout=5) as sock:
            def send_packet(p_type, p_body):
                p_id = 0x1234
                packet = struct.pack('<ii', p_id, p_type) + p_body.encode('utf-8') + b'\x00\x00'
                sock.sendall(struct.pack('<i', len(packet)) + packet)
                
                header = sock.recv(4)
                if not header: return None
                p_len = struct.unpack('<i', header)[0]
                p_data = sock.recv(p_len)
                return p_data[8:-2].decode('utf-8')

            # ログイン認証 (Type 3)
            send_packet(3, password)
            # コマンド実行 (Type 2)
            return send_packet(2, command)
    except Exception as e:
        return f"Error: RCON Connection Failed ({str(e)})"

def format_msg(resources, category, key, locale='ja', **kwargs):
    """多言語リソースからメッセージを取得してフォーマットする"""
    lang = 'ja' if locale == 'ja' else 'en'
    
    # 与えられたリソースになければ、グローバルリソースから探す
    if category not in resources or key not in resources[category]:
        target_res = GLOBAL_TEXT_RESOURCES
    else:
        target_res = resources

    try:
        text = target_res[category][key][lang]
        return text.format(**kwargs) if kwargs else text
    except (KeyError, TypeError):
        return f"MISSING_TEXT: {category}.{key}"

def notify_via_lambda(notifier_name, content, mode='followup', event=None, embeds=None, components=None):
    """Notifier Lambdaを呼び出して通知を委譲"""
    payload = {'mode': mode, 'content': content}
    if embeds: payload['embeds'] = embeds
    if components: payload['components'] = components
    if event:
        payload.update({
            'application_id': event.get('application_id'), 
            'token': event.get('token'),
            'type': event.get('type') # インタラクションタイプを通知
        })
    get_client('lambda').invoke(FunctionName=notifier_name, InvocationType='Event', Payload=json.dumps(payload))