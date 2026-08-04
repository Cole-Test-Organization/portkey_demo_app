#!/usr/bin/env python3
"""Dependency-free server for the role-based AI demo.

It serves the SPA and proxies model requests so the gateway key never reaches
the browser.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import socket
import tempfile
import unicodedata
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


SECRET_ENVIRONMENT_KEYS = frozenset({"PORTKEY_API_KEY"})


def load_environment_file(path: Path) -> dict[str, str]:
    """Load a small KEY=VALUE file without shell evaluation or expansion.

    Every parsed value is returned to the caller, but secrets are deliberately
    kept out of ``os.environ`` so the key is not exposed through the process
    environment or inherited by any child process.
    """
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}

    values: dict[str, str] = {}
    for line_number, line in enumerate(content.split("\n"), start=1):
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

        values[key] = value
        if key not in SECRET_ENVIRONMENT_KEYS:
            os.environ.setdefault(key, value)

    return values


FILE_ENVIRONMENT = load_environment_file(ENV_FILE)

ROUTING_CONFIG_PATH = Path(
    os.environ.get(
        "PORTKEY_ROUTING_CONFIG_FILE",
        BASE_DIR / "portkey-role-routing-config.json",
    )
).resolve()


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
PORTKEY_API_KEY = (
    os.environ.get("PORTKEY_API_KEY") or FILE_ENVIRONMENT.get("PORTKEY_API_KEY", "")
).strip()
PROVIDER_SLUG = os.environ.get("PORTKEY_PROVIDER_SLUG", "gemini-example").strip()
REQUEST_TIMEOUT = min(max(int(os.environ.get("REQUEST_TIMEOUT", "90")), 5), 300)
MAX_GATEWAY_RESPONSE_BYTES = min(
    max(int(os.environ.get("MAX_GATEWAY_RESPONSE_BYTES", "2000000")), 65_536),
    10_000_000,
)
MAX_PROMPT_CHARS = min(
    max(int(os.environ.get("MAX_PROMPT_CHARS", "32000")), 1000), 250000
)
KEY_VERIFY_TIMEOUT = min(max(int(os.environ.get("KEY_VERIFY_TIMEOUT", "10")), 2), 60)

# Result of the last gateway check on the stored key. "unknown" means no check
# has completed yet, so the GUI stays quiet rather than nagging on every boot.
KEY_STATUS = "unknown"

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
        "model": "gemini-3.5-flash",
    },
    {
        "id": "devs",
        "label": "Devs",
        "description": "Maximum capability for engineering and complex analysis.",
        "model": "gemini-3.6-flash",
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


def clean_service_key(value: str) -> str:
    """Remove paste artifacts from a service key.

    Copying a key out of a dashboard or PDF routinely picks up zero-width
    characters, byte-order marks, and non-breaking spaces. None of them are
    meaningful in a key, so drop them rather than rejecting a correct paste.
    """
    return "".join(
        char
        for char in value
        if not char.isspace() and unicodedata.category(char) != "Cf"
    )


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
        # Fall back until the slot has an unused ID. Reusing the slot default
        # unconditionally could collide again and silently drop a whole role.
        role_id = next(
            (
                candidate
                for candidate in (role_id, defaults["id"], f"role-{slot}")
                if candidate and candidate not in roles
            ),
            f"role-slot-{slot}",
        )
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
    """Derive the GUI model labels from the Portkey routing artifact.

    Before onboarding writes the artifact there is nothing to read, which is an
    expected state rather than a fault: callers fall back to the models declared
    in the environment, so an absent file returns an empty mapping silently.
    """
    if not ROUTING_CONFIG_PATH.is_file():
        return {}
    try:
        config = json.loads(ROUTING_CONFIG_PATH.read_text(encoding="utf-8"))
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


def verify_service_key(service_key: str) -> str:
    """Ask the gateway whether a key works.

    Returns "verified", "rejected", or "unverified". Only an explicit 401/403
    counts as rejection; anything else (unreachable gateway, missing endpoint,
    upstream outage) is inconclusive, so setup is never blocked by a problem
    that has nothing to do with the key.
    """
    request = urllib.request.Request(
        f"{GATEWAY_URL}/models",
        headers={
            "Accept": "application/json",
            "User-Agent": "portkey-model-console/1.0",
            "x-portkey-api-key": service_key,
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=KEY_VERIFY_TIMEOUT) as response:
            response.read(8192)
            return "verified"
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            return "rejected"
        LOGGER.info("Key check inconclusive: gateway returned HTTP %s", exc.code)
        return "unverified"
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
        LOGGER.info("Key check inconclusive: %s", exc)
        return "unverified"


def refresh_key_status() -> str:
    """Re-check the stored key and remember the outcome."""
    global KEY_STATUS
    with CONFIG_LOCK:
        key = PORTKEY_API_KEY
    status = verify_service_key(key) if key else "unknown"
    with CONFIG_LOCK:
        KEY_STATUS = status
    return status


def state_path_writable(path: Path) -> bool:
    """Report whether the app could create or replace a state file.

    Saving creates any missing parent directories, so a path that does not
    exist yet is writable as long as its nearest existing ancestor is.
    """
    if path.is_file():
        return os.access(path, os.W_OK)
    return next(
        (
            os.access(ancestor, os.W_OK)
            for ancestor in path.parents
            if ancestor.is_dir()
        ),
        False,
    )


def setup_status() -> dict[str, Any]:
    """Describe what onboarding still needs so the GUI can prompt for it."""
    issues: list[str] = []
    if not PORTKEY_API_KEY:
        issues.append("missing-service-key")
    elif KEY_STATUS == "rejected":
        issues.append("key-rejected")
    elif KEY_STATUS == "unverified":
        issues.append("key-unverified")
    if not ROUTING_CONFIG_PATH.is_file():
        issues.append("missing-routing-config")

    declared_ids = [
        os.environ.get(f"PORTKEY_ROLE_{slot}_ID", "").strip()
        for slot in range(1, len(DEFAULT_ROLE_SLOTS) + 1)
    ]
    if any(role_id and role_id not in ROLES for role_id in declared_ids):
        issues.append("role-ids-adjusted")

    status: dict[str, Any] = {"issues": issues}
    if not all(
        state_path_writable(path) for path in (ENV_FILE, ROUTING_CONFIG_PATH)
    ):
        # Only disclose the location when the operator has to go fix it.
        issues.append("state-not-writable")
        status["stateDir"] = str(ENV_FILE.parent)
    return status


def public_config() -> dict[str, Any]:
    """Return browser-safe application configuration."""
    with CONFIG_LOCK:
        role_models = role_models_from_routing_config()
        return {
            "configured": bool(PORTKEY_API_KEY),
            "providerSlug": clean_provider_slug(PROVIDER_SLUG),
            "maxPromptChars": MAX_PROMPT_CHARS,
            "setup": setup_status(),
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

    if not isinstance(service_key, str):
        raise ValueError("Enter the service API key.")
    service_key = clean_service_key(service_key)
    if not service_key:
        raise ValueError("Enter the service API key.")
    if len(service_key) > 1000:
        raise ValueError("The service API key is too long.")
    # Printable ASCII only. Keys are sent as an HTTP header value, where
    # non-ASCII bytes are not representable.
    unsupported = next(
        (char for char in service_key if not "\x21" <= char <= "\x7e"), ""
    )
    if unsupported:
        name = unicodedata.name(unsupported, "an unsupported character")
        raise ValueError(
            f"The service API key contains {name} (U+{ord(unsupported):04X}). "
            "It may have picked up a look-alike character when copied; "
            "try retyping it."
        )

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

    # Name each target after its role so the gateway config is stable no matter
    # which slot a role occupies, and predictable without copying JSON around.
    for role in roles:
        target_name = f"{role['id']}_route"
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
            "default": targets[0]["name"],
        },
        "targets": targets,
    }


def quote_environment_value(value: str) -> str:
    """Encode exactly as load_environment_file decodes.

    json.dumps escapes non-ASCII, so line separators such as U+2028 are written
    as text rather than as characters that would split the line on reload.
    """
    return json.dumps(value)


def render_environment(
    service_key: str, provider_slug: str, roles: list[dict[str, str]]
) -> str:
    values = [
        ("PORTKEY_API_KEY", service_key),
        ("PORTKEY_GATEWAY_URL", GATEWAY_URL),
        ("PORTKEY_PROVIDER_SLUG", provider_slug),
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
        f"{key}={quote_environment_value(value)}\n" for key, value in values
    )


def overwrite_state_file(path: Path, content: str) -> None:
    """Replace a state file atomically with owner-only permissions.

    Writing a sibling temporary file and renaming it means a failed or
    interrupted save never leaves a half-written file behind, and the rename
    replaces any symlink at the destination instead of writing through it.
    """
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_path = tempfile.mkstemp(
        dir=directory, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        os.unlink(temporary_path)
        raise

    directory_descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def save_onboarding(
    service_key: str, provider_slug: str, roles: list[dict[str, str]]
) -> dict[str, Any]:
    global PORTKEY_API_KEY, PROVIDER_SLUG, ROLES

    routing_config = build_routing_config(provider_slug, roles)
    environment = render_environment(service_key, provider_slug, roles)

    with CONFIG_LOCK:
        # .env governs behaviour, the routing JSON is a paste-able artifact, so
        # persist the credential first and only adopt the config once both land.
        overwrite_state_file(ENV_FILE, environment)
        overwrite_state_file(
            ROUTING_CONFIG_PATH,
            json.dumps(routing_config, indent=2) + "\n",
        )

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

        # Authorize on whether the key actually works, not on whether it matches
        # the one already stored. Matching the stored value made key rotation
        # impossible: changing a rotated key required knowing the old one.
        key_status = verify_service_key(service_key)
        if key_status == "rejected":
            self._json(
                HTTPStatus.FORBIDDEN,
                {"error": "The AI gateway rejected this service API key."},
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

        global KEY_STATUS
        with CONFIG_LOCK:
            KEY_STATUS = key_status

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

    if PORTKEY_API_KEY:
        # Re-check a key loaded from .env off the request path, so a slow or
        # unreachable gateway never delays the first page load.
        threading.Thread(target=refresh_key_status, daemon=True).start()

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
