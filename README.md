# Nimbus: an AI chatbot with cloud storage

Nimbus is a Gemini-style chat app. A **Gen AI model** (Claude or Gemini) writes the answers and streams them word by word, and a **cloud database** stores each user's account and chat history, so people can log in from any device and pick up where they left off.

![Nimbus welcome screen](docs/screenshot-welcome.png)

## What it does

- **Answers like Gemini:** replies stream in live with Markdown, tables and highlighted code, and you can stop, regenerate or copy a reply.
- **Up-to-date answers (optional):** turn on web search and the AI looks things up and shows its sources.
- **Accounts:** people sign up and log in, and every chat is saved to their account in the database.
- **Chat history:** a sidebar lists past chats, which you can rename or delete.
- **Personal answers:** "About you" in Settings is saved to the account and the AI uses it to tailor replies.
- **Your data:** users can download all their data as JSON or delete their account.
- **Works everywhere:** light and dark themes, and a layout that adapts to phones.
- **Demo mode:** runs without an API key, so you can try everything before you get one.

| Chat | Dark mode | Phone |
|---|---|---|
| ![Chat](docs/screenshot-chat.png) | ![Dark mode](docs/screenshot-dark.png) | ![Phone](docs/screenshot-mobile.png) |

## How it works

```mermaid
flowchart LR
    U["User's browser<br/>chat interface"]
    subgraph Cloud["Cloud (e.g. Google Cloud Run)"]
        A["Nimbus server<br/>Python + Starlette"]
    end
    M["Gen AI model<br/>Claude API or Gemini API"]
    D[("Cloud database<br/>PostgreSQL")]
    U -- "1. message" --> A
    A -- "2. prompt + chat history" --> M
    M -- "3. reply, word by word" --> A
    A -- "4. reply streamed live" --> U
    A <-- "each user's data" --> D
```

1. The browser sends the user's message to the server, along with a secure login cookie.
2. The server saves the message, then sends the recent chat history to the AI model with instructions (including the user's name, today's date and their "About you" notes).
3. The model streams its answer back piece by piece.
4. The server forwards each piece to the browser as it arrives, then saves the full reply.

**Gen AI** lives in `ai_providers.py`. **Cloud computing** covers the PostgreSQL database (`database.py`) and the container that runs on a cloud platform (`Dockerfile`). A rendered version of the diagram is in `docs/architecture.png`.

## Run it on your computer

You need Python 3.10 or newer.

```bash
cd nimbus-chatbot
python -m venv .venv
# Windows:      .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # Windows: copy .env.example .env
uvicorn main:app --reload
```

Open http://localhost:8000 and create an account. Without an AI key the app runs in **demo mode** with sample replies. Everything else (accounts and saved chats) works fully. Locally, data goes into a SQLite file called `chat.db`.

## Connect the AI

Put **one** key in `.env`, then restart the server.

| | Claude (Anthropic) | Gemini (Google) |
|---|---|---|
| Get a key | [console.anthropic.com](https://console.anthropic.com) → API keys | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| Cost | Pay as you go (buy credits first) | Free tier with small daily limits; paid tier for more |
| Setting in `.env` | `ANTHROPIC_API_KEY=...` | `GEMINI_API_KEY=...` |
| Default model | `claude-sonnet-5-5` (cheaper: `claude-haiku-5-5`) | `gemini-3.8-flash` |

If both keys are set, Claude is used; set `AI_PROVIDER=gemini` to choose Gemini. Model names change over time: if you get a "model not found" error, set `CLAUDE_MODEL` or `GEMINI_MODEL` to a current model.

> **Privacy note:** Google uses content sent through the Gemini **free** tier to improve its products. Tell your users, or use a paid key for real personal data.

### Web search (optional)

Set `WEB_SEARCH=true` to let the AI search the web for current information. Answers then show the sources they used.

- **Claude:** costs $10 per 1,000 searches plus normal token costs. It's on by default for your organization, and an admin can turn it off in the Claude Console.
- **Gemini:** search grounding isn't available on the free tier. Paid keys include 5,000 free searches a month, then $14 per 1,000. Google requires apps to show its "search suggestions" box with grounded answers, and Nimbus does this automatically.

## Put it in the cloud

Going live has two parts: a cloud database for user data and a cloud service to run the app.

### 1. Create a PostgreSQL database

Use any managed PostgreSQL service:

| Service | Notes |
|---|---|
| [Neon](https://neon.tech) or [Supabase](https://supabase.com) | Free starter plans, quickest to set up |
| Google Cloud SQL for PostgreSQL | Pairs with Cloud Run |
| Amazon RDS for PostgreSQL | For AWS |
| Azure Database for PostgreSQL | For Azure |

Copy its connection string into `DATABASE_URL`, for example:

```
DATABASE_URL=postgresql://user:password@host:5432/nimbus?sslmode=require
```

The tables are created automatically when the app starts. You can test this locally first by putting the URL in your `.env`.

> Don't use SQLite in the cloud. Cloud containers lose their files when they restart, so your users' chats would disappear. The app logs a warning if it detects this on Cloud Run.

### 2. Deploy the app (example: Google Cloud Run, Mumbai region)

Install the [gcloud CLI](https://cloud.google.com/sdk/docs/install), then run:

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
    artifactregistry.googleapis.com secretmanager.googleapis.com

# Keep secrets in Secret Manager, not in code
printf "%s" "YOUR_ANTHROPIC_KEY"  | gcloud secrets create anthropic-key --data-file=-
printf "%s" "YOUR_DATABASE_URL"   | gcloud secrets create database-url  --data-file=-

# Allow Cloud Run to read the secrets
PROJECT_NUMBER=$(gcloud projects describe $(gcloud config get-value project) --format="value(projectNumber)")
for s in anthropic-key database-url; do
  gcloud secrets add-iam-policy-binding $s \
    --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor"
done

# Build the Dockerfile and deploy
gcloud run deploy nimbus --source . --region asia-south1 --allow-unauthenticated \
  --set-secrets ANTHROPIC_API_KEY=anthropic-key:latest,DATABASE_URL=database-url:latest
```

Cloud Run prints an `https://...run.app` address where your chatbot is live. For Gemini, store the key as a secret and map it to `GEMINI_API_KEY` instead.

**Other clouds:** the same Docker image runs anywhere. Set the same environment variables:

| Cloud | Run the app | Database | Secrets |
|---|---|---|---|
| AWS | App Runner or ECS | RDS for PostgreSQL | Secrets Manager |
| Azure | Container Apps | Azure Database for PostgreSQL | Key Vault |
| Simple hosts | Render, Railway | Their PostgreSQL add-on | Their env settings |

## Settings (`.env`)

| Setting | Default | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` | empty | Key for the AI model. With neither set, the app runs in demo mode. |
| `AI_PROVIDER` | `auto` | `auto`, `claude`, `gemini` or `demo` |
| `CLAUDE_MODEL` / `GEMINI_MODEL` | `claude-sonnet-5-5` / `gemini-3.8-flash` | Which model to use |
| `WEB_SEARCH` | `false` | Let the AI search the web and show sources |
| `SYSTEM_PROMPT` | empty | Extra instructions for the AI (tone, topic, language…) |
| `DATABASE_URL` | empty (SQLite) | PostgreSQL connection string for the cloud |
| `APP_NAME` | `Nimbus` | Name shown in the app and given to the AI |
| `ALLOW_SIGNUPS` | `true` | Set to `false` to stop new sign-ups |
| `CHAT_LIMIT_PER_10_MIN` | `30` | Messages each user can send per 10 minutes |
| `MAX_MESSAGE_CHARS` | `8000` | Longest message allowed |
| `MAX_OUTPUT_TOKENS` | `4096` | Longest Claude reply (Gemini: `GEMINI_MAX_OUTPUT_TOKENS`, 8192) |

## Project structure

```
main.py             Web server: pages, accounts, chats and live streaming
ai_providers.py     Talks to Claude or Gemini (plus demo mode)
database.py         Stores users, sessions, chats and messages (SQLite or PostgreSQL)
security.py         Password hashing, session tokens, input checks, rate limits
config.py           Reads settings from .env
static/             The chat interface: HTML, CSS and JavaScript (no build step)
  markdown.js       Safe Markdown renderer and code highlighter for replies
tests/smoke_test.py End-to-end test of the whole API
Dockerfile          Container for cloud hosting
docs/               Screenshots and the architecture diagram
```

## API

| Method and path | Purpose |
|---|---|
| `POST /api/auth/signup` · `POST /api/auth/login` · `POST /api/auth/logout` | Accounts |
| `GET /api/me` · `PATCH /api/me` · `DELETE /api/me` | Profile, "About you", delete account |
| `GET /api/me/export` | Download all of the user's data as JSON |
| `GET /api/conversations` | List the user's chats |
| `GET / PATCH / DELETE /api/conversations/{id}` | Open, rename or delete a chat |
| `POST /api/chat` | Send a message (or `regenerate`) and stream the reply as Server-Sent Events |
| `GET /api/config` · `GET /healthz` | App info and health check for the cloud |

## Security and privacy

- **Passwords:** hashed with scrypt, never stored as text.
- **Sessions:** only a hash of each session token is kept in the database, so a leaked database can't be used to log in.
- **Cookies and requests:** login cookies are HttpOnly and SameSite. Requests from other websites are blocked, and a strict Content Security Policy stops injected scripts.
- **Private chats:** every chat lookup checks who owns it. Other users get "not found".
- **Rate limits:** logins, sign-ups and messages are limited to stop abuse and protect your API bill. The limits are kept in memory per server instance, so use Redis if you run many instances.
- **Data rights:** users can download or delete their data at any time.
- **Keys:** API keys stay on the server in `.env` or Secret Manager, never in the browser or in Git (`.env` is in `.gitignore`).

## Testing

```bash
python tests/smoke_test.py
```

This starts its own server in demo mode with a temporary database and runs 39 checks: sign-up and login, streaming, history, regenerate, Stop, privacy between users, data export and account deletion.

## Ideas to extend

- Sign in with Google (Firebase Authentication or OAuth)
- Upload images and PDFs: store them in Cloud Storage and send them to the model
- Answer questions from your own documents (RAG with `pgvector` in the same PostgreSQL)
- An admin page with usage statistics
#   N i m b u s - C h a t b o t  
 