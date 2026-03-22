# llm_router.py
"""
Bedrock LLM router for 3 models:
  - Amazon Nova Lite  (fast/high-RPM)
  - Claude 3.5 Sonnet (strongest)
  - Claude 3.5 Haiku  (mid-tier)

Features:
- Per-model RPM shaping (token-bucket)
- Exponential backoff with jitter on throttling/5xx/timeouts
- 5-minute in-memory cache for identical requests
- Converse-first; falls back to invoke_model
- Task-based routing helper: choose_models(task)

Usage:
    from llm_router import generate_text, choose_models
    out = generate_text("Rewrite this in one sentence.",
                        prefer=choose_models("rewrite"),
                        max_tokens=128, temperature=0.2)
    print(out["model_id"], out["text"])
"""

from __future__ import annotations

import os
import json
import time
import random
import hashlib
import threading
from typing import Optional, Dict, Any, List

import boto3
import botocore
from botocore.config import Config
from botocore.exceptions import ClientError

# --------------------------
# Model IDs (override via env)
# --------------------------
REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")

# Defaults; override if your console shows different IDs
NOVA_LITE_ID     = os.getenv("NOVA_LITE_ID",     "amazon.nova-lite-v1")
CLAUDE_SONNET_ID = os.getenv("CLAUDE_SONNET_ID", "anthropic.claude-3-5-sonnet-20240620-v1:0")
CLAUDE_HAIKU_ID  = os.getenv("CLAUDE_HAIKU_ID",  "anthropic.claude-3-5-haiku-20241022-v1:0")

# Per-model RPM caps (set these to your actual Service Quotas)
RPM_LIMITS: Dict[str, int] = {
    NOVA_LITE_ID: 60,      # adjust to your RPM
    CLAUDE_HAIKU_ID: 10,   # example
    CLAUDE_SONNET_ID: 4,   # new accounts are often low here
}

# Default fallback order (cheap/fast → strong)
FALLBACK_ORDER: List[str] = [NOVA_LITE_ID, CLAUDE_HAIKU_ID, CLAUDE_SONNET_ID]

# --------------------------
# Task-based routing helper
# --------------------------
def choose_models(task: str) -> List[str]:
    """
    Return a preferred model order based on a simple task hint.
    Examples: "extract", "rewrite", "summarize", "plan", "complex", "reasoning"
    """
    t = (task or "").lower()

    if any(k in t for k in [
        "extract","classify","rewrite","short","summarize","caption","bullet",
        "parse","tag","json","regex","format"
    ]):
        return [NOVA_LITE_ID, CLAUDE_HAIKU_ID, CLAUDE_SONNET_ID]

    if any(k in t for k in [
        "plan","tool","multi-step","complex","reasoning","agent","synthesize"
    ]):
        return [CLAUDE_SONNET_ID, CLAUDE_HAIKU_ID, NOVA_LITE_ID]

    if any(k in t for k in ["creative","long","compose","caregiver","empathetic","nuanced"]):
        return [CLAUDE_SONNET_ID, NOVA_LITE_ID, CLAUDE_HAIKU_ID]

    return [NOVA_LITE_ID, CLAUDE_HAIKU_ID, CLAUDE_SONNET_ID]

# -----------------------
# Bedrock client + low-level retry
# -----------------------
bedrock = boto3.client(
    "bedrock-runtime",
    region_name=REGION,
    config=Config(
        retries={"max_attempts": 10, "mode": "adaptive"},
        connect_timeout=5,
        read_timeout=20,
    ),
)

# broaden what we consider retryable
_THROTTLE_CODES = {
    "ThrottlingException",
    "Throttling",
    "TooManyRequestsException",
    "TooManyRequests",
}
_RETRY_STATUS = {429, 500, 502, 503, 504}
_RETRY_EXC = (
    botocore.exceptions.EndpointConnectionError,
    botocore.exceptions.ConnectionClosedError,
    botocore.exceptions.ReadTimeoutError,
)

def _invoke_with_retry(fn, *args, **kwargs):
    backoff = 0.5
    for attempt in range(6):  # ~0.5 + 1 + 2 + 4 + 8 + 16
        try:
            return fn(*args, **kwargs)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
            if code in _THROTTLE_CODES or status in _RETRY_STATUS:
                time.sleep(backoff + random.random() * 0.3)
                backoff = min(backoff * 2, 8)
                continue
            raise
        except _RETRY_EXC:
            time.sleep(backoff + random.random() * 0.3)
            backoff = min(backoff * 2, 8)
            continue
    raise RuntimeError("Bedrock throttled or timed out repeatedly")

# ---------------------------
# Simple in-memory TTL cache
# ---------------------------
_CACHE: Dict[str, Dict[str, Any]] = {}
_CACHE_TTL_SECONDS = 300  # 5 minutes
_CACHE_LOCK = threading.Lock()

def _cache_key(model_id: str, body: Dict[str, Any]) -> str:
    h = hashlib.sha256()
    h.update(model_id.encode())
    h.update(json.dumps(body, sort_keys=True).encode())
    return h.hexdigest()

def cache_get(model_id: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    now = time.time()
    k = _cache_key(model_id, body)
    with _CACHE_LOCK:
        v = _CACHE.get(k)
        if v and now - v["ts"] < _CACHE_TTL_SECONDS:
            return v["resp"]
        return None

def cache_set(model_id: str, body: Dict[str, Any], resp: Dict[str, Any]) -> None:
    k = _cache_key(model_id, body)
    with _CACHE_LOCK:
        _CACHE[k] = {"ts": time.time(), "resp": resp}

# --------------------------
# Per-model RPM rate limiter
# --------------------------
class RPMLimiter:
    def __init__(self, rpm: int):
        self.rpm = max(1, int(rpm))
        self.allowance = float(self.rpm)
        self.last_check = time.time()
        self.lock = threading.Lock()

    def acquire(self):
        with self.lock:
            now = time.time()
            elapsed = now - self.last_check
            self.last_check = now
            self.allowance = min(self.rpm, self.allowance + elapsed * (self.rpm / 60.0))
            if self.allowance < 1.0:
                needed = (1.0 - self.allowance) / (self.rpm / 60.0)
                time.sleep(max(0.0, needed))
                self.allowance = 0.0
            else:
                self.allowance -= 1.0

_LIMITERS = {mid: RPMLimiter(rpm) for mid, rpm in RPM_LIMITS.items()}

# -----------------------------------
# Converse first, then invoke_model
# -----------------------------------
def _converse_once(model_id: str, prompt: str, max_tokens: int, temperature: float,
                   system_prompt: Optional[str]) -> Dict[str, Any]:
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": [{"text": system_prompt}]})
    messages.append({"role": "user", "content": [{"text": prompt}]})
    params: Dict[str, Any] = {
        "modelId": model_id,
        "messages": messages,
        "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature},
    }
    # <- now wrapped with retry
    return _invoke_with_retry(bedrock.converse, **params)

def _invoke_once(model_id: str, prompt: str, max_tokens: int, temperature: float,
                 system_prompt: Optional[str]) -> Dict[str, Any]:
    mid = model_id.lower()

    if "anthropic" in mid:
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [
                *(([{"role": "system", "content": [{"type": "text", "text": system_prompt}]}]
                   ) if system_prompt else []),
                {"role": "user", "content": [{"type": "text", "text": prompt}]},
            ],
        }
    elif "amazon.nova" in mid or "amazon.titan" in mid:
        body = {
            "inputText": prompt if not system_prompt else f"{system_prompt}\n\n{prompt}",
            "textGenerationConfig": {
                "maxTokenCount": max_tokens,
                "temperature": temperature,
                "topP": 0.9,
            },
        }
    else:
        # Generic fallback
        body = {
            "inputText": prompt,
            "textGenerationConfig": {"maxTokenCount": max_tokens, "temperature": temperature},
        }

    resp = _invoke_with_retry(bedrock.invoke_model, modelId=model_id, body=json.dumps(body))
    payload = json.loads(resp["body"].read().decode("utf-8"))
    return payload

def _extract_text(payload: Dict[str, Any]) -> str:
    # Converse
    if "output" in payload and isinstance(payload["output"], dict):
        msg = payload["output"].get("message") or {}
        content = msg.get("content") or []
        texts = [c.get("text") for c in content if isinstance(c, dict) and "text" in c]
        if texts:
            return "\n".join(t for t in texts if t)

    # Anthropic (invoke)
    if isinstance(payload.get("content"), list):
        texts = [c.get("text") for c in payload["content"] if isinstance(c, dict) and "text" in c]
        if texts:
            return "\n".join(t for t in texts if t)

    # Titan/Nova (invoke)
    if "outputText" in payload:
        return str(payload["outputText"])
    if isinstance(payload.get("results"), list) and payload["results"]:
        ot = payload["results"][0].get("outputText")
        if ot:
            return str(ot)

    # Generic fallback
    if "result" in payload and isinstance(payload["result"], str):
        return payload["result"]

    return json.dumps(payload)

def _limited_call(fn, limiter: RPMLimiter, attempt_backoffs: int = 6, **kwargs) -> Dict[str, Any]:
    """
    Rate-limit + retry on throttling/5xx/timeouts around our higher-level call.
    """
    attempt = 0
    while True:
        limiter.acquire()
        try:
            return fn(**kwargs)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
            if code in _THROTTLE_CODES or status in _RETRY_STATUS:
                attempt += 1
                if attempt > attempt_backoffs:
                    raise
                # full jitter
                sleep_s = min(8.0, (0.5 * (2 ** attempt)))
                time.sleep(random.uniform(0, sleep_s))
                continue
            raise
        except _RETRY_EXC:
            attempt += 1
            if attempt > attempt_backoffs:
                raise
            sleep_s = min(8.0, (0.5 * (2 ** attempt)))
            time.sleep(random.uniform(0, sleep_s))
            continue

def call_bedrock(model_id: str, prompt: str, max_tokens: int = 300, temperature: float = 0.3,
                 system_prompt: Optional[str] = None, use_cache: bool = True) -> Dict[str, Any]:
    """
    Returns: { "model_id": str, "raw": dict, "text": str }
    """
    limiter = _LIMITERS.get(model_id) or RPMLimiter(2)

    cache_body = {
        "k": "auto",
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system_prompt or "",
    }
    if use_cache:
        c = cache_get(model_id, cache_body)
        if c is not None:
            return {"model_id": model_id, "raw": c, "text": _extract_text(c)}

    # Converse path (preferred)
    try:
        raw = _limited_call(
            _converse_once,
            limiter,
            model_id=model_id,
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            system_prompt=system_prompt,
        )
        if use_cache:
            cache_set(model_id, cache_body, raw)
        return {"model_id": model_id, "raw": raw, "text": _extract_text(raw)}
    except Exception:
        # fall back to invoke_model
        pass

    raw = _limited_call(
        _invoke_once,
        limiter,
        model_id=model_id,
        prompt=prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        system_prompt=system_prompt,
    )
    if use_cache:
        cache_set(model_id, cache_body, raw)
    return {"model_id": model_id, "raw": raw, "text": _extract_text(raw)}

def generate_text(prompt: str,
                  max_tokens: int = 300,
                  temperature: float = 0.3,
                  prefer: Optional[List[str]] = None,
                  system_prompt: Optional[str] = None,
                  use_cache: bool = True) -> Dict[str, Any]:
    """
    Try each model in 'prefer' (or FALLBACK_ORDER) until one succeeds.
    Returns: { "model_id": str, "text": str, "raw": dict }
    """
    order = prefer or FALLBACK_ORDER
    last_err: Optional[Exception] = None
    for mid in order:
        try:
            return call_bedrock(mid, prompt, max_tokens, temperature, system_prompt, use_cache)
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"All models failed; last error: {last_err}")

if __name__ == "__main__":
    out = generate_text(
        "Say hi in exactly eight words.",
        max_tokens=64,
        temperature=0.2,
        prefer=choose_models("rewrite"),
    )
    print("Model used:", out["model_id"])
    print("Text:", out["text"])
