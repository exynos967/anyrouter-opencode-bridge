import httpx
import json
import sys
import os
import traceback
import argparse
import asyncio
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
import uvicorn

CONFIG_FILE = 'proxy_config.json'

DEFAULT_CONFIG = {
    "api_key": "",
    "proxy_url": "http://127.0.0.1:2080",
    "use_proxy": True,
    "debug": False,
    "target_base_url": "https://anyrouter.top/v1"
}

config = {}
CLIENT = None
CLAUDE_CODE_TOOLS = []
CLAUDE_CODE_SYSTEM = []

def load_claude_code_templates():
    global CLAUDE_CODE_TOOLS, CLAUDE_CODE_SYSTEM
    tools_file = os.path.join(os.path.dirname(__file__), 'claude_code_tools.json')
    system_file = os.path.join(os.path.dirname(__file__), 'claude_code_system.json')
    if os.path.exists(tools_file):
        try:
            with open(tools_file, 'r', encoding='utf-8') as f:
                CLAUDE_CODE_TOOLS = json.load(f)
            print(f"[SYSTEM] Loaded {len(CLAUDE_CODE_TOOLS)} Claude Code tools")
        except Exception as e:
            print(f"[SYSTEM] Error loading tools: {e}")
    if os.path.exists(system_file):
        try:
            with open(system_file, 'r', encoding='utf-8') as f:
                CLAUDE_CODE_SYSTEM = json.load(f)
            print("[SYSTEM] Loaded Claude Code system prompt")
        except Exception as e:
            print(f"[SYSTEM] Error loading system: {e}")

def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                loaded_config = json.load(f)
            config = DEFAULT_CONFIG.copy()
            config.update(loaded_config)
            print(f"[SYSTEM] Configuration loaded from {CONFIG_FILE}")
            return True
        except Exception as e:
            print(f"[SYSTEM] Error loading config: {e}")
            config = DEFAULT_CONFIG.copy()
            return False
    else:
        config = DEFAULT_CONFIG.copy()
        return False

def save_config():
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4)
        print(f"[SYSTEM] Configuration saved to {CONFIG_FILE}")
    except Exception as e:
        print(f"[SYSTEM] Error saving config: {e}")

def setup_wizard():
    print("\n" + "="*60)
    print("AnyRouter Proxy Setup Wizard")
    print("="*60)
    print("Please configure your proxy settings.\n")
    current_key = config.get('api_key', '')
    masked_key = f"{current_key[:8]}...{current_key[-4:]}" if len(current_key) > 12 else current_key
    api_key = input(f"Enter AnyRouter API Key [{masked_key}]: ").strip()
    if api_key:
        config['api_key'] = api_key
    elif not current_key:
        print("Warning: API Key is empty!")
    use_proxy_str = "y" if config.get('use_proxy', True) else "n"
    use_proxy = input(f"Use HTTP Proxy? (y/n) [{use_proxy_str}]: ").strip().lower()
    if use_proxy:
        config['use_proxy'] = (use_proxy == 'y')
    if config['use_proxy']:
        current_proxy = config.get('proxy_url', '')
        proxy_url = input(f"Proxy URL [{current_proxy}]: ").strip()
        if proxy_url:
            config['proxy_url'] = proxy_url
    debug_str = "y" if config.get('debug', False) else "n"
    debug_mode = input(f"Enable Debug Mode? (y/n) [{debug_str}]: ").strip().lower()
    if debug_mode:
        config['debug'] = (debug_mode == 'y')
    save_config()
    print("\n" + "="*60)
    print("Setup complete!")
    print("="*60 + "\n")

app = FastAPI()

def get_claude_headers(is_stream=False, model=""):
    if "opus" in model.lower() or "sonnet" in model.lower():
        beta = "claude-code-20250219,interleaved-thinking-2025-05-14"
    else:
        beta = "interleaved-thinking-2025-05-14"
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "connection": "keep-alive",
        "user-agent": "claude-cli/2.0.76 (external, cli)",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": beta,
        "anthropic-dangerous-direct-browser-access": "true",
        "x-app": "cli",
        "x-stainless-arch": "x64",
        "x-stainless-lang": "js",
        "x-stainless-os": "Windows",
        "x-stainless-package-version": "0.70.0",
        "x-stainless-retry-count": "0",
        "x-stainless-runtime": "node",
        "x-stainless-runtime-version": "v24.3.0",
        "x-stainless-timeout": "600",
    }
    if is_stream:
        headers["x-stainless-helper-method"] = "stream"
    return headers

def create_async_client():
    proxy_url = config['proxy_url'] if config['use_proxy'] else None
    if config['debug']:
        print(f"[SYSTEM] Creating client with proxy: {proxy_url}")
    return httpx.AsyncClient(
        http2=True,
        verify=False,
        timeout=httpx.Timeout(connect=60.0, read=300.0, write=60.0, pool=300.0),
        proxy=proxy_url,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10)
    )

@app.on_event("startup")
async def startup():
    global CLIENT
    CLIENT = create_async_client()

@app.on_event("shutdown")
async def shutdown():
    global CLIENT
    if CLIENT:
        await CLIENT.aclose()

async def stream_response(resp):
    try:
        async for chunk in resp.aiter_bytes():
            yield chunk
    except Exception as e:
        print(f"[PROXY] Stream error: {e}")

TRAILING_ASSISTANT_INVALID_ENDINGS = ",，"

def _sanitize_text_tail(text, aggressive=False):
    if not isinstance(text, str):
        return text
    sanitized = text.rstrip()
    if aggressive:
        sanitized = sanitized.rstrip(TRAILING_ASSISTANT_INVALID_ENDINGS)
    return sanitized

def _sanitize_assistant_content(content, aggressive=False):
    changed = False
    if isinstance(content, str):
        sanitized = _sanitize_text_tail(content, aggressive=aggressive)
        changed = sanitized != content
        return sanitized, changed
    if isinstance(content, dict):
        if content.get("type") == "text" and isinstance(content.get("text"), str):
            sanitized_text = _sanitize_text_tail(content["text"], aggressive=aggressive)
            changed = sanitized_text != content["text"]
            if changed:
                return {**content, "text": sanitized_text}, True
        return content, False
    if isinstance(content, list):
        blocks = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                sanitized_text = _sanitize_text_tail(block["text"], aggressive=False)
                if sanitized_text != block["text"]:
                    block = {**block, "text": sanitized_text}
                    changed = True
            blocks.append(block)
        if aggressive and blocks:
            for idx in range(len(blocks) - 1, -1, -1):
                block = blocks[idx]
                if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                    sanitized_text = _sanitize_text_tail(block["text"], aggressive=True)
                    if sanitized_text != block["text"]:
                        blocks[idx] = {**block, "text": sanitized_text}
                        changed = True
                    break
        while blocks:
            tail = blocks[-1]
            if not (isinstance(tail, dict) and tail.get("type") == "text" and tail.get("text") == ""):
                break
            blocks.pop()
            changed = True
        return blocks, changed
    return content, False

def sanitize_messages(messages, aggressive=False):
    if not isinstance(messages, list):
        return messages, 0
    changed = 0
    sanitized = []
    total = len(messages)
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            sanitized.append(msg)
            continue
        content = msg.get("content")
        is_last_message = idx == (total - 1)
        content, msg_changed = _sanitize_assistant_content(content, aggressive=aggressive and is_last_message)
        if msg_changed:
            msg = {**msg, "content": content}
            changed += 1
        sanitized.append(msg)
    if sanitized and isinstance(sanitized[-1], dict) and sanitized[-1].get("role") == "assistant":
        last_content = sanitized[-1].get("content")
        if (isinstance(last_content, str) and last_content == "") or (isinstance(last_content, list) and len(last_content) == 0):
            sanitized.pop()
            changed += 1
    return sanitized, changed

def _to_int(value):
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

def _calculate_thinking_budget(max_tokens):
    max_tokens_int = _to_int(max_tokens)
    if max_tokens_int is None:
        return 1024
    if max_tokens_int <= 1024:
        return None
    budget = max(1024, min(10000, max_tokens_int // 4))
    if budget >= max_tokens_int:
        budget = max_tokens_int - 1
    if budget < 1024:
        return None
    return budget

def _normalize_thinking_config(existing_thinking, max_tokens):
    max_tokens_int = _to_int(max_tokens)
    if isinstance(existing_thinking, dict):
        budget = _to_int(existing_thinking.get("budget_tokens"))
    else:
        budget = None
    if budget is None:
        budget = _calculate_thinking_budget(max_tokens_int)
    if budget is None:
        return None
    if max_tokens_int is not None and budget >= max_tokens_int:
        if max_tokens_int <= 1024:
            return None
        budget = max_tokens_int - 1
        if budget < 1024:
            return None
    return {"type": "enabled", "budget_tokens": budget}

@app.get("/config")
async def get_config():
    safe_config = config.copy()
    if len(safe_config['api_key']) > 10:
        safe_config['api_key'] = safe_config['api_key'][:8] + "..." + safe_config['api_key'][-4:]
    return safe_config

@app.post("/config/reload")
async def reload_config():
    global CLIENT
    load_config()
    if CLIENT:
        await CLIENT.aclose()
    CLIENT = create_async_client()
    return {"status": "ok", "message": "Configuration reloaded"}

@app.get("/health")
async def health():
    return {"status": "ok", "version": "v22", "proxy_enabled": config['use_proxy'], "tools_loaded": len(CLAUDE_CODE_TOOLS)}

@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
async def proxy(path: str, request: Request):
    global CLIENT
    target_url = f"{config['target_base_url']}/{path}"
    if path == "messages":
        target_url += "?beta=true"
    body = await request.body()
    body_json = {}
    wants_stream = False
    removed_sampling_params = {}
    if body:
        try:
            body_json = json.loads(body)
            safe_keys = {'model', 'messages', 'max_tokens', 'metadata', 'stop_sequences', 'stream', 'system', 'temperature', 'top_k', 'top_p', 'tools', 'thinking'}
            filtered_body = {k: v for k, v in body_json.items() if k in safe_keys}
            model = filtered_body.get('model', '')
            if 'anyrouter/' in model:
                filtered_body['model'] = model.replace('anyrouter/', '')
            if config['debug']:
                print(f"[PROXY] Original request keys: {list(body_json.keys())}")
                print(f"[PROXY] Has tools: {'tools' in body_json}, tools count: {len(body_json.get('tools', []))}")
                print(f"[PROXY] Has system: {'system' in body_json}")
                print(f"[PROXY] Has thinking: {'thinking' in body_json}")
            if 'messages' in filtered_body:
                filtered_body['messages'], sanitize_changed = sanitize_messages(filtered_body.get('messages'))
                if config['debug'] and sanitize_changed:
                    print(f"[PROXY] Sanitized assistant messages: {sanitize_changed}")
            if ('sonnet' in model.lower() or 'opus' in model.lower() or 'haiku' in model.lower()) and CLAUDE_CODE_TOOLS:
                filtered_body['tools'] = CLAUDE_CODE_TOOLS
                if config['debug']:
                    print(f"[PROXY] Injected {len(CLAUDE_CODE_TOOLS)} Claude Code tools")
                if CLAUDE_CODE_SYSTEM:
                    filtered_body['system'] = CLAUDE_CODE_SYSTEM
                    if config['debug']:
                        print("[PROXY] Injected Claude Code system prompt")
                if 'sonnet' in model.lower() or 'opus' in model.lower():
                    normalized_thinking = _normalize_thinking_config(filtered_body.get('thinking'), filtered_body.get('max_tokens'))
                    if normalized_thinking:
                        filtered_body['thinking'] = normalized_thinking
                        if config['debug']:
                            print(f"[PROXY] Normalized thinking config: {normalized_thinking}")
                    elif 'thinking' in filtered_body:
                        filtered_body.pop('thinking', None)
                        if config['debug']:
                            print("[PROXY] Removed invalid thinking config")
                    if 'thinking' in filtered_body:
                        for sampling_key in ("temperature", "top_p", "top_k"):
                            if sampling_key in filtered_body:
                                removed_sampling_params[sampling_key] = filtered_body.pop(sampling_key)
                        if config['debug'] and removed_sampling_params:
                            print(f"[PROXY] Removed sampling params for thinking compatibility: {list(removed_sampling_params.keys())}")
                filtered_body['metadata'] = {"user_id": "proxy_user"}
            wants_stream = filtered_body.get('stream', False)
            body_json = filtered_body
        except Exception as e:
            if config['debug']:
                print(f"[PROXY] Body parse error: {e}")
    model_name = body_json.get('model', '')
    headers = get_claude_headers(is_stream=wants_stream, model=model_name)
    req_auth = request.headers.get("Authorization")
    if config['api_key']:
        headers["x-api-key"] = config['api_key']
        headers["Authorization"] = f"Bearer {config['api_key']}"
    elif req_auth:
        headers["Authorization"] = req_auth
    if config['debug']:
        print(f"\n{'='*60}")
        print(f"[PROXY] Target: {target_url}")
        print(f"[PROXY] Model: {body_json.get('model', 'N/A')}")
        print(f"[PROXY] Stream: {wants_stream}")
    max_attempts = 5
    retry_delay = 1
    thinking_compat_retried = False
    aggressive_sanitize_retried = False
    for attempt in range(max_attempts):
        try:
            if config['debug']:
                print(f"[PROXY] Attempt {attempt + 1}/{max_attempts}...")
                sys.stdout.flush()
            req = CLIENT.build_request(request.method, target_url, headers=headers, json=body_json, timeout=None)
            if wants_stream:
                resp = await CLIENT.send(req, stream=True)
                if config['debug']:
                    print(f"[PROXY] Status: {resp.status_code}")
                if resp.status_code in [520, 502]:
                    await resp.aclose()
                    if attempt < max_attempts - 1:
                        CLIENT = create_async_client()
                        await asyncio.sleep(retry_delay)
                        continue
                    return Response(content=b'{"error":{"message":"Network error after max retries"}}', status_code=502, media_type="application/json")
                if resp.status_code in [403, 500]:
                    error_content = await resp.aread()
                    await resp.aclose()
                    if config['debug']:
                        print(f"[PROXY] Error response: {error_content.decode('utf-8', errors='ignore')[:500]}")
                    return Response(content=error_content, status_code=resp.status_code, media_type="application/json")
                if resp.status_code >= 400:
                    error_content = await resp.aread()
                    await resp.aclose()
                    error_text = error_content.decode('utf-8', errors='ignore')
                    if config['debug']:
                        print(f"[PROXY] Error response: {error_text[:500]}")
                        print(f"[PROXY] Error response body length: {len(error_content)}")
                    if (
                        resp.status_code == 400
                        and 'thinking' in body_json
                        and not thinking_compat_retried
                    ):
                        body_json.pop('thinking', None)
                        if removed_sampling_params:
                            for sampling_key, sampling_value in removed_sampling_params.items():
                                body_json[sampling_key] = sampling_value
                        thinking_compat_retried = True
                        if config['debug']:
                            print("[PROXY] Retrying without thinking for compatibility")
                        await asyncio.sleep(0.1)
                        continue
                    if (
                        resp.status_code == 400
                        and "messages: final assistant content cannot end with trailing" in error_text
                        and not aggressive_sanitize_retried
                        and isinstance(body_json, dict)
                        and 'messages' in body_json
                    ):
                        body_json['messages'], aggressive_changed = sanitize_messages(body_json.get('messages'), aggressive=True)
                        aggressive_sanitize_retried = True
                        if config['debug']:
                            print(f"[PROXY] Retrying after aggressive assistant-tail sanitize, changed: {aggressive_changed}")
                        await asyncio.sleep(0.1)
                        continue
                    return Response(content=error_content, status_code=resp.status_code, media_type="application/json")
                return StreamingResponse(stream_response(resp), status_code=resp.status_code, media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
            else:
                resp = await CLIENT.send(req)
                if config['debug']:
                    print(f"[PROXY] Status: {resp.status_code}")
                if resp.status_code in [520, 502]:
                    if attempt < max_attempts - 1:
                        CLIENT = create_async_client()
                        await asyncio.sleep(retry_delay)
                        continue
                    return Response(content=b'{"error":{"message":"Network error after max retries"}}', status_code=502, media_type="application/json")
                if resp.status_code in [403, 500]:
                    return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")
                if resp.status_code >= 400:
                    error_content = resp.content
                    error_text = error_content.decode('utf-8', errors='ignore')
                    if config['debug']:
                        print(f"[PROXY] Error response: {error_text[:500]}")
                        print(f"[PROXY] Error response body length: {len(error_content)}")
                    if (
                        resp.status_code == 400
                        and 'thinking' in body_json
                        and not thinking_compat_retried
                    ):
                        body_json.pop('thinking', None)
                        if removed_sampling_params:
                            for sampling_key, sampling_value in removed_sampling_params.items():
                                body_json[sampling_key] = sampling_value
                        thinking_compat_retried = True
                        if config['debug']:
                            print("[PROXY] Retrying without thinking for compatibility")
                        await asyncio.sleep(0.1)
                        continue
                    if (
                        resp.status_code == 400
                        and "messages: final assistant content cannot end with trailing" in error_text
                        and not aggressive_sanitize_retried
                        and isinstance(body_json, dict)
                        and 'messages' in body_json
                    ):
                        body_json['messages'], aggressive_changed = sanitize_messages(body_json.get('messages'), aggressive=True)
                        aggressive_sanitize_retried = True
                        if config['debug']:
                            print(f"[PROXY] Retrying after aggressive assistant-tail sanitize, changed: {aggressive_changed}")
                        await asyncio.sleep(0.1)
                        continue
                    return Response(content=error_content, status_code=resp.status_code, media_type="application/json")
                return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")
        except Exception as e:
            if config['debug']:
                print(f"[PROXY] Error: {type(e).__name__}: {e}")
                traceback.print_exc()
            if attempt < max_attempts - 1:
                CLIENT = create_async_client()
            else:
                return Response(content=json.dumps({"error": {"message": str(e)}}), status_code=500)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="AnyRouter Proxy Server")
    parser.add_argument("--setup", action="store_true", help="Run setup wizard")
    args = parser.parse_args()
    config_loaded = load_config()
    load_claude_code_templates()
    if args.setup or not config_loaded or not config.get('api_key'):
        setup_wizard()
    print("=" * 60)
    print("AnyRouter Proxy Server v22")
    print("=" * 60)
    print(f"Target: {config['target_base_url']}")
    print(f"Proxy:  {config['proxy_url'] if config['use_proxy'] else 'Disabled'}")
    print(f"Debug:  {'Enabled' if config['debug'] else 'Disabled'}")
    print(f"Tools:  {len(CLAUDE_CODE_TOOLS)} Claude Code tools loaded")
    print("-" * 60)
    if sys.platform == 'win32':
        sys.stdout.reconfigure(encoding='utf-8')
    log_level = "info" if config['debug'] else "warning"
    try:
        uvicorn.run(app, host="0.0.0.0", port=8765, log_level=log_level)
    except KeyboardInterrupt:
        print("\nStopping server...")
