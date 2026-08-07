# EC2 & S3 Files セットアップリファレンス

本プロジェクトの管理対象となる Factorio サーバー（EC2）の構築に関する参考資料です。
AWS アカウントをお持ちの方が、何もない状態から1から環境構築できるよう、ネットワーク作成から記載しています。

> 記載されている `<...>` はプレースホルダーです。`<>` 内はご自身の運用ルールに合わせた任意の名前・値へ変更してください。

## 1. ネットワーク環境の構築（任意）

既存の VPC を使用する場合は、この手順をスキップできます。
ここでは、Factorio 用の独立した VPC ネットワーク（VPC, サブネット, IGW, ルートテーブル）を AWS CLI で作成する例を示します。

```bash
# VPC の作成
aws ec2 create-vpc --cidr-block 10.1.0.0/16 --tag-specifications 'ResourceType=vpc,Tags=[{Key=Name,Value=<VPC_NAME>}]'

# サブネットの作成 (VPC_IDは上記で作成したVPCのIDを指定してください)
aws ec2 create-subnet --vpc-id <VPC_ID> --cidr-block 10.1.1.0/24 --availability-zone ap-northeast-1a --tag-specifications 'ResourceType=subnet,Tags=[{Key=Name,Value=<SUBNET_NAME>}]'

# パブリック IP の自動割り当てを有効化 (作成したサブネットに割り当てます)
aws ec2 modify-subnet-attribute --subnet-id <SUBNET_ID> --map-public-ip-on-launch

# インターネットゲートウェイの作成とアタッチ
aws ec2 create-internet-gateway --tag-specifications 'ResourceType=internet-gateway,Tags=[{Key=Name,Value=<IGW_NAME>}]'
aws ec2 attach-internet-gateway --vpc-id <VPC_ID> --internet-gateway-id <IGW_ID>

# ルートテーブルの作成と設定
aws ec2 create-route-table --vpc-id <VPC_ID> --tag-specifications 'ResourceType=route-table,Tags=[{Key=Name,Value=<RTB_NAME>}]'
aws ec2 create-route --route-table-id <RTB_ID> --destination-cidr-block 0.0.0.0/0 --gateway-id <IGW_ID>
aws ec2 associate-route-table --subnet-id <SUBNET_ID> --route-table-id <RTB_ID>
```

## 2. セキュリティグループの作成と設定

各 Lambda 関数からの通信や、ゲームプレイのための通信を許可するために、以下のインバウンドルールを設定します。

- **TCP `<RCON_PORT>`**: RCON 通信用（各 Lambda 関数が所属するセキュリティグループ、または VPC CIDR からの許可）
- **UDP 34197**: Factorio ゲームポート（プレイヤーおよび管理用）
- **TCP 22**: SSH 接続用（自身のIPアドレス等に制限することを強く推奨）

**AWS CLI での設定例（開発環境）:**

```bash
# セキュリティグループの作成
aws ec2 create-security-group --group-name <SG_NAME> --description "Security group for Factorio server" --vpc-id <VPC_ID>

# Factorio ゲームポート (UDP 34197) の開放
aws ec2 authorize-security-group-ingress --group-id <SG_ID> --protocol udp --port 34197 --cidr 0.0.0.0/0

# RCON ポート (TCP <RCON_PORT>) の開放
# ※ 運用方針に応じて `--cidr <RCON_CIDR>`（例: VPC CIDR）へ置き換えてください。
aws ec2 authorize-security-group-ingress --group-id <SG_ID> --protocol tcp --port <RCON_PORT> --cidr 0.0.0.0/0

# SSH ポート (TCP 22) の開放 (※自身のIPアドレスに制限する例)
aws ec2 authorize-security-group-ingress --group-id <SG_ID> --protocol tcp --port 22 --cidr <YOUR_IP>/32
```

## 3. EC2 インスタンスの作成

ここからは「既存の VPC / Subnet / SG / KeyPair を利用する前提」で進めます。

### 3-1. Ubuntu AMI ID を取得

```bash
# Ubuntu 22.04 LTS (x86_64) の最新AMIを取得
aws ssm get-parameter \
  --name /aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp3/ami-id \
  --query "Parameter.Value" \
  --output text
```

`ParameterNotFound` が出る場合のフォールバック:

```bash
# 利用可能なパラメータ名を確認
aws ssm get-parameters-by-path \
  --path /aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ \
  --recursive \
  --query "Parameters[].Name" \
  --output text

# 例: gp2 が存在する場合はこちらを利用
aws ssm get-parameter \
  --name /aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id \
  --query "Parameter.Value" \
  --output text
```

### 3-2. EC2 起動

```bash
# 例: t3.medium で起動
aws ec2 run-instances \
  --image-id <AMI_ID> \
  --instance-type t3.medium \
  --count 1 \
  --key-name <KEY_NAME> \
  --security-group-ids <SG_ID> \
  --subnet-id <SUBNET_ID> \
  --associate-public-ip-address \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=<INSTANCE_NAME>}]" \
  --query "Instances[0].{InstanceId:InstanceId,State:State.Name,PublicIp:PublicIpAddress,PrivateIp:PrivateIpAddress}" \
  --output table
```

### 3-3. 起動待機と接続先確認

```bash
aws ec2 wait instance-status-ok --instance-ids <INSTANCE_ID>

aws ec2 describe-instances \
  --instance-ids <INSTANCE_ID> \
  --query "Reservations[0].Instances[0].{PublicIp:PublicIpAddress,PrivateIp:PrivateIpAddress,State:State.Name}" \
  --output table
```

## 4. EC2 へ SSH 接続と初期セットアップ

### 4-1. SSH 接続（PowerShell 例）

```bash
ssh -i .\<KEY_FILE_NAME>.pem ubuntu@<PUBLIC_IP>
```

※ Windows OpenSSH で鍵権限エラーが出る場合は、鍵ファイルの ACL から他ユーザー権限を外してください。

### 4-2. 初期セットアップ

```bash
sudo apt update
sudo apt -y upgrade
sudo apt -y install curl wget tar jq unzip awscli

# Factorio 実行ユーザー作成
sudo useradd -m -s /bin/bash factorio || true
sudo mkdir -p /opt/factorio
sudo chown -R factorio:factorio /opt/factorio
```

`awscli` は `/stop` 時の `logs` 退避（`aws s3 cp`）で利用します。未導入だと同期ステップが失敗し、停止オーケストレーションが中断されます。

## 5. Factorio サーバーのインストールと RCON 有効化

### 5-1. Factorio ダウンロードと展開

```bash
sudo -u factorio bash <<'EOF'
cd /opt/factorio
wget -O factorio_headless.tar.xz https://factorio.com/get-download/stable/headless/linux64
tar -xJf factorio_headless.tar.xz --strip-components=1
mkdir -p saves config mods logs
EOF
```

バージョンを固定したい場合は URL の `stable` を具体バージョンに置き換えます（例: `https://factorio.com/get-download/2.0.77/headless/linux64`）。
クライアントとサーバーのバージョン不一致時は、稼働中インスタンス向けに Executor の `integration_upgrade_factorio`（test_mode）で headless を更新できます。`.env` の `FACTORIO_VERSION`（例: `2.0.77`）を設定して `register.py` すると、以降の `/start` で差分更新されます。

### 5-2. server-settings.json 作成

```bash
sudo -u factorio /opt/factorio/bin/x64/factorio --create /opt/factorio/saves/init.zip
sudo -u factorio tee /opt/factorio/config/server-settings.json > /dev/null <<'EOF'
{
  "name": "Factorio Server",
  "description": "Managed by factorio-server-manager",
  "tags": ["prod"],
  "max_players": 10,
  "visibility": { "public": false, "lan": false },
  "username": "",
  "password": "",
  "token": "",
  "game_password": "",
  "require_user_verification": true,
  "max_upload_in_kilobytes_per_second": 0,
  "max_upload_slots": 5,
  "minimum_latency_in_ticks": 0,
  "ignore_player_limit_for_returning_players": false,
  "allow_commands": "admins-only",
  "autosave_interval": 10,
  "autosave_slots": 5,
  "afk_autokick_interval": 30,
  "auto_pause": true,
  "only_admins_can_pause_the_game": true
}
EOF
```

### 5-3. RCON 有効化

```bash
echo 'RCON_PORT=<RCON_PORT>' | sudo tee /etc/factorio.env
echo 'RCON_PASSWORD=<STRONG_PASSWORD>' | sudo tee -a /etc/factorio.env
sudo chmod 600 /etc/factorio.env
```

### 5-4. systemd サービス作成

```bash
sudo tee /etc/systemd/system/factorio.service > /dev/null <<'EOF'
[Unit]
Description=Factorio Headless Server
After=network-online.target
Wants=network-online.target
RequiresMountsFor=/mnt/factorio-data

[Service]
User=factorio
Group=factorio
EnvironmentFile=/etc/factorio.env
# Headless はログをカレントディレクトリに factorio-current.log / factorio-previous.log として出力するため、作業ディレクトリを logs に固定する
WorkingDirectory=/opt/factorio/logs
ExecStart=/opt/factorio/bin/x64/factorio \
  --server-settings /mnt/factorio-data/config/server-settings.json \
  --mod-directory /mnt/factorio-data/mods \
  --start-server /mnt/factorio-data/saves/save.zip \
  --rcon-port ${RCON_PORT} \
  --rcon-password ${RCON_PASSWORD}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now factorio
sudo systemctl status factorio --no-pager
```

補足（本番/開発を分ける場合）:

- `server-settings-prod.json` / `server-settings-dev.json` と、`/etc/factorio.prod.env` / `/etc/factorio.dev.env` を作成します。
- `factorio-prod.service` / `factorio-dev.service` を分離して、用途に応じて片方だけ起動します（同時起動は非推奨）。AMI を起動オプションで切り替える運用では、**どちらのユニットも** `WorkingDirectory=/opt/factorio/logs` を揃え、`/opt/factorio/logs` を事前に作成しておくとログの場所が環境間で一致します。
- **重要**: save の実体はローカルの `/opt/factorio/saves/*.zip` ではなく、**S3 Files 側の `/mnt/factorio-data/saves/save.zip`** を `--start-server` に指定してください。`SAVE_FILE_KEY` も `.env` / `.env.dev` で **`saves/save.zip`** に揃えます。（`factorio-prod.service` の `--start-server` がローカル `init.zip` のままの場合は、本番運用に合わせて上記パスへ揃えること。）
- **重要**: `config` と `mods` も S3 Files 側を正とし、`--server-settings /mnt/factorio-data/config/server-settings.json` と `--mod-directory /mnt/factorio-data/mods` を指定してください。これによりインスタンス再作成後も同一データを継続利用できます。
- `RequiresMountsFor=/mnt/factorio-data` を設定して、S3 Files がマウントされる前に `factorio.service` が起動しないようにしてください（`/etc/fstab` の設定とセットで適用）。
- **注意（独自構築サーバー）**: Executor 側の起動時チェックと停止時アンマウントは、現時点では `/mnt/factorio-data` を前提にしています。`ec2_setup_reference.md` を使わず独自構成で構築した場合は、マウント先・`ExecStart`・`/etc/fstab` を本手順の前提に合わせてください。
- 開発/本番で S3 バケットを分ける設計であれば、サービス側の save パスは同じ `saves/save.zip` でも問題ありません。向き先バケットは、EC2 が `/etc/fstab` でどの `S3_FILES_SYSTEM_ID` をマウントしているかで切り替わります。
- 推奨運用（2026-04 合意）: `saves` / `mods` / `config` は S3 Files 側を正とし、`logs` は **ローカル `/opt/factorio/logs` に出力して停止時に S3 (`logs/`) へ同期**します。`logs` を常時 S3 Files 直書きにしないことで、実行中のログ追記をローカル I/O で安定化しつつ、停止後の集計（Glue/Athena）用データは S3 に集約できます。
- `SERVICE_UNIT_NAME` は `.env` / SSM で停止対象ユニットを指定するため、独自構築サーバー（他プロセス同居）では誤設定リスクがあります。適用前に `systemctl status <SERVICE_UNIT_NAME>` で対象ユニットを確認してください（本資料の標準構成である Factorio 専用EC2 では通常リスクは低い）。

### 5-5. RCON ポート疎通確認

```bash
# EC2内で LISTEN を確認
sudo ss -ltnup | grep 27015

# AWS側 SG で 27015/tcp が許可されているか再確認
aws ec2 describe-security-groups --group-ids <SG_ID> \
  --query "SecurityGroups[0].IpPermissions[?FromPort==`27015` && ToPort==`27015`]" \
  --output json
```

## 6. S3 Files の導入とマウント

Linux 上では **`/etc/fstab` の `s3files` 型**はカーネル標準ではなく、**amazon-efs-utils（v3.0.0 以上）** が提供する `mount.s3files` が必要です。加えて、VPC 内に **マウントターゲット** を作成しないと、マウント時の DNS 名（`*.s3files.<region>.on.aws`）が解決できません。以下の順序を推奨します。

1. ファイルシステム ID（`fs-...`）の確定と `.env` 反映  
2. EC2 に **amazon-efs-utils** をインストール  
3. **マウントターゲット** の作成と **セキュリティグループ（TCP 2049）**  
4. マウントポイント作成と **`/etc/fstab` 追記** → `mount -a`

運用補足:

- **factorio-server-manager は、セーブ保存先の S3 バケットでバージョニングが有効であることを前提**にしています。`/restore list` や `/restore select`、保存履歴の追跡は、`saves/save.zip` のオブジェクトバージョンを参照して動作します。
- セーブ保存先の S3 バケットは履歴を継続的に蓄積していく前提のため、**S3 Files の導入タイミングで S3 ライフサイクルルールも合わせて検討することを推奨**します。
- 特に `saves/save.zip` の **オブジェクトバージョン** を保持する設計では、非現行バージョンの保持期間、旧バージョンの削除日数、必要なら Glacier 系ストレージクラスへの移行有無を先に決めておくと、容量増加とコストを管理しやすくなります。
- ただし、`/restore list` や手動復元で参照したい世代を削除しないよう、**運用要件を決めてから** ルールを適用してください。

### 6-1. S3 Files File System ID の準備

`S3_FILES_SYSTEM_ID` が未設定の場合は、ローカルで `init_aws_resources.py` を実行して作成を試行できます。

```bash
python scripts/init_aws_resources.py <env>
```

処理内容の概要:

- 上記は **`FactorioRegistUser(-dev)` にアタッチした `FactorioRegistPolicy(-dev)`** の権限だけで実行してください（初期化・登録系の権限はこのポリシーに集約する）。
- `S3_FILES_SERVICE_ROLE_NAME`（既定: `FactorioS3FilesServiceRole`、dev では `-dev` サフィックス付与）という **S3 Files 用 IAM ロール** を作成または更新し、指定バケットへのアクセスインラインポリシーを付与します。
- AWS CLI の `aws s3files create-file-system` に **`--bucket` は S3 バケット ARN**、`--role-arn` は上記ロールの ARN を渡してファイルシステムを作成します。

作成に成功すると `fs-...` 形式の ID が表示されます。対象の `.env` / `.env.dev` に反映してください。

```plaintext
S3_FILES_SYSTEM_ID='fs-xxxxxxxxxxxxxxxxx'
```

> **初回のみ**: `FactorioRegistPolicy` に S3 Files 作成用の権限が含まれる必要があります。テンプレート更新後は `python scripts/setup_config.py <env>` と `python scripts/deploy_policies.py <env>` を実行してから `init_aws_resources.py` を再実行してください。

> 失敗した場合（`s3files` CLI 未導入や権限不足など）は、S3 Files を手動作成して同様に `S3_FILES_SYSTEM_ID` を設定してください。

作成済みか一覧で確認する例（ローカルまたは操作端末）:

```bash
aws s3files list-file-systems --region <REGION> --profile <PROFILE> --output table
```

### 6-2. EC2 に amazon-efs-utils をインストールする

**何をするか**: `mount -t s3files` を可能にするため、パッケージを入れて `mount.s3files` を配置します。

```bash
curl -fsSL https://amazon-efs-utils.aws.com/efs-utils-installer.sh | sudo sh -s -- --install
```

Ubuntu 等で上記が使えない場合は、[aws/efs-utils の INSTALL.md](https://github.com/aws/efs-utils/blob/master/INSTALL.md) の **DEB-based** 手順（`./build-deb.sh`）に従ってビルド・インストールします。

確認:

```bash
ls -la /sbin/mount.s3files
dpkg -l | grep amazon-efs-utils   # Debian/Ubuntu の場合
```

`unknown filesystem type 's3files'` が出る場合は、本節が未実施です。

### 6-3. VPC にマウントターゲットを作成する

**何をするか**: S3 Files は EFS と同様に **VPC 内のマウントターゲット** 経由で接続します。未作成のままマウントすると、`Failed to resolve "...s3files....on.aws"` のような **DNS 解決エラー**になります。

1. マウント元の EC2 の **サブネット ID** を取得する（マウントターゲットは通常 **同じサブネット**、または同じ AZ 内のサブネットに作成）:

```bash
aws ec2 describe-instances \
  --instance-ids <INSTANCE_ID> \
  --region <REGION> \
  --profile <PROFILE> \
  --query "Reservations[0].Instances[0].SubnetId" \
  --output text
```

2. マウントターゲット用の **セキュリティグループ** を用意し、後述の **6-4** に従い **TCP 2049** を許可する（新規作成でも既存でも可）。

3. マウントターゲットを作成する（`FactorioRegistPolicy` 相当の権限が必要です）:

```bash
aws s3files create-mount-target \
  --file-system-id <S3_FILES_SYSTEM_ID> \
  --subnet-id <SUBNET_ID> \
  --security-groups <MOUNT_TARGET_SG_ID> \
  --region <REGION> \
  --profile <PROFILE> \
  --output table
```

4. `status` が **`available`** になるまで待ってからマウントする:

```bash
aws s3files list-mount-targets \
  --file-system-id <S3_FILES_SYSTEM_ID> \
  --region <REGION> \
  --profile <PROFILE> \
  --output table
```

### 6-4. セキュリティグループ（TCP 2049）

**何をするか**: マウントターゲットの ENI と EC2 の間で **NFS（TCP 2049）** が通るようにします。

| 方向 | 設定の目安 |
|------|------------|
| マウントターゲット用 SG のインバウンド | タイプ **NFS** または **TCP 2049**、ソースは **EC2 インスタンスのセキュリティグループ**（推奨） |
| EC2 側 SG のアウトバウンド | デフォルトの全許可で問題ないことが多い。制限している場合は **TCP 2049** をマウントターゲット SG 宛てに許可 |

詳細は [S3 Files の前提条件（セキュリティグループ）](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files-prereq-policies.html#s3-files-prereq-security-groups) を参照してください。

### 6-5. VPC の DNS 設定（推奨）

**何をするか**: プライベート DNS 名の解決に失敗する場合、VPC の **DNS 解決** と **DNS ホスト名** が有効か確認します。

```bash
aws ec2 describe-vpc-attribute --vpc-id <VPC_ID> --attribute enableDnsSupport --region <REGION> --profile <PROFILE>
aws ec2 describe-vpc-attribute --vpc-id <VPC_ID> --attribute enableDnsHostnames --region <REGION> --profile <PROFILE>
```

### 6-6. マウントポイントと `/etc/fstab`

上記まで完了したうえで、EC2 上でディレクトリを用意し、`fstab` に追記します。

```bash
sudo mkdir -p /mnt/factorio-data
sudo chown factorio:factorio /mnt/factorio-data
sudo chmod 775 /mnt/factorio-data
```

`factorio` ユーザーで `saves/` などのサブディレクトリを作れることも確認してください。

```bash
sudo -u factorio mkdir -p /mnt/factorio-data/saves
```

`/etc/fstab` 追記例（`S3_FILES_SYSTEM_ID` は `.env` の `fs-...` と一致させる）:

```plaintext
<S3_FILES_SYSTEM_ID>:/  /mnt/factorio-data  s3files  _netdev,rw  0  0
```

本番では `_netdev,nofail` の併用も検討してください（[自動マウントの注意](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files-mounting.html)）。

反映と確認:

```bash
sudo systemctl daemon-reload
sudo mount -a
mount | grep factorio-data
findmnt -T /mnt/factorio-data
```

初回投入の例（既存のローカル save を S3 Files 側へ移す場合）:

```bash
sudo -u factorio cp /opt/factorio/saves/init.zip /mnt/factorio-data/saves/save.zip
ls -l --full-time /mnt/factorio-data/saves/save.zip
```

`/save` 実行後は、EC2 側の更新時刻と S3 上の version が増えていることを確認してください。

```bash
ls -l --full-time /mnt/factorio-data/saves/save.zip
```

```bash
aws s3api list-object-versions --bucket <S3_BUCKET_NAME> --prefix "saves/save.zip" --region <REGION> --profile <PROFILE> --output table
```

### 6-6-1. `mods` / `config` / `logs` をマウント先へ初回同期する場合（`rsync` が失敗するとき）

**よくある症状（`sudo -u factorio rsync -a ...` 時）**

| メッセージ | 想定原因 |
|------------|----------|
| `chgrp ".../mods/." failed: Operation not permitted` | `rsync -a` はグループ（および所有者）の再現を試みる。S3 Files の NFS では **`chgrp` が許可されない**ことがある。 |
| `mkstemp ".../mods/.mod-list.json.xxx" failed: Permission denied` | rsync は既定で**転送先ディレクトリ内**に一時ファイルを作る。S3 Files マウント上では、**隠しファイル名の作成**や **NFS の制約**で失敗することがある。 |
| `failed to set times on ".../mods/.": Operation not permitted` | NFS 上でディレクトリの **mtime/utime の更新**が拒否されることがある。`rsync -a` は時刻の再現を試みる。 |
| `open ".../mod-list.json": Permission denied` / `copy ".../tmp/..." -> "mod-list.json": Permission denied` | 転送先ディレクトリが **`factorio` に書き込み可能でない**（`root` 所有のまま等）。またはバケット側の **空オブジェクト `mods/`** がマウント上で **通常ファイル**として見え、ディレクトリとして使えていない。 |

上記は「バケットにプレフィックスが無い」問題ではなく、**クライアント側のコピー方法と NFS の POSIX 差分**が原因であることが多い。

**推奨（初回のローカル → マウント先コピー）**

- グループ/所有者の再現をやめる: `--no-group --no-owner`
- 一時ファイルをローカル（例: `/tmp`）に逃がす: `--temp-dir=/tmp`（短形: `-T /tmp`）
- ディレクトリの時刻を触らない: `--omit-dir-times`（短形: `-O`）。**ファイル**でも `failed to set times` が出る場合は `-a` をやめ、タイムスタンプ無しで再試行する（例: `-rlDv` に上記オプションを組み合わせる）。

```bash
sudo -u factorio mkdir -p /mnt/factorio-data/mods /mnt/factorio-data/config

# 書き込み不可のときは root で所有者を揃えてから rsync（NFS では chgrp は失敗しがちだが chown は通ることが多い）
sudo chown -R factorio:factorio /mnt/factorio-data/mods /mnt/factorio-data/config

sudo -u factorio rsync -av --no-group --no-owner --omit-dir-times --temp-dir=/tmp \
  /opt/factorio/mods/ /mnt/factorio-data/mods/

sudo -u factorio rsync -av --no-group --no-owner --omit-dir-times --temp-dir=/tmp \
  /opt/factorio/config/ /mnt/factorio-data/config/
```

**`Permission denied` が続く場合の確認**

```bash
ls -la /mnt/factorio-data/
ls -la /mnt/factorio-data/mods/
```

- `mods` の行が **ディレクトリ**（先頭が `d`）で、`factorio` が書き込める権限になっているか確認する。
- 先頭が **`-`（通常ファイル）** のように見える場合、バケットに **プレフィックス用の空オブジェクト `mods/`** だけが存在し、マウント上で「フォルダ」として扱えていない可能性がある。その場合は S3 側でキー `mods/`（0 バイトのオブジェクト）を **削除**し、EC2 で `sudo -u factorio mkdir -p /mnt/factorio-data/mods` をやり直してから rsync を再実行する。

`logs` は空でよい運用なら `mkdir` のみで足りることが多く、コピーは不要です。

**代替（rsync を使わない）**: メタデータを極力いじらない単純コピーなら `cp -r` でもよい場合があります（大量ファイル時は `rsync` の方が再実行に強い）。

```bash
sudo -u factorio cp -r /opt/factorio/mods/. /mnt/factorio-data/mods/
sudo -u factorio cp -r /opt/factorio/config/. /mnt/factorio-data/config/
```

その後、`mod-list.json` などがゲームユーザーで読めるか `ls -la` で確認してください。シンボリックリンクを含む場合は `rsync -av --no-group --no-owner --omit-dir-times --temp-dir=/tmp -L`（リンク先を辿る）など、内容に応じて調整します。

`.env` / `.env.dev` の `SAVE_FILE_KEY` を変更した場合は、Lambda が参照する SSM へ再同期が必要です。

```bash
python scripts/register.py <env>
```

手動で試す場合:

```bash
sudo mount -t s3files -v <S3_FILES_SYSTEM_ID>:/ /mnt/factorio-data
```

### 6-7. トラブルシューティング（よくあるエラー）

| 症状 | 想定原因 | 対処のヒント |
|------|----------|--------------|
| `unknown filesystem type 's3files'` | **amazon-efs-utils 未インストール** | **6-2** を実施し `mount.s3files` を確認 |
| `Failed to resolve "...s3files....on.aws"` | **マウントターゲット未作成**、または **作成直後で `creating`**、**SG で 2049 が不通**、**VPC DNS 無効** | **6-3**〜**6-5** を確認し、`list-mount-targets` で `available` になってから再マウント |
| `rsync`: `chgrp ... Operation not permitted`（マウント先） | **`rsync -a` が NFS 上で `chgrp` を試みる** | **`--no-group`**（必要なら **`--no-owner`**）。詳細は **6-6-1** |
| `rsync`: `mkstemp ... Permission denied`（マウント先） | **転送先に一時ファイルを作れない** | **`--temp-dir=/tmp`**（**`-T /tmp`**）。詳細は **6-6-1** |
| `rsync`: `failed to set times on ".../."` | **ディレクトリの時刻を NFS が拒否** | **`--omit-dir-times`**（**`-O`**）。詳細は **6-6-1** |
| `rsync` / `cp`: `.../mods/mod-list.json`: **Permission denied** | **転送先が `factorio` 非所有**、または **S3 の空キー `mods/` がファイル扱い** | **`sudo chown -R factorio:factorio`** で揃える。改善しない場合は **6-6-1** の `ls -la` と S3 の `mods/` キー確認 |

ログの参照先の例: `/var/log/amazon/efs/mount.log`（[efs-utils README](https://github.com/aws/efs-utils)）。

### 6-8. logs をローカル出力して停止時に S3 同期する

`saves/mods/config` を S3 Files 側で運用しつつ、`logs` はローカル出力 + 停止時同期とする場合の最小構成です。

1. `factorio-dev.service` / `factorio-prod.service` の `WorkingDirectory` を `/opt/factorio/logs` に固定する。  
2. 停止フローは「`/save` → logs 同期 → EC2 停止」の順序で実行する（直接 `StopInstances` しない）。  
3. 同期先は `s3://<S3_BUCKET_NAME>/logs/date=YYYY-MM-DD/` とし、同日複数停止でも上書きしないようにセッションIDをファイル名へ付与する（例: `factorio-current__session-20260420T142530Z.log`）。

同期コマンド例（EC2 上）:

```bash
SESSION_ID=$(date -u +%Y%m%dT%H%M%SZ)
LOG_DATE=${SESSION_ID:0:4}-${SESSION_ID:4:2}-${SESSION_ID:6:2}
aws s3 cp /opt/factorio/logs/factorio-current.log "s3://<S3_BUCKET_NAME>/logs/date=${LOG_DATE}/factorio-current__session-${SESSION_ID}.log"
aws s3 cp /opt/factorio/logs/factorio-previous.log "s3://<S3_BUCKET_NAME>/logs/date=${LOG_DATE}/factorio-previous__session-${SESSION_ID}.log"
printf '{"session_id":"%s","utc_date":"%s","jst_date":"%s"}\n' "$SESSION_ID" "$LOG_DATE" "$(TZ=Asia/Tokyo date +%F)" > /tmp/session-${SESSION_ID}.json
aws s3 cp /tmp/session-${SESSION_ID}.json "s3://<S3_BUCKET_NAME>/logs/date=${LOG_DATE}/session-${SESSION_ID}.json"
```

補足:

- 同期順序の担保は Lambda 側（Executor の stop オーケストレーション）で実装する。
- AMI + 起動オプション運用でも、停止経路を一本化すればログ退避漏れを抑えられる。

## 7. EC2 インスタンスへの IAM ロール割り当て

### 7-1. インスタンスプロファイル作成（初回のみ）

```bash
aws iam create-instance-profile --instance-profile-name <INSTANCE_PROFILE_NAME>
aws iam add-role-to-instance-profile --instance-profile-name <INSTANCE_PROFILE_NAME> --role-name <EC2_ROLE_NAME>
```

開発環境で本番と分離する例（`-dev` サフィックス）:

```bash
# 1) カスタムポリシーを作成（事前に policy.json は dev 向けARNで生成済みであること）
aws iam create-policy --policy-name FactorioServerPolicy-dev --policy-document file://aws/IAM/FactorioServerPolicy/policy.json

# 2) EC2ロールを作成（EC2の信頼ポリシー）
aws iam create-role --role-name EC2-Factorio-Server-Role-dev --assume-role-policy-document '{
  "Version":"2012-10-17",
  "Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]
}'

# 3) マネージドポリシー + カスタムポリシーをアタッチ
aws iam attach-role-policy --role-name EC2-Factorio-Server-Role-dev --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam attach-role-policy --role-name EC2-Factorio-Server-Role-dev --policy-arn arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy
aws iam attach-role-policy --role-name EC2-Factorio-Server-Role-dev --policy-arn arn:aws:iam::<ACCOUNT_ID>:policy/FactorioServerPolicy-dev
```

### 7-2. EC2 へアタッチ

```bash
aws ec2 associate-iam-instance-profile \
  --instance-id <INSTANCE_ID> \
  --iam-instance-profile Name=<INSTANCE_PROFILE_NAME>
```

開発環境の推奨命名例:

- `INSTANCE_PROFILE_NAME`: `EC2-Factorio-Server-Profile-dev`
- `EC2_ROLE_NAME`: `EC2-Factorio-Server-Role-dev`

### 7-3. EC2 上で確認

```bash
curl -s http://169.254.169.254/latest/meta-data/iam/security-credentials/
aws sts get-caller-identity
```

## 8. サーバーレスアプリからの疎通確認

最小確認項目:

1. Lambda から `ec2:StartInstances` / `ec2:StopInstances` が実行できる  
2. Lambda から RCON ポート `27015/tcp` へ到達できる  
3. RCON コマンド送信（例: `/players online` 相当）が成功する

```bash
# EC2起動
aws ec2 start-instances --instance-ids <INSTANCE_ID>
aws ec2 wait instance-running --instance-ids <INSTANCE_ID>

# EC2停止
aws ec2 stop-instances --instance-ids <INSTANCE_ID>
aws ec2 wait instance-stopped --instance-ids <INSTANCE_ID>
```

RCON は Lambda 実装側（Notifier / Worker）から実行し、CloudWatch Logs でレスポンスを確認してください。

## 9. AMI 化と起動テンプレート作成（ID:006 / DYNAMIC 運用準備）

本節は、ここまでの手順で構築した **1つの EC2 インスタンス環境**をベースに AMI を作成し、DYNAMIC 起動へ移行するための手順です。  
本番のみを運用する場合はこのまま進めてください。開発/本番を分離する場合は、同じ手順で `-prod` / `-dev` サフィックスを付けたリソース（AMI 名、Launch Template 名、ロール名、ポリシー名など）をそれぞれ作成する運用を推奨します。  
**公開ドキュメントには実値を残さない**ため、以下はすべてプレースホルダーで記載しています。

### 9-1. 事前準備（現在値の確認）

```bash
# 対象インスタンスのネットワーク・起動設定を取得（テンプレート作成で再利用）
aws ec2 describe-instances \
  --instance-ids <INSTANCE_ID> \
  --region <REGION> \
  --profile <PROFILE> \
  --query "Reservations[0].Instances[0].{SubnetId:SubnetId,SecurityGroups:SecurityGroups[].GroupId,InstanceType:InstanceType,KeyName:KeyName,IamProfile:IamInstanceProfile.Arn}" \
  --output table
```

### 9-2. AMI 作成（停止ベース）

```bash
# 1) 停止（稼働中なら）
aws ec2 stop-instances --instance-ids <INSTANCE_ID> --region <REGION> --profile <PROFILE>
aws ec2 wait instance-stopped --instance-ids <INSTANCE_ID> --region <REGION> --profile <PROFILE>

# 2) AMI 作成
aws ec2 create-image \
  --instance-id <INSTANCE_ID> \
  --name <BASE_AMI_NAME> \
  --description "Base AMI for Factorio dynamic run mode" \
  --no-reboot \
  --region <REGION> \
  --profile <PROFILE> \
  --query "ImageId" \
  --output text
```

AMI の状態確認:

```bash
aws ec2 describe-images \
  --image-ids <AMI_ID> \
  --owners self \
  --region <REGION> \
  --profile <PROFILE> \
  --query "Images[0].{State:State,Name:Name,CreationDate:CreationDate}" \
  --output table
```

### 9-3. 起動テンプレート作成（初回）

共通 AMI を dev/prod で使い回す場合は、Launch Template の `UserData` で `/etc/fstab` を毎回再生成して環境差分を吸収する運用を推奨します。

`lt-userdata.sh`（例）:

```bash
#!/bin/bash
set -euo pipefail

S3_FILES_SYSTEM_ID="<S3_FILES_SYSTEM_ID>"
SERVER_SETTINGS_FILE_NAME="<SERVER_SETTINGS_FILE_NAME>"

mkdir -p /mnt/factorio-data /mnt/factorio-data/saves /mnt/factorio-data/mods /mnt/factorio-data/config

tmp_fstab="$(mktemp)"
awk '$2 != "/mnt/factorio-data" {print}' /etc/fstab > "$tmp_fstab"
echo "${S3_FILES_SYSTEM_ID}:/  /mnt/factorio-data  s3files  _netdev,rw  0  0" >> "$tmp_fstab"
cp /etc/fstab /etc/fstab.bak.factorio 2>/dev/null || true
mv "$tmp_fstab" /etc/fstab

mountpoint -q /mnt/factorio-data || mount -a

if [ ! -f "/mnt/factorio-data/config/${SERVER_SETTINGS_FILE_NAME}" ]; then
  if [ -f "/opt/factorio/config/${SERVER_SETTINGS_FILE_NAME}" ]; then
    cp "/opt/factorio/config/${SERVER_SETTINGS_FILE_NAME}" "/mnt/factorio-data/config/${SERVER_SETTINGS_FILE_NAME}"
  elif [ -f "/opt/factorio/config/server-settings.json" ]; then
    cp /opt/factorio/config/server-settings.json "/mnt/factorio-data/config/${SERVER_SETTINGS_FILE_NAME}"
  elif [ -f "/opt/factorio/data/server-settings.example.json" ]; then
    cp /opt/factorio/data/server-settings.example.json "/mnt/factorio-data/config/${SERVER_SETTINGS_FILE_NAME}"
  fi
fi

chown -R factorio:factorio /mnt/factorio-data || true
```

`UserData` は Base64 で埋め込んだ `launch-template-data` JSON を使います（PowerShell 例）:

```powershell
$userdata = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes((Get-Content -Raw .\lt-userdata.sh)))
$ltData = @{
  ImageId = "<AMI_ID>"
  InstanceType = "<INSTANCE_TYPE>"
  KeyName = "<KEY_NAME>"
  SecurityGroupIds = @("<SG_ID_1>", "<SG_ID_2>")
  SubnetId = "<SUBNET_ID>"
  IamInstanceProfile = @{ Name = "<INSTANCE_PROFILE_NAME>" }
  UserData = $userdata
} | ConvertTo-Json -Depth 6 -Compress
$ltData | Set-Content -Encoding ascii .\lt-data.json
```

```bash
aws ec2 create-launch-template \
  --launch-template-name <LAUNCH_TEMPLATE_NAME> \
  --version-description "base-v1" \
  --launch-template-data file://lt-data.json \
  --region <REGION> \
  --profile <PROFILE> \
  --query "LaunchTemplate.{Id:LaunchTemplateId,Name:LaunchTemplateName}" \
  --output table
```

既存テンプレートを更新する場合（新バージョン追加）:

```bash
aws ec2 create-launch-template-version \
  --launch-template-id <LAUNCH_TEMPLATE_ID> \
  --source-version '$Latest' \
  --version-description "base-v2" \
  --launch-template-data file://lt-data.json \
  --region <REGION> \
  --profile <PROFILE> \
  --query "LaunchTemplateVersion.{Version:VersionNumber,TemplateId:LaunchTemplateId}" \
  --output table
```

### 9-4. `.env.<env>` 反映項目（DYNAMIC）

`ID:006` 実装では以下を参照します。

```dotenv
SERVER_RUN_MODE='DYNAMIC'
INSTANCE_LIFECYCLE_MODE='PERSISTENT'   # または EPHEMERAL
DYNAMIC_CAPACITY_MODE='ONDEMAND'       # または SPOT
BASE_AMI_ID='<AMI_ID>'
LAUNCH_TEMPLATE_ID='<LAUNCH_TEMPLATE_ID>'
LAUNCH_TEMPLATE_VERSION='$Latest'
SUBNET_ID='<SUBNET_ID>'
SECURITY_GROUP_IDS='<SG_ID_1>,<SG_ID_2>'
INSTANCE_TYPE='<INSTANCE_TYPE>'
KEY_NAME='<KEY_NAME>'
EC2_INSTANCE_PROFILE_NAME='<INSTANCE_PROFILE_NAME>'
```

> 変更後は `python scripts/register.py <env>` を実行し、SSM へ同期してください。

### 9-5. 動作確認（DYNAMIC）

1. `deploy_lambda.py <env>` で Executor / Worker を反映  
2. `/start` 実行後に DynamoDB の `ActiveInstanceId` が作成されることを確認  
3. EventBridge の EC2 state-change ルールが `instance-id=<ActiveInstanceId>` に更新されることを確認

補足（AMI の扱い）:

- 1回の `create-image` で作成されるのは **1つの AMI** です（自動で「本番用」と「開発用」の2つが同時作成されるわけではありません）。
- 同じ AMI を本番/開発で共用する運用は可能です。
- 環境ごとに差分（パッケージ構成、タグ、ミドルウェア設定など）を分けたい場合は、環境ごとに AMI を作成してください。

## 10. 付録: 技術的な解説と選定理由

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
