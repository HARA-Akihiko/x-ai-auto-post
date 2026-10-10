# x-ai-auto-post

MacBookでのセットアップ: [docs/mac-setup.md](docs/mac-setup.md)
Sign in with ChatGPTを利用し、AIで最新記事を収集・分析・要約してX向け投稿文を生成し、FastAPI・APScheduler・X APIで1日に複数回自動投稿するアプリケーション。

## 構成

Python 3.12 / FastAPI / SQLAlchemy / Alembic / PostgreSQL / APScheduler /
httpx。API と投稿 Worker は**別プロセス**です。API の lifespan は HTTP client
だけを管理し、Scheduler は起動しません。外部 API に API Key を渡す実装はありません。

```text
公式 RSS → ArticleCollector → 正規化・DB重複排除・ローカル候補選定
→ ArticleAnalyzer → ChatGPT OAuth / Responses API → PostGenerator
→ 投稿検証 → PostgreSQL Draft → 独立 Worker → X API v2
```

## 認証仕様と前提

ChatGPT は OpenAI の **Sign in with ChatGPT の token sharing（オープンソースアプリ向け、preview）**
で接続します。OpenAI API Key、client secret、事前登録の client ID は使いません。初回の
Sign in で client が動的に登録され、発行された client ID（issued client ID）を保存して以後も
使います。他アプリの Codex client ID、ブラウザ cookie、非公開 `backend-api` は使用しません。
ChatGPT プランを使えない場合に、従量課金 API Key へ自動フォールバックすることもありません。

- Sign in 時に、利用者が ChatGPT プランの利用（`chatgpt.tokens.use.direct`）に同意する必要が
  あります。この scope が付与されていなければ推論しません。
- ChatGPT Plus の5時間利用上限は、ChatGPT プランを使う全アプリで共有されます。利用状況、
  アプリ別の上限、接続解除は ChatGPT の Settings → Usage で確認できます。
- 公式文書の対象は、オープンソースでローカル／セルフホストのアプリです。第三者向けに有償または
  リモートで提供する場合は、OpenAI の interest form からの申請が必要です。
- preview のため仕様が変わる可能性があります。導入前に以下の公式資料を確認してください。

2026-10-11 に、以下の公式資料と
`https://auth.openai.com/.well-known/openid-configuration` を直接取得して確認しました。

- [Token sharing overview](https://developers.openai.com/siwc/token-sharing-open-source)
- [Registration and sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [Accounts and sessions](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)
- [Models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [Self-hosted VMs](https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms)
- [Token reference](https://developers.openai.com/siwc/token-sharing-open-source/token-reference)
- [Errors and recovery](https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery)
- [Preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)
- [X OAuth 2.0 / PKCE](https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code)
- [X character counting](https://docs.x.com/fundamentals/counting-characters)

### ChatGPT の認証フロー

`127.0.0.1` の loopback callback は、ブラウザを開いた PC にしか届きません。そのため Sign in は
ブラウザのある PC で CLI（`python -m app.cli.chatgpt login`）として実行し、取得した credential
を API / Worker が動くサーバー（ローカルの Compose またはセルフホスト VM）へ取り込みます。

1. CLI が、この PC 用の `ext_agent_host_id`（`urn:uuid:…`）を初回だけ生成して保存します。
2. 初回は `client_id=dynamic_agent_client` と `agent_name_hint=x-ai-auto-post` で認可します。
   再認証では、保存済みの issued client ID を使います。scope は
   `openid profile email offline_access resource.invoke chatgpt.tokens.use.direct`、
   resource は `https://api.openai.com/v1` です。
3. callback（`http://127.0.0.1:<port>/auth/callback`）で state を照合し、callback で返された
   issued client ID と PKCE S256 で code を交換します。
4. ID token を OpenAI の JWKS で署名検証し、`iss`・`aud`（issued client ID）・`exp`・`nonce`・
   `sub` を確認します。付与 scope に `chatgpt.tokens.use.direct` がなければ保存しません。
5. credential を `~/.config/x-ai-auto-post/chatgpt-credentials.json`（権限 `0600`）に保存します。
6. サーバーで `import` すると、token を Fernet で暗号化して PostgreSQL に保存します。host ID は、
   サーバー自身の `CHATGPT_HOST_ID` を使います。
7. 以後の refresh はサーバーが行います。access token の期限の5分前になると、issued client ID・
   refresh token・resource だけを送って refresh し、返された新しい refresh token に置き換えます
   （rotation）。API と Worker から同時に呼ばれても、PostgreSQL の行ロックで refresh を1回に
   まとめます。refresh の応答を受け取った後は、処理がキャンセルされても新しい token を保存します。

サーバーは ID token を推論の権限判定に使いません。アカウントの識別子（`sub`）は、CLI で検証した
値を保存します。import するファイルは、運用者が自分の PC から SSH などで渡す入力として信頼し、
ID token の再検証はしません。

X は `tweet.read tweet.write users.read offline.access` を要求し、FastAPI の login / callback、
PKCE S256、期限付き一回限りの state で接続します。X アプリには、投稿・ユーザー情報・直近投稿取得の
権限と利用枠が必要です。本アプリは単一所有者・単一接続アカウント用です。

Responses は OAuth access token を使い、`stream=true`、`store=false` で
呼び出します。token 期限は固定せず token response の `expires_in` を使用します。
access / refresh / id token と X の PKCE verifier は Fernet で暗号化保存し、
PostgreSQL 行ロック下で refresh・rotation を保存します。

## 起動

```bash
cp .env.example .env
uv sync --frozen
uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
uv run python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

生成した値をそれぞれ `TOKEN_ENCRYPTION_KEY` / `ADMIN_API_TOKEN` に設定します。
`.env` はコミットしないでください。`DATABASE_URL` には PostgreSQL 接続 URL
を指定します。Compose では host を `db` とし、`POSTGRES_PASSWORD` と一致する
パスワードを URL に URL エンコードして含めてください。
X の OAuth 設定、`CHATGPT_HOST_ID`、`CHATGPT_MODEL` を入力してから起動します。

```bash
docker compose up --build -d
```

Compose は DB の ready → Alembic → API / Worker の順で起動します。
API はホストの `127.0.0.1:8000` のみに公開します。外部公開する場合は HTTPS
reverse proxy と、X に登録済みの HTTPS callback を設定してください。
DB をインターネットに公開しないでください。

ローカル開発では PostgreSQL を起動したうえで、別ターミナルで実行します。

```bash
uv run alembic upgrade head
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
uv run python -m app.scheduler.worker
```

OAuth code/state の query を記録しないよう、Uvicorn の access log は無効にしています。
reverse proxy でも callback query と Authorization header を記録しないでください。
暗号化キーは DB と別に保管・バックアップしてください。キー紛失時は再認証が必要です。
管理トークンを URL query に渡さないでください。

## ChatGPT の Sign in と取り込み

まず、サーバー側の `.env` に、サーバー自身の host ID と利用するモデルを設定します。

```bash
python3 -c 'import uuid; print("urn:uuid:" + str(uuid.uuid4()))'   # CHATGPT_HOST_ID に設定
```

`CHATGPT_MODEL` は、下記の smoke test が表示する、接続アカウントで使えるモデルから選びます。

### 1. ブラウザのある PC で Sign in する

このリポジトリを PC に取得して `uv sync --frozen` を実行してから、次を実行します。

```bash
uv run python -m app.cli.chatgpt login
```

ブラウザで ChatGPT にサインインし、agent 名を確認して ChatGPT プランの利用を許可します。

- `127.0.0.1:1455` が使用中なら、`--port <番号>` で変更できます（変えられるのはポートだけです）。
- ChatGPT プランの利用を許可しなかった場合は、`--consent` を付けて再実行します。
- DB や暗号鍵は不要です。端末に token は表示しません。

### 2. サーバーへ取り込む

ローカルの Docker Compose の場合:

```bash
docker compose run --rm -T api python -m app.cli.chatgpt import \
  < ~/.config/x-ai-auto-post/chatgpt-credentials.json
```

セルフホスト VM の場合（SSH で転送し、以後は VM が refresh を管理します）:

```bash
ssh <vm> 'cd x-ai-auto-post && docker compose run --rm -T api python -m app.cli.chatgpt import' \
  < ~/.config/x-ai-auto-post/chatgpt-credentials.json
```

- 取り込んだ後は、サーバーだけが refresh します。refresh token は使うたびに置き換わるため、
  同じ credential を複数のホストで使わないでください。
- PC 側のファイルには token が含まれます。再認証用に issued client ID の記録として残す場合は、
  権限 `0600` のまま安全に保管してください。
- 別の ChatGPT アカウントに切り替えるときは、`DELETE /auth/chatgpt` を実行してから取り込みます。

### 3. 動作を確認する（smoke test）

```bash
docker compose run --rm api python -m app.cli.chatgpt smoke
```

接続アカウントで使えるモデル（`GET /v1/models` のうち `visibility: "list"` のもの）を表示します。
`CHATGPT_MODEL` が含まれていれば、Responses API を1回呼び、`response.completed` まで受信できる
ことを確認します。ChatGPT プランの利用枠を消費するため、CI では実行しません。

### 再認証・切断

- refresh token が使えなくなった場合（`invalid_grant`、`refresh_token_reused` など）、サーバーは
  token を消去して `oauth_reauthorization_required` を返します。同じ PC で `login` を再実行して
  から `import` します。保存済みの issued client ID が再利用されます。一時的な通信障害や 5xx では
  token を消去しません。
- `DELETE /auth/chatgpt` は、サーバーの token を消去します（client ID と host ID は残します）。
  OpenAI 側での接続解除は、ChatGPT の Settings で行ってください。
- PC 側で別のアカウントを新規登録するときは、`~/.config/x-ai-auto-post/chatgpt-credentials.json`
  を削除してから `login` します。`host-id` は削除しないでください。

### 旧版からの移行

FastAPI の `/auth/chatgpt/login` を使っていた旧版からは、次の手順で移行します。

1. `.env` から `CHATGPT_CLIENT_ID`・`CHATGPT_CLIENT_SECRET`・`CHATGPT_REDIRECT_URI`・
   `CHATGPT_SCOPE`・`CHATGPT_RESOURCE`・`CHATGPT_AUTHORIZE_URL`・`CHATGPT_TOKEN_URL` を削除します
   （残っていても無視されます）。
2. `CHATGPT_HOST_ID` を上記の形式で設定します。形式が不正な場合、起動時にエラーになります。
3. `alembic upgrade head` を実行します（Compose では `migrate` が自動で実行します）。
4. 上記の手順で、Sign in と取り込みをやり直します。旧版で保存した ChatGPT credential は
   「再認証が必要」と判定され、推論には使いません。

`chatgpt_credentials` に identity 列を追加した migration より前へ `alembic downgrade` した場合も、
ChatGPT の Sign in と取り込みをやり直してください。

## 接続と管理 API

`/health` と X の OAuth callback 以外の API には、`Authorization: Bearer <ADMIN_API_TOKEN>` が
必要です。X は、login API が返す `authorization_url` をブラウザで開いて同意します。callback は
一回限りの state で検証します。ChatGPT は上記の CLI で接続します。X と ChatGPT の両方を接続して
ください。

| API | 用途 |
| --- | --- |
| `GET /health` | DB 接続の readiness |
| `GET /auth/x/login` | X の PKCE 認可 URL を発行 |
| `GET /auth/x/callback` | X の code 交換と暗号化保存 |
| `GET /auth/{chatgpt,x}/status` | token を含まない接続状態 |
| `DELETE /auth/{chatgpt,x}` | サーバーの token を削除（client ID・host ID は保持） |
| `GET /sources`, `GET /articles` | 収集元・候補の確認 |
| `POST /articles/collect` | RSS 収集のみ（AI 不使用） |
| `GET /schedules`, `PUT /schedules` | 投稿時刻と timezone の変更 |
| `GET /posts`, `POST /posts/generate` | Draft の確認・生成 |
| `POST /posts/{id}/publish` | Worker に投稿要求をキュー登録 |
| `POST /jobs/run-now` | Worker に全工程の要求をキュー登録 |
| `GET /jobs/runs` | 結果・安全な error_code の確認 |

`PUT /schedules` の例：

```json
{"times":["08:00","13:00","19:00"],"timezone":"Asia/Tokyo"}
```

初期値は `POST_TIMES` と `TIMEZONE` です。DB に時刻が登録された後は管理 API
を使用します。OS timezone に依存しません。`POST /jobs/run-now` は
`{"idempotency_key":"operator-request-001"}` のような body を要求します。
同じキーを再送しても新しい Job は作成しません。
Worker は最大 5 分前までの未実行 slot を拾います。長時間停止中の投稿を
再起動時にまとめて送信することはありません。
切断はローカル削除です。プロバイダ側での許可取消はアカウント設定で行ってください。

## 収集・AI 利用量

初期 feed は OpenAI / GitHub / Google / Microsoft / Hugging Face / Zenn です。
Zenn はコミュニティ記事です。Anthropic を含む公式サイトの大量スクレイピングは
行いません。feed URL は運用者が DB で管理し、一般公開の任意 URL 取得 API はありません。
feed の失敗は source 単位で処理し、他 source の収集を続けます。

URL から tracking query と fragment を除去し、canonical URL と本文特徴 hash
を UNIQUE 制約で重複排除します。全文を取得して AI に送ることはありません。
日付、source priority、Claude Code / Codex / Copilot / MCP / agentic coding /
開発効率等の keyword、過去投稿をローカルで判定します。
`CANDIDATE_LIMIT` は 1～5（既定 3）、`ARTICLE_MAX_AGE_DAYS` は 1～30（既定 7）。
通常は候補分析 1 回、選定記事の生成 1 回だけを実行します。
候補なし・低評価・AI エラー・投稿検証失敗時は X へ送信しません。

`WEB_SEARCH_ENABLED` は既定で false です。有効化する前に接続アカウント・
モデルで Responses の hosted Web Search が許可されることを確認してください。
RSS 候補が足りない場合にのみ使用し、通常の RSS 収集で毎回検索しません。
記事の記述は非信頼データとして prompt に渡し、構造化した AI 出力を検証します。
投稿には変更点・開発者への影響・具体的な使い方を含めますが、
AI の事実誤認を完全に防ぐことはできないため、運用前に Draft を確認してください。

`URL_POLICY` は `ALWAYS` / `IMPORTANT_ONLY` / `NONE`。
`IMPORTANT_ONLY` は AI 判定の重要度が 10 点中 8 点以上の場合に URL を添えます。
標準 X 投稿の上限は weighted 280、HTTP(S) URL は 23 units として検証します。
複合 emoji は安全側に過大計数する場合があり、上限内の文を拒否する可能性があります。
自動切り捨てで意味を変更したり、検証失敗文をそのまま送信したりしません。

## 二重投稿防止と復旧

PostgreSQL advisory lock と slot/hash の UNIQUE 制約を併用します。
API が複数 Worker でも投稿 Scheduler は独立しています。
X POST 前に Draft の `publishing` と開始時刻を commit します。
成功後に `published_posts` と Draft / Job の結果を保存します。

**X は DB と同じ transaction に参加できず、exactly-once を保証できません。**
timeout・通信断・結果不明時は `publishing` を保持し、直近投稿を照合します。
同一文が見つかれば成功として記録します。X の t.co URL は entities の展開 URL
を照合に使います。見つからない／取得権限なし／照合範囲外でも、
「失敗した」と断定して自動再 POST はしません。
再起動後も同じ Draft は照合専用です。この安全性のため、未送信の投稿が
保留される場合があります。X 側を確認してから運用者が手動で対処してください。
自動照合は DB に回数を保存して最大 3 回までです。上限後は
`publish_manual_review` として保留し、X API を毎分無限に呼び続けません。
X の投稿履歴と権限を確認し、Worker を停止した状態で運用者が
Draft / Job の状態を修復してください。照合失敗だけを根拠に再送しないでください。

自動無限 retry はありません。401 は再認証、429 は枠・rate limit の確認、
5xx / network は次回処理の判断が必要です。投稿 POST の結果不明は一般的な
再実行可能エラーと区別します。ログに token・HTTP response body・記事本文は
出しません。Job ID / slot / draft / status / duration / error_code で追跡します。
Worker を停止してから DB/キーのメンテナンスを行ってください。

## テスト

```bash
uv run pytest
TEST_DATABASE_URL=postgresql+psycopg:///x_ai_test uv run pytest -m integration
```

`TEST_DATABASE_URL` は**破棄可能な専用 PostgreSQL**にしてください。
外部 OpenAI / X / RSS は HTTP 境界で Fake/Mock を使用し、DB と内部処理は
実際の SQLAlchemy を使います。外部アカウントの有料・利用枠を消費する
ライブテストは通常テストに含めません。

## 参考設計

以下の LICENSE / README / 依存 / 認証 / DB / Scheduler / エラー / テストを
調査しました。コードの転載は行っていません。

- [ftnext/sign-in-with-chatgpt-py](https://github.com/ftnext/sign-in-with-chatgpt-py)
  （Apache-2.0）：PKCE、token rotation、認証エラーの分類。
- [skalaliya/notion-x-scheduler](https://github.com/skalaliya/notion-x-scheduler)
  （MIT）：RSS とローカル記事選定。Notion/OAuth1 は採用しません。
- [pippinlovesdot/dot-automation](https://github.com/pippinlovesdot/dot-automation)
  （MIT）：FastAPI/APScheduler/PostgreSQL。API 内 Scheduler と API Key 前提は
  採用しません。
