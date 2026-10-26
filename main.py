"""Nimbus: a Gemini-style AI chatbot.

Gen AI (Claude or Gemini) writes the answers; a cloud database stores each user's
account and chat history. Run locally with:  uvicorn main:app --reload
"""
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import anyio
from starlette.applications import Starlette
from starlette.concurrency import iterate_in_threadpool, run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import ai_providers as ai
import config
import database as db
import security

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("nimbus")

STATIC_DIR = Path(__file__).parent / "static"
SESSION_COOKIE = "nimbus_session"
MAX_BODY_BYTES = 64_000
limiter = security.RateLimiter()


# --- Helpers ---------------------------------------------------------------------
def api(handler):
    """Reads the JSON body, then runs the handler in a worker thread so slow
    database or network calls never freeze the server."""

    async def endpoint(request):
        data = {}
        if request.method in ("POST", "PATCH", "PUT", "DELETE"):
            body = b""
            async for chunk in request.stream():
                body += chunk
                if len(body) > MAX_BODY_BYTES:
                    raise HTTPException(413, "Request is too large.")
            if body:
                try:
                    data = json.loads(body)
                except ValueError:
                    raise HTTPException(400, "Request body must be JSON.")
                if not isinstance(data, dict):
                    raise HTTPException(400, "Request body must be a JSON object.")
        return await run_in_threadpool(handler, request, data)

    return endpoint


def client_ip(request) -> str:
    return request.client.host if request.client else "unknown"


def public_user(user: dict) -> dict:
    return {"id": user["id"], "name": user["name"], "email": user["email"], "about": user["about"]}


def current_user(request) -> dict:
    token = request.cookies.get(SESSION_COOKIE)
    user = db.get_session_user(security.token_hash(token)) if token else None
    if not user:
        raise HTTPException(401, "Please log in.")
    return user


def owned_conversation(conversation_id, user) -> dict:
    conv = db.get_conversation(str(conversation_id), user["id"]) if conversation_id else None
    if not conv:
        raise HTTPException(404, "Chat not found.")
    return conv


def start_session(request, user: dict, status: int = 200):
    token = security.new_token()
    db.create_session(user["id"], security.token_hash(token), config.SESSION_DAYS)
    response = JSONResponse({"user": public_user(user)}, status_code=status)
    response.set_cookie(
        SESSION_COOKIE, token, max_age=config.SESSION_DAYS * 86400, httponly=True,
        samesite="lax", secure=request.url.scheme == "https", path="/",
    )
    return response


def make_title(text: str) -> str:
    title = " ".join(text.split())
    return title if len(title) <= 60 else title[:57].rstrip() + "…"


def sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


# --- Pages & config ----------------------------------------------------------------
async def index(request):
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


async def healthz(request):
    return PlainTextResponse("ok")


async def app_config(request):
    provider = ai.active_provider()
    return JSONResponse({
        "app_name": config.APP_NAME,
        "provider": provider,
        "model": ai.model_label(),
        "web_search": config.WEB_SEARCH and provider != "demo",
        "allow_signups": config.ALLOW_SIGNUPS,
        "max_message_chars": config.MAX_MESSAGE_CHARS,
    })


# --- Accounts ------------------------------------------------------------------------
@api
def signup(request, data):
    if not config.ALLOW_SIGNUPS:
        raise HTTPException(403, "New sign-ups are closed.")
    if not limiter.allow(f"signup:{client_ip(request)}", 10, 3600):
        raise HTTPException(429, "Too many sign-ups from your network. Please try again later.")
    name = security.clean_text(data.get("name"), 60)
    email = security.normalize_email(data.get("email"))
    password = data.get("password")
    if not name:
        raise HTTPException(400, "Please enter your name.")
    if len(email) > 254 or not security.EMAIL_RE.match(email):
        raise HTTPException(400, "Please enter a valid email address.")
    problem = security.password_problem(password)
    if problem:
        raise HTTPException(400, problem)
    if db.get_user_by_email(email):
        raise HTTPException(409, "An account with this email already exists. Try logging in.")
    user = db.create_user(name, email, security.hash_password(password))
    if not user:
        raise HTTPException(409, "An account with this email already exists. Try logging in.")
    log.info("New account created")
    return start_session(request, user, status=201)


@api
def login(request, data):
    email = security.normalize_email(data.get("email"))
    password = data.get("password") if isinstance(data.get("password"), str) else ""
    ip = client_ip(request)
    if not limiter.allow(f"login:{ip}", 50, 900) or not limiter.allow(f"login:{ip}:{email}", 8, 900):
        raise HTTPException(429, "Too many login attempts. Please wait 15 minutes and try again.")
    user = db.get_user_by_email(email) if email else None
    if not user:
        security.verify_password(password, security.DUMMY_HASH)  # same timing as a real check
        raise HTTPException(401, "Incorrect email or password.")
    if not security.verify_password(password, user["password_hash"]):
        raise HTTPException(401, "Incorrect email or password.")
    db.delete_expired_sessions()
    return start_session(request, user)


@api
def logout(request, data):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        db.delete_session(security.token_hash(token))
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@api
def me(request, data):
    if request.method == "GET":  # also used to ask "am I logged in?"
        try:
            return JSONResponse({"user": public_user(current_user(request))})
        except HTTPException:
            return JSONResponse({"user": None})
    user = current_user(request)
    if request.method == "PATCH":
        name = security.clean_text(data["name"], 60) if "name" in data else None
        if name is not None and not name:
            raise HTTPException(400, "Name can't be empty.")
        about = data.get("about") if "about" in data else None
        if about is not None:
            if not isinstance(about, str):
                raise HTTPException(400, "Invalid 'about' text.")
            about = about.strip()[:1500]
        return JSONResponse({"user": public_user(db.update_user(user["id"], name=name, about=about))})
    # DELETE = delete the account and all of its data
    if not security.verify_password(data.get("password") or "", user["password_hash"]):
        raise HTTPException(403, "Password is incorrect.")
    db.delete_user(user["id"])
    log.info("Account deleted")
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@api
def export_data(request, data):
    user = current_user(request)
    body = json.dumps(db.export_user(user["id"]), indent=2, ensure_ascii=False)
    return PlainTextResponse(body, media_type="application/json", headers={
        "Content-Disposition": f'attachment; filename="{config.APP_NAME.lower()}-my-data.json"',
    })


# --- Chats -----------------------------------------------------------------------------
@api
def conversations(request, data):
    user = current_user(request)
    return JSONResponse({"conversations": db.list_conversations(user["id"])})


@api
def conversation(request, data):
    user = current_user(request)
    conv = owned_conversation(request.path_params["conversation_id"], user)
    if request.method == "GET":
        conv["messages"] = db.get_messages(conv["id"])
        return JSONResponse({"conversation": conv})
    if request.method == "PATCH":
        title = security.clean_text(data.get("title"), 80)
        if not title:
            raise HTTPException(400, "Title can't be empty.")
        db.rename_conversation(conv["id"], user["id"], title)
        return JSONResponse({"ok": True, "title": title})
    db.delete_conversation(conv["id"], user["id"])
    return JSONResponse({"ok": True})


@api
def chat(request, data):
    """Saves the user's message, then streams the AI reply as Server-Sent Events."""
    user = current_user(request)
    if not limiter.allow(f"chat:{user['id']}", config.CHAT_LIMIT_PER_10_MIN, 600):
        raise HTTPException(429, "You're sending messages very fast. Please wait a few minutes.")
    regenerate = bool(data.get("regenerate"))
    conv = owned_conversation(data["conversation_id"], user) if data.get("conversation_id") else None

    if regenerate:
        if not conv:
            raise HTTPException(400, "Nothing to regenerate.")
        history = db.get_messages(conv["id"])
        if not history:
            raise HTTPException(400, "Nothing to regenerate.")
        if history[-1]["role"] == "assistant":
            db.delete_message(history[-1]["id"])
    else:
        text = data.get("message")
        text = text.strip() if isinstance(text, str) else ""
        if not text:
            raise HTTPException(400, "Type a message first.")
        if len(text) > config.MAX_MESSAGE_CHARS:
            raise HTTPException(400, f"Messages can be at most {config.MAX_MESSAGE_CHARS} characters.")
        if not conv:
            conv = db.create_conversation(user["id"], make_title(text))
        db.add_message(conv["id"], "user", text)

    messages = ai.build_history(db.get_messages(conv["id"]))
    system = ai.system_prompt(user)
    return StreamingResponse(
        close_when_done(stream_reply(conv, messages, system)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def close_when_done(generator):
    """Streams the reply and closes it the moment the user presses Stop or leaves.
    That cancels the AI request (no tokens wasted) and saves the partial reply."""
    try:
        async for chunk in iterate_in_threadpool(generator):
            yield chunk
    finally:
        with anyio.CancelScope(shield=True):
            await run_in_threadpool(generator.close)


def stream_reply(conv: dict, messages: list, system: str):
    yield sse({"type": "meta", "conversation_id": conv["id"], "title": conv["title"]})
    parts, meta, finished = [], {}, False
    try:
        for kind, value in ai.stream(messages, system):
            if kind == "text":
                parts.append(value)
                yield sse({"type": "delta", "text": value})
            elif kind == "status":
                yield sse({"type": "status", "text": value})
            elif kind == "sources":
                meta["sources"] = value.get("links", [])
                if value.get("search_widget"):
                    meta["search_widget"] = value["search_widget"]
                yield sse({"type": "sources", **meta})
        finished = True
        yield sse({"type": "done"})
    except ai.ProviderError as exc:
        finished = True
        yield sse({"type": "error", "message": str(exc)})
    except Exception:
        finished = True
        log.exception("AI reply failed")
        yield sse({"type": "error", "message": "Something went wrong while writing the reply. Please try again."})
    finally:
        # Runs even if the user pressed Stop, so partial replies are kept too.
        reply = "".join(parts).strip()
        if reply:
            if not finished:
                meta["stopped"] = True
            db.add_message(conv["id"], "assistant", reply, meta)
        db.touch_conversation(conv["id"])


# --- Errors, security headers and startup --------------------------------------------------
async def http_error(request, exc):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))


async def server_error(request, exc):
    log.exception("Unhandled error", exc_info=exc)
    return JSONResponse({"error": "Internal server error."}, status_code=500)


CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self'; frame-src 'self'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'; object-src 'none'"
)


class SecurityMiddleware:
    """Blocks cross-site form/API posts and adds protective browser headers."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        if scope["method"] not in ("GET", "HEAD", "OPTIONS"):
            origin = headers.get("origin")
            hosts = {headers.get("host"), headers.get("x-forwarded-host")}
            if origin and urlparse(origin).netloc not in hosts:
                response = JSONResponse({"error": "Cross-site request blocked."}, status_code=403)
                return await response(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                h = MutableHeaders(scope=message)
                h.setdefault("X-Content-Type-Options", "nosniff")
                h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
                h.setdefault("X-Frame-Options", "DENY")
                h.setdefault("Content-Security-Policy", CSP)
            await send(message)

        await self.app(scope, receive, send_with_headers)


@asynccontextmanager
async def lifespan(app):
    await run_in_threadpool(db.init_db)
    if not db.IS_POSTGRES and os.getenv("K_SERVICE"):
        log.warning("Running on Cloud Run with SQLite: data is lost on restart. Set DATABASE_URL to PostgreSQL.")
    log.info("AI model: %s | web search: %s", ai.model_label(), "on" if config.WEB_SEARCH else "off")
    yield
    db.close()


routes = [
    Route("/", index),
    Route("/healthz", healthz),
    Route("/api/config", app_config),
    Route("/api/auth/signup", signup, methods=["POST"]),
    Route("/api/auth/login", login, methods=["POST"]),
    Route("/api/auth/logout", logout, methods=["POST"]),
    Route("/api/me", me, methods=["GET", "PATCH", "DELETE"]),
    Route("/api/me/export", export_data),
    Route("/api/conversations", conversations),
    Route("/api/conversations/{conversation_id}", conversation, methods=["GET", "PATCH", "DELETE"]),
    Route("/api/chat", chat, methods=["POST"]),
    Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
]

app = Starlette(
    routes=routes,
    middleware=[Middleware(SecurityMiddleware)],
    exception_handlers={HTTPException: http_error, 500: server_error},
    lifespan=lifespan,
)
