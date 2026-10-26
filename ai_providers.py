"""Gets replies from the Gen AI model and streams them back piece by piece.

Supports Claude (Anthropic) and Gemini (Google) through their REST APIs, plus a demo
mode that needs no key. Each stream yields (kind, value) pairs:
    ("text", "...")        a piece of the reply
    ("status", "...")      progress note, e.g. "Searching the web..."
    ("sources", {...})     web sources used for the answer
"""
import codecs
import json
import re
import time
from datetime import date

import requests

import config


class ProviderError(Exception):
    """A problem we can show to the user as-is (bad key, quota, model name...)."""


BLOCKED_REASONS = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}
CUT_OFF_NOTE = "\n\n*(The reply was cut off because it reached the length limit.)*"


def active_provider() -> str:
    if config.AI_PROVIDER == "auto":
        if config.ANTHROPIC_API_KEY:
            return "claude"
        if config.GEMINI_API_KEY:
            return "gemini"
        return "demo"
    return config.AI_PROVIDER


def model_label() -> str:
    """Human-friendly model name for the UI, e.g. 'Claude Sonnet 5.5' or 'Gemini 3.8 Flash'."""
    provider = active_provider()
    model = {"claude": config.CLAUDE_MODEL, "gemini": config.GEMINI_MODEL}.get(provider)
    if not model:
        return "Demo mode"
    words, numbers = [], []
    for part in model.split("-"):
        if re.fullmatch(r"[\d.]+", part):
            numbers.append(part)
            continue
        if numbers:
            words.append(".".join(numbers))
            numbers = []
        words.append(part.capitalize())
    if numbers:
        words.append(".".join(numbers))
    return " ".join(words)


def system_prompt(user: dict) -> str:
    lines = [
        f"You are {config.APP_NAME}, a helpful, friendly and knowledgeable AI assistant.",
        "Give accurate, well-organized answers. Use Markdown (headings, lists, tables and code "
        "blocks with a language tag) when it makes an answer easier to read, and keep simple "
        "answers short. If you are not sure about something, say so instead of guessing.",
        f"Today's date is {date.today():%A, %d %B %Y}.",
        f"The user's name is {user['name']}.",
    ]
    if user.get("about"):
        lines.append(
            "The user shared this about themselves. Use it to personalise answers when relevant:\n"
            + user["about"]
        )
    if config.EXTRA_INSTRUCTIONS:
        lines.append(config.EXTRA_INSTRUCTIONS)
    return "\n".join(lines)


def build_history(rows: list[dict]) -> list[dict]:
    """Stored messages -> model format. Merges repeated roles, skips empty
    messages and keeps only the newest messages that fit the size budget."""
    merged = []
    for row in rows:
        content = (row["content"] or "").strip()
        if not content:
            continue
        if merged and merged[-1]["role"] == row["role"]:
            merged[-1]["content"] += "\n\n" + content
        else:
            merged.append({"role": row["role"], "content": content})
    kept, total = [], 0
    for message in reversed(merged):
        total += len(message["content"])
        if kept and (total > config.HISTORY_CHAR_BUDGET or len(kept) >= config.HISTORY_MAX_MESSAGES):
            break
        kept.append(message)
    kept.reverse()
    while kept and kept[0]["role"] != "user":
        kept.pop(0)
    return kept


def stream(messages: list[dict], system: str):
    provider = active_provider()
    if provider == "claude":
        yield from _claude(messages, system)
    elif provider == "gemini":
        yield from _gemini(messages, system)
    elif provider == "demo":
        yield from _demo(messages)
    else:
        raise ProviderError(f"Unknown AI_PROVIDER '{provider}'. Use auto, claude, gemini or demo.")


# --- HTTP helpers ------------------------------------------------------------------
def _post(url: str, headers: dict, body: dict, who: str):
    try:
        resp = requests.post(url, headers=headers, json=body, stream=True, timeout=(15, 180))
    except requests.RequestException as exc:
        raise ProviderError(
            f"Could not reach the {who} API. Check the server's internet connection. ({type(exc).__name__})"
        ) from exc
    if resp.status_code != 200:
        message = _error_message(resp)
        resp.close()
        hint = {
            401: " Check that your API key is correct.",
            403: " Your key may not have access to this model or feature.",
            404: " Check the model name in your settings.",
            429: " You've hit a rate limit or used up your quota. Wait a little or check your plan.",
        }.get(resp.status_code, "")
        if not hint and "api key" in message.lower():
            hint = " Check that your API key is correct."
        raise ProviderError(f"{who} API error {resp.status_code}: {message}{hint}")
    return resp


def _error_message(resp) -> str:
    try:
        data = resp.json()
        if isinstance(data, list) and data:
            data = data[0]
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:300]
    except ValueError:
        pass
    return (resp.text or resp.reason or "unknown error").strip()[:300]


def _sse(resp):
    """Parse a Server-Sent Events stream into (event, data-as-dict) pairs as data arrives."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    buffer, event, data_lines = "", None, []
    for chunk in resp.iter_content(chunk_size=None):
        buffer += decoder.decode(chunk)
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.rstrip("\r")
            if line == "":
                if data_lines:
                    try:
                        yield event, json.loads("\n".join(data_lines))
                    except json.JSONDecodeError:
                        pass
                event, data_lines = None, []
            elif line.startswith(":"):
                continue
            else:
                field, _, value = line.partition(":")
                value = value[1:] if value.startswith(" ") else value
                if field == "event":
                    event = value
                elif field == "data":
                    data_lines.append(value)
    if data_lines:
        try:
            yield event, json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            pass


# --- Claude (Anthropic Messages API) -------------------------------------------------
def _claude(messages, system):
    if not config.ANTHROPIC_API_KEY:
        raise ProviderError("ANTHROPIC_API_KEY is not set. Add it to your .env file (get a key at console.anthropic.com).")
    body = {
        "model": config.CLAUDE_MODEL,
        "max_tokens": config.MAX_OUTPUT_TOKENS,
        "system": system,
        "messages": messages,
        "stream": True,
    }
    if config.WEB_SEARCH:
        body["tools"] = [{"type": config.CLAUDE_WEB_SEARCH_TOOL, "name": "web_search", "max_uses": 3}]
    headers = {
        "x-api-key": config.ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    resp = _post(f"{config.ANTHROPIC_BASE_URL}/v1/messages", headers, body, "Claude")
    cited, found = {}, {}
    wrote_text = tool_since_text = False
    with resp:
        for event, data in _sse(resp):
            kind = data.get("type") or event
            if kind == "content_block_start":
                block = data.get("content_block") or {}
                if block.get("type") == "text":
                    if wrote_text and tool_since_text:
                        yield ("text", "\n\n")
                    tool_since_text = False
                    if block.get("text"):
                        wrote_text = True
                        yield ("text", block["text"])
                elif block.get("type") == "server_tool_use":
                    tool_since_text = True
                    yield ("status", "Searching the web…")
                elif block.get("type") == "web_search_tool_result":
                    tool_since_text = True
                    results = block.get("content")
                    if isinstance(results, list):  # a dict here means the search failed
                        for result in results:
                            if result.get("url"):
                                found.setdefault(result["url"], result.get("title") or result["url"])
            elif kind == "content_block_delta":
                delta = data.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    wrote_text = True
                    yield ("text", delta["text"])
                elif delta.get("type") == "citations_delta":
                    citation = delta.get("citation") or {}
                    if citation.get("url"):
                        cited.setdefault(citation["url"], citation.get("title") or citation["url"])
            elif kind == "message_delta":
                if (data.get("delta") or {}).get("stop_reason") == "max_tokens":
                    yield ("text", CUT_OFF_NOTE)
            elif kind == "error":
                error = data.get("error") or {}
                raise ProviderError(f"Claude API error: {error.get('message') or error.get('type') or 'unknown error'}")
    links = cited or dict(list(found.items())[:5])
    if links:
        yield ("sources", {"links": [{"url": u, "title": t} for u, t in links.items()]})


# --- Gemini (Google Gemini API) -------------------------------------------------------
def _gemini(messages, system):
    if not config.GEMINI_API_KEY:
        raise ProviderError("GEMINI_API_KEY is not set. Add it to your .env file (get a key at aistudio.google.com).")
    body = {
        "contents": [
            {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
            for m in messages
        ],
        "systemInstruction": {"parts": [{"text": system}]},
        "generationConfig": {"maxOutputTokens": config.GEMINI_MAX_OUTPUT_TOKENS},
    }
    if config.WEB_SEARCH:
        body["tools"] = [{"google_search": {}}]
    url = f"{config.GEMINI_BASE_URL}/v1beta/models/{config.GEMINI_MODEL}:streamGenerateContent?alt=sse"
    headers = {"x-goog-api-key": config.GEMINI_API_KEY, "content-type": "application/json"}
    resp = _post(url, headers, body, "Gemini")
    links, widget, got_text, finish = {}, None, False, None
    with resp:
        for _, data in _sse(resp):
            if isinstance(data.get("error"), dict):
                raise ProviderError(f"Gemini API error: {data['error'].get('message', 'unknown error')}")
            blocked = (data.get("promptFeedback") or {}).get("blockReason")
            if blocked:
                raise ProviderError(f"Gemini declined this request ({blocked}). Try rephrasing it.")
            for candidate in (data.get("candidates") or [])[:1]:
                for part in (candidate.get("content") or {}).get("parts") or []:
                    if part.get("text") and not part.get("thought"):
                        got_text = True
                        yield ("text", part["text"])
                grounding = candidate.get("groundingMetadata") or {}
                for chunk in grounding.get("groundingChunks") or []:
                    web = chunk.get("web") or {}
                    if web.get("uri"):
                        links.setdefault(web["uri"], web.get("title") or web["uri"])
                widget = (grounding.get("searchEntryPoint") or {}).get("renderedContent") or widget
                finish = candidate.get("finishReason") or finish
    if not got_text:
        if finish in BLOCKED_REASONS:
            raise ProviderError(f"Gemini stopped this reply for safety reasons ({finish}). Try rephrasing your message.")
        raise ProviderError("Gemini returned an empty reply. Please try again.")
    if finish == "MAX_TOKENS":
        yield ("text", CUT_OFF_NOTE)
    if links or widget:
        # Google's terms ask apps to show the "Search suggestions" widget with grounded answers.
        yield ("sources", {"links": [{"url": u, "title": t} for u, t in links.items()], "search_widget": widget})


# --- Demo mode (no API key needed) ------------------------------------------------------
DEMO_REPLY = """**Demo mode:** no AI key is set yet, so this is a sample reply rather than a real answer.

You asked: *{question}*

To get real answers, add **one** key to your `.env` file and restart the server:

| AI model | Setting in `.env` | Get a key at |
|---|---|---|
| Claude | `ANTHROPIC_API_KEY` | console.anthropic.com |
| Gemini | `GEMINI_API_KEY` | aistudio.google.com |

Everything else already works: your account, this chat and its history are saved in the database. Code is formatted like this:

```python
def greet(name):
    return f"Hello, {{name}}! Your chatbot is running."
```"""


def _demo(messages):
    question = " ".join((messages[-1]["content"] if messages else "").split())
    question = question[:160].replace("*", "").replace("`", "") or "(empty)"
    text = DEMO_REPLY.format(question=question)
    for piece in re.findall(r"\s*\S+", text):
        time.sleep(config.DEMO_DELAY)
        yield ("text", piece)
