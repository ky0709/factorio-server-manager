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

## 5. 付録: 技術的な解説と選定理由

### ストレージに S3 Files (s3files-utils) を採用している理由

本プロジェクトでは、セーブデータの永続化に EFS や標準の S3 マウントではなく s3files-utils を推奨しています。

- **圧倒的なコスト削減**: EFS と比較してストレージ費用を 1/10 以下に、スループット費用も大幅に抑制。
- **完全なファイルシステム互換性**: 従来の S3 マウント（s3fs-fuse 等）や Mountpoint for Amazon S3 では困難だった「ファイルロック」「POSIX 権限」「共有書き込みアクセス（将来の拡張性）」などをサポートしています。
- **断続的アクセスの最適化**: セーブ時のみ高負荷な書き込みが発生する Factorio の特性に適合しています。
- **既存アプリとの完全互換**: アプリケーション側からは通常のディレクトリとして見えるため、Factorio 本体の設定変更が不要です。

### 💡 安全なシャットダウンのための工夫

管理アプリケーション（Lambda）が安全な停止シーケンスを完走させるために、EC2側では OS レベルの `_netdev` オプション設定が必要です。これにより、ネットワーク切断とマウント解除の順序を適切に制御し、ファイルシステムのハングアップ（OSフリーズ）を防止しています。

---
[戻る](../README.md)
