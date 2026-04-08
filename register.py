import os
import requests
from dotenv import load_dotenv

# .envファイルを読み込む
load_dotenv()

# 環境変数から値を取得
BOT_TOKEN = os.getenv('DISCORD_TOKEN')
APP_ID = os.getenv('APP_ID')
GUILD_ID = os.getenv('GUILD_ID')

# コマンド登録用のURL
url = f"https://discord.com/api/v10/applications/{APP_ID}/guilds/{GUILD_ID}/commands"

# 登録するコマンドの定義
commands = [
    {
        "name": "start",
        "description": "Factorioサーバーを起動します"
    },
    {
        "name": "stop",
        "description": "Factorioサーバーを停止します"
    }
]

headers = {
    "Authorization": f"Bot {BOT_TOKEN}",
    "Content-Type": "application/json"
}

# 登録実行
for cmd in commands:
    response = requests.post(url, headers=headers, json=cmd)
    if response.status_code in [200, 201]:
        print(f"✅ Command '{cmd['name']}': Success!")
    else:
        print(f"❌ Command '{cmd['name']}': Failed ({response.status_code})")
        print(response.text)