import hashlib
import hmac
import json
import logging
import os
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Header, HTTPException, Request


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


DEFAULT_FEISHU_BASE_URL = "https://open.feishu.cn"
FEISHU_BASE_URL = os.getenv("FEISHU_BASE_URL", DEFAULT_FEISHU_BASE_URL)
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
FEISHU_VERIFICATION_TOKEN = os.getenv("FEISHU_VERIFICATION_TOKEN", "")
FEISHU_SIGNING_SECRET = os.getenv("FEISHU_SIGNING_SECRET", "")
FEISHU_RAG_API_BASE = os.getenv("FEISHU_RAG_API_BASE", "http://rag-api:8000")
FEISHU_RAG_MODEL = os.getenv("FEISHU_RAG_MODEL", "rag-model")
FEISHU_RAG_API_KEY = os.getenv("FEISHU_RAG_API_KEY", "feishu-bot")
FEISHU_REPLY_PREFIX = os.getenv("FEISHU_REPLY_PREFIX", "")
FEISHU_ALLOWED_CHAT_TYPES = {
    item.strip().lower()
    for item in os.getenv("FEISHU_ALLOWED_CHAT_TYPES", "p2p").split(",")
    if item.strip()
}
FEISHU_MESSAGE_MAX_CHARS = int(os.getenv("FEISHU_MESSAGE_MAX_CHARS", "4000"))
FEISHU_HTTP_TIMEOUT_SECONDS = float(os.getenv("FEISHU_HTTP_TIMEOUT_SECONDS", "120"))
DEBUG = env_bool("DEBUG", False)

logger = logging.getLogger("feishu-bot")
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)

app = FastAPI(title="rag-feishu-bot", version="0.1.0")

token_cache_lock = threading.Lock()
tenant_access_token: str | None = None
tenant_access_token_expires_at = 0.0

dedupe_lock = threading.Lock()
processed_events: dict[str, float] = {}
PROCESSED_EVENT_TTL_SECONDS = 900


def cleanup_processed_events(now: float) -> None:
    expired = [event_id for event_id, expires_at in processed_events.items() if expires_at <= now]
    for event_id in expired:
        processed_events.pop(event_id, None)


def mark_event_processed(event_id: str) -> bool:
    if not event_id:
        return False
    now = time.time()
    with dedupe_lock:
        cleanup_processed_events(now)
        if event_id in processed_events:
            return True
        processed_events[event_id] = now + PROCESSED_EVENT_TTL_SECONDS
        return False


def verify_signature(timestamp: str, nonce: str, body: bytes, signature: str | None) -> None:
    if not FEISHU_SIGNING_SECRET:
        return
    if not signature:
        raise HTTPException(status_code=401, detail="missing Feishu signature")
    payload = b"".join([timestamp.encode("utf-8"), nonce.encode("utf-8"), body])
    expected = base16_hmac_sha256(FEISHU_SIGNING_SECRET.encode("utf-8"), payload)
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="invalid Feishu signature")


def base16_hmac_sha256(secret: bytes, payload: bytes) -> str:
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def verify_token(payload: dict[str, Any]) -> None:
    if not FEISHU_VERIFICATION_TOKEN:
        return
    token = payload.get("token")
    if token != FEISHU_VERIFICATION_TOKEN:
        raise HTTPException(status_code=401, detail="invalid Feishu verification token")


async def get_tenant_access_token() -> str:
    global tenant_access_token
    global tenant_access_token_expires_at

    now = time.time()
    with token_cache_lock:
        if tenant_access_token and now < tenant_access_token_expires_at - 60:
            return tenant_access_token

    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        raise RuntimeError("FEISHU_APP_ID and FEISHU_APP_SECRET are required")

    last_error = None
    payload = None
    for base_url in candidate_feishu_base_urls():
        try:
            async with httpx.AsyncClient(timeout=FEISHU_HTTP_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    build_feishu_api_url(base_url, "/open-apis/auth/v3/tenant_access_token/internal"),
                    json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
                )
                response.raise_for_status()
                payload = response.json()
                break
        except httpx.ConnectError as exc:
            last_error = exc
            logger.warning("failed to connect to Feishu auth endpoint host=%s", base_url_host(base_url))

    if payload is None:
        raise RuntimeError("failed to connect to Feishu auth endpoint") from last_error

    if payload.get("code") != 0:
        raise RuntimeError(f"failed to get tenant access token: {payload}")

    token = payload["tenant_access_token"]
    expire = int(payload.get("expire", 7200))
    with token_cache_lock:
        tenant_access_token = token
        tenant_access_token_expires_at = time.time() + expire
        return token


def parse_feishu_text_content(content: str) -> str:
    if not content:
        return ""
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return content.strip()
    if isinstance(payload, dict):
        return str(payload.get("text", "")).strip()
    return content.strip()


def normalize_base_url(url: str, default: str) -> str:
    value = (url or "").strip().strip('"').strip("'")
    if not value:
        return default
    if "://" not in value:
        value = f"https://{value}"
    return value.rstrip("/")


def candidate_feishu_base_urls() -> list[str]:
    bases = []
    configured = normalize_base_url(FEISHU_BASE_URL, DEFAULT_FEISHU_BASE_URL)
    for value in [configured, DEFAULT_FEISHU_BASE_URL]:
        if value not in bases:
            bases.append(value)
    return bases


def build_feishu_api_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def base_url_host(url: str) -> str:
    return urlsplit(url).netloc


def build_text_message(text: str) -> str:
    message = text.strip()
    if FEISHU_REPLY_PREFIX:
        message = f"{FEISHU_REPLY_PREFIX}{message}"
    if len(message) > FEISHU_MESSAGE_MAX_CHARS:
        message = f"{message[: FEISHU_MESSAGE_MAX_CHARS - 12].rstrip()}\n\n[已截断]"
    return json.dumps({"text": message}, ensure_ascii=False)


async def ask_rag(question: str) -> str:
    payload = {
        "model": FEISHU_RAG_MODEL,
        "messages": [{"role": "user", "content": question}],
        "temperature": 0.2,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {FEISHU_RAG_API_KEY}"}
    async with httpx.AsyncClient(timeout=FEISHU_HTTP_TIMEOUT_SECONDS) as client:
        response = await client.post(
            f"{FEISHU_RAG_API_BASE}/v1/chat/completions",
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
        data = response.json()

    try:
        answer = data["choices"][0]["message"]["content"]
    except Exception as exc:
        raise RuntimeError(f"unexpected rag response: {data}") from exc
    return str(answer).strip()


async def send_feishu_message(chat_id: str, text: str) -> None:
    token = await get_tenant_access_token()
    payload = {
        "receive_id": chat_id,
        "msg_type": "text",
        "content": build_text_message(text),
    }
    headers = {"Authorization": f"Bearer {token}"}
    last_error = None
    data = None
    for base_url in candidate_feishu_base_urls():
        try:
            async with httpx.AsyncClient(timeout=FEISHU_HTTP_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    build_feishu_api_url(base_url, "/open-apis/im/v1/messages?receive_id_type=chat_id"),
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()
                break
        except httpx.ConnectError as exc:
            last_error = exc
            logger.warning(
                "failed to connect to Feishu message endpoint host=%s chat_id=%s",
                base_url_host(base_url),
                chat_id,
            )

    if data is None:
        raise RuntimeError("failed to connect to Feishu message endpoint") from last_error
    if data.get("code") != 0:
        raise RuntimeError(f"failed to send Feishu message: {data}")


async def handle_message_event(event: dict[str, Any]) -> None:
    sender = event.get("sender", {})
    message = event.get("message", {})
    chat_type = str(message.get("chat_type", "")).lower()

    if FEISHU_ALLOWED_CHAT_TYPES and chat_type not in FEISHU_ALLOWED_CHAT_TYPES:
        logger.info("ignored unsupported chat type", extra={"chat_type": chat_type})
        return

    if message.get("message_type") != "text":
        chat_id = message.get("chat_id")
        if chat_id:
            await send_feishu_message(chat_id, "当前只支持文本消息提问。")
        return

    sender_type = str(sender.get("sender_type", "")).lower()
    if sender_type == "app":
        return

    content = parse_feishu_text_content(str(message.get("content", "")))
    if not content:
        return

    chat_id = str(message.get("chat_id", "")).strip()
    if not chat_id:
        logger.warning("missing chat_id in Feishu event")
        return

    logger.info(
        "processing Feishu message",
        extra={
            "chat_id": chat_id,
            "message_id": message.get("message_id"),
            "chat_type": chat_type,
        },
    )

    try:
        answer = await ask_rag(content)
    except Exception:
        logger.exception("failed to query rag-api")
        await send_feishu_message(chat_id, "查询知识库失败，请稍后重试。")
        return

    if not answer:
        answer = "没有检索到可用内容。"

    try:
        await send_feishu_message(chat_id, answer)
    except Exception:
        logger.exception("failed to send Feishu reply")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/feishu/events")
async def feishu_events(
    request: Request,
    x_lark_request_timestamp: str = Header(default="", alias="X-Lark-Request-Timestamp"),
    x_lark_request_nonce: str = Header(default="", alias="X-Lark-Request-Nonce"),
    x_lark_signature: str | None = Header(default=None, alias="X-Lark-Signature"),
) -> dict[str, Any]:
    body = await request.body()
    verify_signature(x_lark_request_timestamp, x_lark_request_nonce, body, x_lark_signature)

    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid json body") from exc

    verify_token(payload)

    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge", "")}

    header = payload.get("header", {})
    event = payload.get("event", {})
    event_id = str(header.get("event_id", "")).strip()
    if mark_event_processed(event_id):
        return {"code": 0}

    event_type = str(header.get("event_type", "")).strip()
    if event_type == "im.message.receive_v1":
        await handle_message_event(event)

    return {"code": 0}
