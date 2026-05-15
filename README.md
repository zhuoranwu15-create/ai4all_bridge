# AI4ALL Weixin Bot

## Local Backend

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Health check:

```bash
curl http://127.0.0.1:8000/health
```

Mock OpenClaw turn:

```bash
python scripts/send_mock_turn.py --text "你好"
```

## LLM

The backend uses an OpenAI-compatible `/chat/completions` API when `LLM_API_KEY`
is set. If `LLM_API_KEY` is empty, it keeps the local mock fallback.

```bash
cp .env.example .env
# edit LLM_API_KEY / LLM_BASE_URL / LLM_MODEL
```

## Storage

SQLite is used for the first version. The default database path is:

```text
data/ai4all.sqlite3
```

The backend separates users by `account_id + session_key`, so one WeChat account
can serve multiple private chat users with isolated context.
