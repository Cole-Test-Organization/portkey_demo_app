#!/usr/bin/env python3
"""Dependency-free server for the role-based AI demo.

It serves the SPA and proxies model requests so the gateway key never reaches
the browser.
"""

from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import os
import re
import socket
import stat
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit


BASE_DIR = Path(__file__).resolve().parent
APP_DIR = Path(os.environ.get("APP_DIR", BASE_DIR / "app")).resolve()
ENV_FILE = Path(os.environ.get("PORTKEY_ENV_FILE", BASE_DIR / ".env")).resolve()


def load_environment_file(path: Path) -> None:
    """Load a small KEY=VALUE file without shell evaluation or expansion."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return

    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, raw_value = stripped.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError(f"Invalid environment entry on line {line_number}.")

        value = raw_value.strip()
        if value.startswith('"'):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid quoted environment value on line {line_number}."
                ) from exc
            if not isinstance(parsed, str):
                raise ValueError(
                    f"Environment value on line {line_number} must be a string."
                )
            value = parsed
        elif value.startswith("'") or value.endswith(("'", '"')):
            raise ValueError(f"Invalid environment quoting on line {line_number}.")

        os.environ.setdefault(key, value)


load_environment_file(ENV_FILE)

ROUTING_CONFIG_PATH = Path(
    os.environ.get(
        "PORTKEY_ROUTING_CONFIG_FILE",
        BASE_DIR / "portkey-role-routing-config.json",
    )
).resolve()
ROUTING_CONFIG_TEMPLATE_PATH = BASE_DIR / "portkey-role-routing-config.example.json"


def normalize_gateway_url(value: str) -> str:
    gateway_url = value.strip().rstrip("/")
    parsed = urlsplit(gateway_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("PORTKEY_GATEWAY_URL must be a plain HTTPS URL.")
    return gateway_url


GATEWAY_URL = normalize_gateway_url(
    os.environ.get("PORTKEY_GATEWAY_URL", "https://aigw.portkey.ai/v1")
)
PORTKEY_API_KEY = os.environ.get("PORTKEY_API_KEY", "").strip()
PROVIDER_SLUG = os.environ.get("PORTKEY_PROVIDER_SLUG", "gemini-example").strip()
REQUEST_TIMEOUT = min(max(int(os.environ.get("REQUEST_TIMEOUT", "90")), 5), 300)
MAX_GATEWAY_RESPONSE_BYTES = min(
    max(int(os.environ.get("MAX_GATEWAY_RESPONSE_BYTES", "2000000")), 65_536),
    10_000_000,
)
MAX_PROMPT_CHARS = min(
    max(int(os.environ.get("MAX_PROMPT_CHARS", "32000")), 1000), 250000
)

DEFAULT_ROLE_SLOTS: tuple[dict[str, str], ...] = (
    {
        "id": "sales",
        "label": "Sales",
        "description": "Fast, economical help for high-volume customer work.",
        "model": "gemini-3.5-flash-lite",
    },
    {
        "id": "hr",
        "label": "HR",
        "description": "Balanced reasoning for people and policy workflows.",
        "model": "gemini-3.6-flash",
    },
    {
        "id": "devs",
        "label": "Devs",
        "description": "Maximum capability for engineering and complex analysis.",
        "model": "gemini-3.5-flash",
    },
)
CONFIG_LOCK = threading.RLock()

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger("portkey-model-console")


def clean_provider_slug(value: str) -> str:
    return value.strip().removeprefix("@")


def clean_model_id(value: str) -> str:
    model = value.strip()
    if model.startswith("@") and "/" in model:
        model = model.split("/", 1)[1]
    return model


def role_id_from_label(label: str) -> str:
    role_id = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    return role_id


def model_reference(provider_slug: str, model: str) -> str:
    return f"@{clean_provider_slug(provider_slug)}/{clean_model_id(model)}"


def roles_from_environment() -> dict[str, dict[str, str]]:
    roles: dict[str, dict[str, str]] = {}
    provider_slug = clean_provider_slug(PROVIDER_SLUG) or "gemini-example"

    for slot, defaults in enumerate(DEFAULT_ROLE_SLOTS, start=1):
        role_id = os.environ.get(
            f"PORTKEY_ROLE_{slot}_ID", defaults["id"]
        ).strip()
        label = os.environ.get(
            f"PORTKEY_ROLE_{slot}_LABEL", defaults["label"]
        ).strip()
        model = os.environ.get(
            f"PORTKEY_ROLE_{slot}_MODEL", defaults["model"]
        ).strip()
        if not role_id or role_id in roles:
            role_id = defaults["id"]
        if not label:
            label = defaults["label"]
        if not model:
            model = defaults["model"]

        description = (
            defaults["description"]
            if role_id == defaults["id"] and label == defaults["label"]
            else f"{label} requests use the configured AI route."
        )
        roles[role_id] = {
            "label": label,
            "description": description,
            "model": model_reference(provider_slug, model),
        }

    return roles


ROLES = roles_from_environment()


def role_models_from_routing_config() -> dict[str, str]:
    """Derive the GUI model labels from the Portkey routing artifact."""
    try:
        source_path = (
            ROUTING_CONFIG_PATH
            if ROUTING_CONFIG_PATH.is_file()
            else ROUTING_CONFIG_TEMPLATE_PATH
        )
        config = json.loads(source_path.read_text(encoding="utf-8"))
        targets = {
            target["name"]: target.get("override_params", {}).get("model")
            for target in config.get("targets", [])
            if isinstance(target, dict) and isinstance(target.get("name"), str)
        }
        models: dict[str, str] = {}
        for condition in config.get("strategy", {}).get("conditions", []):
            if not isinstance(condition, dict):
                continue
            query = condition.get("query", {})
            role_query = (
                query.get("metadata.user_role", {})
                if isinstance(query, dict)
                else {}
            )
            role = role_query.get("$eq") if isinstance(role_query, dict) else None
            model = targets.get(condition.get("then"))
            if role in ROLES and isinstance(model, str) and model:
                models[role] = model
        return models
    except (OSError, json.JSONDecodeError, TypeError, KeyError) as exc:
        LOGGER.warning("Unable to load model display mapping: %s", exc)
        return {}


def public_config() -> dict[str, Any]:
    """Return browser-safe application configuration."""
    with CONFIG_LOCK:
        role_models = role_models_from_routing_config()
        return {
            "configured": bool(PORTKEY_API_KEY),
            "providerSlug": clean_provider_slug(PROVIDER_SLUG),
            "maxPromptChars": MAX_PROMPT_CHARS,
            "roles": {
                role: {
                    **profile,
                    "model": role_models.get(role) or profile.get("model"),
                }
                for role, profile in ROLES.items()
            },
        }


def validate_onboarding(
    payload: dict[str, Any],
) -> tuple[str, str, list[dict[str, str]]]:
    service_key = payload.get("serviceKey")
    provider_slug = payload.get("providerSlug")
    role_inputs = payload.get("roles")

    if not isinstance(service_key, str) or not service_key.strip():
        raise ValueError("Enter the service API key.")
    service_key = service_key.strip()
    if len(service_key) > 1000 or any(ord(char) < 32 for char in service_key):
        raise ValueError("The service API key is invalid.")

    if not isinstance(provider_slug, str):
        raise ValueError("Enter the provider profile slug.")
    provider_slug = clean_provider_slug(provider_slug)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", provider_slug):
        raise ValueError(
            "The provider slug may contain only letters, numbers, underscores, and hyphens."
        )

    if not isinstance(role_inputs, list) or len(role_inputs) != 3:
        raise ValueError("Configure exactly three roles.")

    roles: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for index, role_input in enumerate(role_inputs, start=1):
        if not isinstance(role_input, dict):
            raise ValueError(f"Role {index} is invalid.")
        label = role_input.get("label")
        model = role_input.get("model")
        if not isinstance(label, str) or not label.strip():
            raise ValueError(f"Enter a name for role {index}.")
        if not isinstance(model, str) or not model.strip():
            raise ValueError(f"Choose a model for role {index}.")
        label = label.strip()
        model = clean_model_id(model)
        role_id = role_id_from_label(label)

        if len(label) > 64 or not role_id or any(ord(char) < 32 for char in label):
            raise ValueError(
                f"Role {index} must contain letters or numbers, no control characters, "
                "and be at most 64 characters."
            )
        if role_id in seen_ids:
            raise ValueError("Use three distinct role names.")
        if not re.fullmatch(r"[A-Za-z0-9._:/-]+", model):
            raise ValueError(f"The model ID for {label} contains unsupported characters.")

        seen_ids.add(role_id)
        roles.append({"id": role_id, "label": label, "model": model})

    return service_key, provider_slug, roles


def build_routing_config(
    provider_slug: str, roles: list[dict[str, str]]
) -> dict[str, Any]:
    conditions: list[dict[str, Any]] = []
    targets: list[dict[str, Any]] = []

    for index, role in enumerate(roles, start=1):
        target_name = f"role-{index}-route"
        conditions.append(
            {
                "query": {"metadata.user_role": {"$eq": role["id"]}},
                "then": target_name,
            }
        )
        targets.append(
            {
                "name": target_name,
                "override_params": {
                    "model": model_reference(provider_slug, role["model"]),
                },
            }
        )

    return {
        "strategy": {
            "mode": "conditional",
            "conditions": conditions,
            "default": "role-1-route",
        },
        "targets": targets,
    }


def quote_environment_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def render_environment(
    service_key: str, provider_slug: str, roles: list[dict[str, str]]
) -> str:
    values = [
        ("PORTKEY_API_KEY", service_key),
        ("PORTKEY_GATEWAY_URL", GATEWAY_URL),
        ("PORTKEY_PROVIDER_SLUG", provider_slug),
        ("PORTKEY_ROLE_COUNT", "3"),
    ]
    for index, role in enumerate(roles, start=1):
        values.extend(
            [
                (f"PORTKEY_ROLE_{index}_ID", role["id"]),
                (f"PORTKEY_ROLE_{index}_LABEL", role["label"]),
                (f"PORTKEY_ROLE_{index}_MODEL", role["model"]),
            ]
        )
    return "".join(
        f'{key}="{quote_environment_value(value)}"\n' for key, value in values
    )


def overwrite_state_file(path: Path, content: str) -> None:
    """Create or update a regular state file without following symlinks."""
    flags = os.O_WRONLY | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise OSError(f"State path is not a regular file: {path}")

    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.seek(0)
        handle.write(content)
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())


def save_onboarding(
    service_key: str, provider_slug: str, roles: list[dict[str, str]]
) -> dict[str, Any]:
    global PORTKEY_API_KEY, PROVIDER_SLUG, ROLES

    routing_config = build_routing_config(provider_slug, roles)
    environment = render_environment(service_key, provider_slug, roles)

    with CONFIG_LOCK:
        overwrite_state_file(
            ROUTING_CONFIG_PATH,
            json.dumps(routing_config, indent=2) + "\n",
        )
        overwrite_state_file(ENV_FILE, environment)

        PORTKEY_API_KEY = service_key
        PROVIDER_SLUG = provider_slug
        for index, role in enumerate(roles, start=1):
            os.environ[f"PORTKEY_ROLE_{index}_ID"] = role["id"]
            os.environ[f"PORTKEY_ROLE_{index}_LABEL"] = role["label"]
            os.environ[f"PORTKEY_ROLE_{index}_MODEL"] = role["model"]
        os.environ["PORTKEY_PROVIDER_SLUG"] = provider_slug
        ROLES = roles_from_environment()

    return routing_config


def extract_content(payload: dict[str, Any]) -> str:
    """Normalize an OpenAI-compatible chat completion into plain text."""
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("Gateway response did not include completion content.") from exc

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        if parts:
            return "\n".join(parts)

    raise ValueError("Gateway returned completion content in an unsupported format.")


def gateway_error_message(raw_body: bytes, status: int) -> str:
    """Extract a useful provider error without returning a full raw response."""
    fallback = f"The AI gateway returned HTTP {status}."
    try:
        payload = json.loads(raw_body.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return fallback

    candidates: list[Any] = [
        payload.get("message") if isinstance(payload, dict) else None,
        payload.get("error") if isinstance(payload, dict) else None,
    ]
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        candidates.insert(0, payload["error"].get("message"))

    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            message = re.sub(
                r"portkey",
                "AI gateway",
                candidate.strip(),
                flags=re.IGNORECASE,
            )
            if PORTKEY_API_KEY:
                message = message.replace(PORTKEY_API_KEY, "[redacted]")
            message = re.sub(
                r"(?i)\b(bearer\s+|sk-|AIza)[A-Za-z0-9._/-]{12,}",
                "[redacted]",
                message,
            )
            return message[:1000]
    return fallback


def read_gateway_response(response: Any) -> bytes:
    body = response.read(MAX_GATEWAY_RESPONSE_BYTES + 1)
    if len(body) > MAX_GATEWAY_RESPONSE_BYTES:
        raise ValueError("Gateway response exceeded the configured size limit.")
    return body


class AppHandler(BaseHTTPRequestHandler):
    server_version = "AIRoleDemo"
    sys_version = ""

    def version_string(self) -> str:
        return self.server_version

    def log_message(self, fmt: str, *args: Any) -> None:
        LOGGER.info("%s %s", self.address_string(), fmt % args)

    def _send_headers(
        self,
        status: int,
        content_type: str,
        content_length: int,
        *,
        cache_control: str = "no-store",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Permitted-Cross-Domain-Policies", "none")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Strict-Transport-Security", "max-age=31536000")
        self.send_header(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=()",
        )
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "font-src 'self'; "
            "base-uri 'none'; "
            "frame-ancestors 'none'; "
            "form-action 'self'",
        )
        self.end_headers()

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any] | None:
        content_type = self.headers.get("Content-Type", "")
        media_type = content_type.partition(";")[0].strip().lower()
        if media_type != "application/json":
            self._json(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                {"error": "Content-Type must be application/json."},
            )
            return None

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid Content-Length."})
            return None

        if content_length <= 0 or content_length > 512_000:
            self._json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": "Request body is empty or too large."},
            )
            return None

        try:
            payload = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON body."})
            return None

        if not isinstance(payload, dict):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "JSON body must be an object."})
            return None
        return payload

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path

        if path == "/api/health":
            self._json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "configured": bool(PORTKEY_API_KEY),
                    "service": "ai-role-demo",
                },
            )
            return

        if path == "/api/config":
            self._json(HTTPStatus.OK, public_config())
            return

        self._serve_static(path)

    def do_HEAD(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path.startswith("/api/"):
            body = b'{"error":"Method not allowed."}'
            self._send_headers(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "application/json; charset=utf-8",
                len(body),
            )
            return
        self._serve_static(path, head_only=True)

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path not in {"/api/chat", "/api/onboarding"}:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
            return

        payload = self._read_json()
        if payload is None:
            return

        if path == "/api/onboarding":
            self._save_onboarding(payload)
            return

        if not PORTKEY_API_KEY:
            self._json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "error": (
                        "The AI gateway is not configured yet. Open Setup to finish onboarding."
                    )
                },
            )
            return

        role = payload.get("role")
        if not isinstance(role, str) or role not in ROLES:
            self._json(
                HTTPStatus.BAD_REQUEST,
                {"error": f"Role must be one of: {', '.join(ROLES)}."},
            )
            return

        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Prompt cannot be empty."})
            return

        prompt = prompt.strip()
        if len(prompt) > MAX_PROMPT_CHARS:
            self._json(
                HTTPStatus.BAD_REQUEST,
                {"error": f"Prompt exceeds the {MAX_PROMPT_CHARS:,}-character limit."},
            )
            return

        self._call_gateway(role, prompt)

    def _save_onboarding(self, payload: dict[str, Any]) -> None:
        try:
            service_key, provider_slug, roles = validate_onboarding(payload)
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        with CONFIG_LOCK:
            current_key = PORTKEY_API_KEY
        if current_key and not hmac.compare_digest(service_key, current_key):
            self._json(
                HTTPStatus.FORBIDDEN,
                {"error": "The service API key does not match the current setup."},
            )
            return

        try:
            routing_config = save_onboarding(service_key, provider_slug, roles)
        except OSError:
            LOGGER.exception("Unable to persist onboarding configuration")
            self._json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "The server could not save the setup files."},
            )
            return

        self._json(
            HTTPStatus.OK,
            {
                "saved": True,
                "config": public_config(),
                "routingConfig": routing_config,
            },
        )

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Method not allowed."})

    def _serve_static(self, request_path: str, *, head_only: bool = False) -> None:
        relative_path = "index.html" if request_path == "/" else unquote(request_path).lstrip("/")
        candidate = (APP_DIR / relative_path).resolve()
        try:
            candidate.relative_to(APP_DIR)
        except ValueError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
            return

        if not candidate.is_file():
            # Unknown browser routes fall back to the SPA shell; files do not.
            if "." not in Path(relative_path).name:
                candidate = APP_DIR / "index.html"
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
                return

        try:
            body = candidate.read_bytes()
        except OSError:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Unable to read asset."})
            return

        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {
            "application/javascript",
            "application/json",
        }:
            content_type += "; charset=utf-8"

        cache_control = (
            "no-cache"
            if candidate.name == "index.html"
            else "public, max-age=3600"
        )
        self._send_headers(
            HTTPStatus.OK,
            content_type,
            len(body),
            cache_control=cache_control,
        )
        if not head_only:
            self.wfile.write(body)

    def _gateway_request(
        self, role: str, prompt: str
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Build a Portkey-routed request without selecting a model locally."""
        request_payload = {
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "portkey-model-console/1.0",
            "x-portkey-api-key": PORTKEY_API_KEY,
            "x-portkey-metadata": json.dumps(
                {"source": "portkey-model-console", "user_role": role},
                separators=(",", ":"),
            ),
        }
        return request_payload, headers

    def _call_gateway(self, role: str, prompt: str) -> None:
        request_payload, headers = self._gateway_request(role, prompt)
        body = json.dumps(request_payload).encode("utf-8")
        request = urllib.request.Request(
            f"{GATEWAY_URL}/chat/completions",
            data=body,
            headers=headers,
            method="POST",
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                raw_response = read_gateway_response(response)
                response_payload = json.loads(raw_response)
                if not isinstance(response_payload, dict):
                    raise ValueError("Gateway returned an invalid JSON response.")
                content = extract_content(response_payload)
                elapsed_ms = round((time.monotonic() - started) * 1000)
                self._json(
                    HTTPStatus.OK,
                    {
                        "content": content,
                        "model": response_payload.get("model", "Gateway-routed"),
                        "role": role,
                        "usage": response_payload.get("usage"),
                        "traceId": (
                            response.headers.get("x-portkey-trace-id")
                            or response.headers.get("x-request-id")
                        ),
                        "elapsedMs": elapsed_ms,
                    },
                )
        except urllib.error.HTTPError as exc:
            raw_error = exc.read(65_536)
            message = gateway_error_message(raw_error, exc.code)
            LOGGER.warning(
                "Gateway rejected %s role request with HTTP %s: %s",
                role,
                exc.code,
                message,
            )
            self._json(
                HTTPStatus.BAD_GATEWAY,
                {
                    "error": message,
                    "upstreamStatus": exc.code,
                    "role": role,
                },
            )
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            LOGGER.warning("Gateway connection failed for role %s: %s", role, exc)
            self._json(
                HTTPStatus.GATEWAY_TIMEOUT,
                {
                    "error": "The AI gateway could not be reached before the timeout.",
                    "role": role,
                },
            )
        except (json.JSONDecodeError, ValueError) as exc:
            LOGGER.warning("Unexpected gateway response for role %s: %s", role, exc)
            self._json(
                HTTPStatus.BAD_GATEWAY,
                {"error": str(exc), "role": role},
            )
        except Exception:
            LOGGER.exception("Unexpected proxy failure for role %s", role)
            self._json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "Unexpected proxy error.", "role": role},
            )


def main() -> None:
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8080"))
    if not APP_DIR.is_dir():
        raise SystemExit(f"SPA asset directory does not exist: {APP_DIR}")

    server = ThreadingHTTPServer((host, port), AppHandler)
    LOGGER.info(
        "Serving AI Role Demo on %s:%s (configured=%s, gateway=%s)",
        host,
        port,
        bool(PORTKEY_API_KEY),
        GATEWAY_URL,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
