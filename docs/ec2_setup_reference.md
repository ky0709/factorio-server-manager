# EC2 & S3 Files セットアップリファレンス

本プロジェクトの管理対象となる Factorio サーバー（EC2）の構築に関する参考資料です。

## 1. セキュリティグループの設定
各 Lambda 関数からの通信を許可するために、以下のインバウンドルールを設定してください。
- **TCP 27015**: RCON 通信用（各 Lambda 関数が所属するセキュリティグループ、または VPC CIDR からの許可）
- **UDP 34197**: Factorio ゲームポート（プレイヤーおよび管理用）

## 2. Factorio サーバーの RCON 有効化
サーバー起動時の引数、または `server-settings.json` にて RCON を有効にする必要があります。

**起動引数の例:**
```bash
./bin/x64/factorio --start-server ./saves/my-save.zip --rcon-port 27015 --rcon-password <YOUR_RCON_PASSWORD>
```

## 3. S3 Files (s3files-utils) の導入とマウント
セーブデータの永続化に S3 Files を使用します。

### インストール
EC2 インスタンスに `mountpoint-s3` および `s3files-utils` がインストールされている必要があります。

### ディレクトリの準備
```bash
sudo mkdir -p /mnt/factorio-saves
sudo chown factorio:factorio /mnt/factorio-saves
```

### 恒久的なマウント設定 (/etc/fstab)
`/etc/fstab` に以下の行を追記します。

```plaintext
<S3_FILES_SYSTEM_ID>:/  /mnt/factorio-saves  s3files  _netdev,rw,allow_other  0  0
```

- **`_netdev`**: ネットワークが利用可能になってからマウントを試行し、停止時にはネットワークが切れる前にアンマウントするために必須です。
- **`allow_other`**: `factorio` 実行ユーザーがマウントポイントへアクセスするために必要です。

### 反映
```bash
sudo systemctl daemon-reload
sudo mount -a
```

## 4. IAM ロールの割り当て
EC2 インスタンスには、S3 バケットへのアクセス権限を持つ IAM ロールを割り当ててください。
ポリシーのテンプレートは `aws/IAM/FactorioServerPolicy/policy.json` を参照してください。

---
戻る: README.md