# MacBookでのセットアップ・起動手順

更新日: 2026-10-11 JST
対象: [HARA-Akihiko/x-ai-auto-post](https://github.com/HARA-Akihiko/x-ai-auto-post)

MacBook（Apple Silicon / Intel）で Docker Desktop と Docker Compose を使って起動する手順です。API と投稿 Worker は別プロセスです。**初回は API の動作確認、OAuth 接続を完了してから Worker を起動**し、意図しない X 投稿を防ぎます。

> **利用条件:** ChatGPT は Sign in with ChatGPT の token sharing（オープンソースアプリ向け、preview）で接続します。OpenAI API Key や事前登録の Client ID は不要ですが、Sign in 時に ChatGPT プランの利用を許可する必要があります。Plus の5時間利用上限は他のアプリと共有されます。X 側では、有効な OAuth アプリ・投稿権限・API 利用枠が必要です。手順5の smoke test が成功するまで Worker を起動しないでください。API Key への自動フォールバックはありません。

## 1. 事前準備

- [Docker Desktop for Mac](https://docs.docker.com/desktop/setup/install/mac-install/) をインストールし、起動する（Apple Silicon / Intel に対応する版）。
- Git と Python 3 を使用できるようにする。
- [uv](https://docs.astral.sh/uv/getting-started/installation/) をインストールする（例: `brew install uv`）。ChatGPT の Sign in は MacBook 上のブラウザで行うため、CLI を MacBook で実行するのに使います。API と Worker はコンテナで動くため、それ以外の用途では不要です。

ターミナルで確認:

```bash
git --version
docker --version
docker compose version
python3 --version
uv --version
```

## 2. ソースコードの取得

```bash
cd ~
git clone https://github.com/HARA-Akihiko/x-ai-auto-post.git
cd x-ai-auto-post
cp .env.example .env
```

すでにクローンしている場合はリポジトリに移動し、既存の `.env` を上書きしないでください。

## 3. `.env` の作成・編集

暗号鍵と管理APIトークンをそれぞれ生成します。

```bash
# TOKEN_ENCRYPTION_KEY（Fernet互換の32バイト鍵）
python3 -c 'import os, base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())'

# ADMIN_API_TOKEN（32文字以上）
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

`nano .env` などで編集します。生成した値を混同しないでください。以下は**設定例**で、`REPLACE_...` を実際の秘密値へ置換します。

```dotenv
POSTGRES_USER=x_ai
POSTGRES_DB=x_ai
POSTGRES_PASSWORD=REPLACE_WITH_STRONG_DB_PASSWORD
DATABASE_URL=postgresql+psycopg://x_ai:REPLACE_WITH_STRONG_DB_PASSWORD@db:5432/x_ai
TOKEN_ENCRYPTION_KEY=REPLACE_WITH_FERNET_KEY
ADMIN_API_TOKEN=REPLACE_WITH_ADMIN_TOKEN
```

- `POSTGRES_PASSWORD` と `DATABASE_URL` 内のパスワードは一致させる。URL 内の特殊文字には URL エンコードが必要です。
- Compose 内の DB ホスト名は `db` です。`localhost` には変更しないでください。
- `.env` は Git にコミットしない。認証情報を Issue・ログ・画面共有などへ貼り付けないでください。
- `TOKEN_ENCRYPTION_KEY` を DB とは別に安全にバックアップする。変更・紛失すると保存済みの OAuth 認証情報を復号できず、再認証が必要です。
- 既存の DB ボリュームがある場合、`.env` のパスワード値を変更しただけでは DB 内の既存パスワードは更新されません。

自動投稿に必要な追加設定（値は `.env.example` を参照）:

| 設定 | 内容 |
| --- | --- |
| `CHATGPT_HOST_ID` | このサーバー用の host ID。`python3 -c 'import uuid; print("urn:uuid:" + str(uuid.uuid4()))'` で一度だけ生成し、変更しない |
| `CHATGPT_MODEL` | 接続アカウントで使えるモデル。手順5の smoke test が一覧を表示する |
| `X_CLIENT_ID` | X Developer Portal の Client ID |
| `X_CLIENT_SECRET` | X アプリのクライアント種別で必要な場合 |
| `X_REDIRECT_URI` | 既定 `http://localhost:8000/auth/x/callback` |

X のリダイレクト URL は、X に登録した値と完全に一致させてください。ChatGPT の Client ID・Client Secret・リダイレクト URL は設定しません（Sign in 時に発行・決定されます）。他アプリの Codex Client ID を流用しないでください。

## 4. DB・マイグレーション・API の起動

投稿 Worker を起動せずに基本動作を確認します。

```bash
docker compose config --quiet
docker compose up --build -d db migrate api
docker compose ps -a
curl --fail-with-body http://127.0.0.1:8000/health
```

成功すれば以下の応答が返ります。

```json
{"status":"ok"}
```

`migrate` が正常終了（通常 `Exited (0)`）、`db` と `api` が稼働中であることを確認します。このヘルスチェックは DB 接続を検証するもので、OAuth や X 投稿の成功は保証しません。

```bash
docker compose logs --tail=100 db migrate api
```

API は MacBook 自身の `127.0.0.1:8000` のみに公開されます。Swagger UI (`/docs`) は無効です。

## 5. ChatGPT と X の OAuth 接続

### ChatGPT（MacBook 上の CLI で Sign in → コンテナへ取り込み）

MacBook で Sign in します。ブラウザが開くので、ChatGPT にサインインし、ChatGPT プランの利用を許可します。

```bash
uv sync --frozen
uv run python -m app.cli.chatgpt login
```

- callback は `http://127.0.0.1:1455/auth/callback` です。ポート1455が使用中なら `--port <番号>` を付けます。
- ChatGPT プランの利用を許可しなかった場合は、`--consent` を付けて再実行します。
- 認証情報は `~/.config/x-ai-auto-post/chatgpt-credentials.json`（権限 `0600`）に保存されます。このファイルを共有・コミットしないでください。

コンテナ内の DB へ取り込み、smoke test で接続を確認します。smoke test は ChatGPT プランの利用枠を消費します。

```bash
docker compose run --rm -T api python -m app.cli.chatgpt import \
  < ~/.config/x-ai-auto-post/chatgpt-credentials.json
docker compose run --rm api python -m app.cli.chatgpt smoke
```

smoke test は使えるモデルを表示します。`CHATGPT_MODEL` が一覧にない場合は、`.env` を修正して再実行します（稼働中の API には `docker compose up -d api` で反映します）。`OK: response.completed received` が表示されれば成功です。

### X

管理トークンをシェル変数として読み込みます（値を画面に出力しない）。

```bash
ADMIN_TOKEN="$(sed -n 's/^ADMIN_API_TOKEN=//p' .env)"
```

X の認証 URL を発行:

```bash
curl --fail-with-body -sS \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8000/auth/x/login
```

返される `authorization_url` を **MacBook 上のブラウザ**で開き、接続を許可します。`state` は10分の有効期限付き・1回限りです。認証 URL やコールバック URL を第三者と共有しないでください。

接続状態の確認:

```bash
curl --fail-with-body -sS -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8000/auth/chatgpt/status
curl --fail-with-body -sS -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8000/auth/x/status
```

**両方 `connected: true` を確認**してください。`expired: true` は、次回利用時に refresh されます。ChatGPT が `reauthorization_required: true` の場合は、`login` と `import` をやり直してください。確認後は `unset ADMIN_TOKEN` で消去できます。

## 6. 投稿 Worker の起動

OAuth 接続に加え、投稿アカウント・公開内容・スケジュールを確認したうえで実行します。**Worker が起動すると、指定時刻に実際の X 投稿が発生する可能性があります。**

```bash
docker compose up -d worker
docker compose ps -a
docker compose logs -f --tail=100 worker
```

初期時刻は `08:00,13:00,19:00`、タイムゾーンは `Asia/Tokyo` です。DB にスケジュールが登録された後は、`.env` を変更するだけではなく管理 API `PUT /schedules` で変更します。長時間停止分を無制限に遡って自動投稿する設計ではありません。

管理APIの詳細は [README（接続と管理 API）](../README.md#接続と管理-api) を参照してください。

## 7. 停止・再起動・削除

```bash
# 自動投稿だけ停止
docker compose stop worker

# 全サービス停止（DBデータは保持）
docker compose stop

# 再起動
docker compose start

# ログ確認
docker compose logs --tail=100 api worker

# コンテナ削除（名前付きDBボリューム保持）
docker compose down
```

**`docker compose down -v` を通常運用で実行しないでください。** PostgreSQL の名前付きボリュームが削除され、DB データを失う可能性があります。ソフトウェア更新前は Worker を止め、DB と暗号鍵をバックアップし、マイグレーションの影響を確認します。

MacBook がスリープ・シャットダウンした場合、Docker Worker は継続できません。24時間稼働が必要なら、常時起動できるホストを使用してください。

## 8. トラブルシューティング

| 現象 | 確認方法 |
| --- | --- |
| `docker: command not found` | Docker Desktop のインストールとシェル PATH |
| `Cannot connect to the Docker daemon` | Docker Desktop が起動しているか |
| `Set POSTGRES_PASSWORD` | `.env` の DB パスワード設定 |
| API 起動失敗 | `docker compose ps -a` / `docker compose logs --tail=100 db migrate api` |
| `/health` が503 | DBパスワード、接続 URL、マイグレーション |
| ポート8000の競合 | `lsof -nP -iTCP:8000 -sTCP:LISTEN` |
| 管理APIが401 | `ADMIN_API_TOKEN` と `Authorization: Bearer ...` |
| `oauth_not_configured` | X の Client ID、`CHATGPT_HOST_ID`（import 時） |
| `oauth_scope_missing`（ChatGPT の login） | `login --consent` で ChatGPT プランの利用を許可し直す |
| `oauth_reauthorization_required` | ChatGPT の `login` と `import` をやり直す |
| `127.0.0.1:1455 is not available` | `login --port <番号>` で別のポートを使う |
| `subscription_sharing_*`（smoke test） | ChatGPT の Settings → Usage で利用枠・利用資格を確認 |
| X の OAuth が失敗 | 登録済み redirect URI、許可 scope、利用資格 |
| 自動投稿されない | Workerのログと管理API `GET /jobs/runs` の `error_code` |

**二重投稿防止:** X への送信結果が通信障害で不明な場合、同じ投稿を自動で再POSTしない設計です。X の実際の投稿履歴を確認する前に手動再送しないでください。

## 出典・実装上の根拠

- [README.md](../README.md)
- [compose.yaml](../compose.yaml)
- [.env.example](../.env.example)
- [Dockerfile](../Dockerfile)
- [app/main.py](../app/main.py)
- [app/core/config.py](../app/core/config.py)
- [app/services/auth.py](../app/services/auth.py)
- [app/cli/chatgpt.py](../app/cli/chatgpt.py)
- [app/scheduler/worker.py](../app/scheduler/worker.py)
- [Docker Desktop（macOS）公式](https://docs.docker.com/desktop/setup/install/mac-install/)
- [Sign in with ChatGPT – token sharing for open-source apps](https://developers.openai.com/siwc/token-sharing-open-source)
- [uv のインストール](https://docs.astral.sh/uv/getting-started/installation/)
- [X OAuth 2.0 / PKCE](https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code)
