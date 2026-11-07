"""Recorded provider calls with explicit identities and no hidden retries."""

from datetime import datetime, timezone
import json
import math
from email.utils import parsedate_to_datetime
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from dotenv import dotenv_values

from .models import MODEL_SPECS, validate_resolved_identity
from .ogx import validate_url


class ProviderError(RuntimeError):
    """A provider call failed; retain safe HTTP metadata, not response bodies."""

    def __init__(self, message, *, http_status=None, request_id=None, retry_after=None):
        super().__init__(message)
        self.http_status = http_status
        self.request_id = request_id
        self.retry_after = retry_after


def _roster_variables() -> set[str]:
    """Environment variable names the roster reads for endpoints and keys."""
    return {
        spec[field]
        for spec in MODEL_SPECS.values()
        for field in ("api_key_env", "base_url_env", "model_env")
        if spec.get(field)
    } | {"OLLAMA_BASE_URL"}


def load_credentials(path: Path | None = None) -> None:
    """Load roster-referenced variables from a dotenv file without running shell code.

    Values already exported in the environment take precedence.
    """
    location = path or os.environ.get("AGENTIC_ENV_FILE")
    if not location:
        return
    if not Path(location).is_file():
        raise ProviderError("Configured credentials file is unavailable")
    values = dotenv_values(location, interpolate=False)
    for name in _roster_variables():
        value = values.get(name)
        if not value or name in os.environ:
            continue
        if any(character in value for character in ("\n", "\r", "\0")):
            raise ProviderError(f"{name} contains invalid control characters")
        os.environ[name] = value


def request_json(url: str, body=None, *, key="", timeout=120):
    """Make one JSON request; never include credentials or error bodies in errors."""
    try:
        url = validate_url(url)
    except ValueError:
        raise ProviderError("Invalid provider endpoint") from None
    if any(character in key for character in ("\n", "\r", "\0")):
        raise ProviderError("Credential contains invalid control characters")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    request = Request(
        url,
        data=None if body is None else json.dumps(body).encode(),
        headers=headers,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as exc:
        headers = exc.headers or {}
        request_id = headers.get("x-request-id") or headers.get("x-correlation-id")
        retry_after = None
        raw_delay = headers.get("Retry-After")
        if raw_delay:
            try:
                retry_after = float(raw_delay)
            except ValueError:
                try:
                    retry_after = (
                        parsedate_to_datetime(raw_delay).timestamp() - time.time()
                    )
                except (ValueError, TypeError, OverflowError):
                    pass
            if retry_after is not None:
                retry_after = (
                    max(0, retry_after) if math.isfinite(retry_after) else None
                )
        raise ProviderError(
            f"Provider returned HTTP {exc.code}",
            http_status=exc.code,
            request_id=request_id,
            retry_after=retry_after,
        ) from None
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise ProviderError(
            f"Provider transport/JSON failure: {type(exc).__name__}"
        ) from None


def endpoint(model_key: str) -> tuple[str, str, str]:
    """Return explicit request model, endpoint and credential variable name."""
    spec = MODEL_SPECS[model_key]
    model = os.environ.get(spec["model_env"], "") if spec.get("model_env") else ""
    model = model or spec.get("model")
    if not model:
        raise ProviderError(f"{model_key}: set {spec['model_env']}")
    key_env = spec.get("api_key_env") or "AGENTIC_NO_API_KEY"
    if spec.get("api_key_env") and not os.environ.get(key_env):
        raise ProviderError(f"{model_key}: {key_env} is missing")
    if spec["provider"] == "ollama":
        base = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1")
    else:
        base = os.environ.get(spec.get("base_url_env") or "", "") or spec.get("base_url")
        if not base:
            raise ProviderError(f"{model_key}: set {spec.get('base_url_env')}")
    return model, validate_url(base), key_env


def _answer_text(response: dict) -> str:
    """Require actual answer text, excluding empty or reasoning-only responses."""
    try:
        content = response["choices"][0]["message"].get("content")
    except (KeyError, IndexError, TypeError, AttributeError):
        raise ProviderError("Malformed provider completion") from None
    if not isinstance(content, str) or not content.strip():
        raise ProviderError("Provider returned no answer text")
    return content


def preflight_model(model_key: str, *, inference=True) -> dict:
    """Verify catalogue/deployment identity and optionally one minimal completion."""
    spec = MODEL_SPECS[model_key]
    model, base, key_env = endpoint(model_key)
    key = os.environ.get(key_env, "ollama")
    catalogue = request_json(base + "/models", key=key)
    if not isinstance(catalogue, dict) or not isinstance(catalogue.get("data"), list):
        raise ProviderError("Malformed provider model catalogue")
    try:
        ids = {entry["id"] for entry in catalogue["data"]}
    except (KeyError, TypeError):
        raise ProviderError("Malformed provider model catalogue") from None
    if model not in ids:
        raise ProviderError(
            f"Requested {model_key} model is absent from provider catalogue"
        )
    resolved = {
        "provider": spec["provider"],
        "request_model": model,
        "returned_model": model,
        "base_url": base,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "identity_evidence": "Authenticated /models catalogue; weights not independently verifiable",
        "inference_verified": False,
    }
    if spec["provider"] == "ollama":
        native = base.removesuffix("/v1")
        tags = request_json(native + "/api/tags", key=key)
        tag = next((item for item in tags["models"] if item["name"] == model), None)
        if not tag:
            raise ProviderError("Local model is not installed")
        info = request_json(native + "/api/show", {"model": model}, key=key)
        version = request_json(native + "/api/version", key=key).get("version")
        if not isinstance(version, str) or not version.strip():
            raise ProviderError("Ollama serving version metadata is missing")
        resolved.update(
            digest=tag["digest"],
            quantization=info.get("details", {}).get("quantization_level", "unknown"),
            model_format=info.get("details", {}).get("format", "unknown"),
            backend="ollama:" + version,
            execution_engine="unreported",
            template=info.get("template"),
            parameters=info.get("parameters"),
            model_info=info.get("model_info"),
            capabilities=info.get("capabilities"),
            ollama_version=version,
        )
        if "unknown" in (resolved["quantization"], resolved["model_format"]):
            raise ProviderError("Local quantization/model-format metadata is missing")
    if inference:
        response = request_json(
            base + "/chat/completions",
            {
                "model": model,
                "messages": [{"role": "user", "content": "Reply with OK."}],
                "max_tokens": 512,
                **spec.get("role_sampling", {"temperature": 0}),
                "stream": False,
            },
            key=key,
        )
        if not isinstance(response, dict):
            raise ProviderError("Malformed provider completion")
        _answer_text(response)
        resolved["returned_model"] = response.get("model")
        resolved["inference_verified"] = True
        resolved["preflight_usage"] = response.get("usage")
        resolved["identity_evidence"] += "; /chat/completions returned model identity"
        if spec["provider"] == "ollama":
            running = request_json(native + "/api/ps", key=key)
            deployment = next(
                (
                    item
                    for item in running.get("models", [])
                    if item.get("name") == model
                ),
                {},
            )
            resolved["context_length"] = deployment.get("context_length")
            resolved["loaded_size_vram"] = deployment.get("size_vram")
    try:
        validate_resolved_identity(spec, resolved)
    except ValueError:
        raise ProviderError("Provider model identity verification failed") from None
    return resolved


def chat_generator(
    model_key,
    resolved,
    recorder,
    *,
    role,
    max_tokens=4096,
    timeout=300,
    retry_delays=(2, 5),
    before_call=None,
    activity=None,
):
    """Build a native Garak generator that records each attacker/judge call."""
    if role not in MODEL_SPECS[model_key]["roles"]:
        raise ProviderError("Model is not configured for this evaluation role")
    try:
        validate_resolved_identity(MODEL_SPECS[model_key], resolved)
    except ValueError:
        raise ProviderError("Provider model identity verification failed") from None
    sampling = dict(MODEL_SPECS[model_key].get("role_sampling", {"temperature": 0}))
    from garak.attempt import Message
    from garak.generators.base import Generator

    class RecordedChatGenerator(Generator):
        """Chat completions adapter with recorded, budgeted transient HTTP retries."""

        generator_family_name = "agentic-evaluation"
        parallel_capable = False

        def __init__(self):
            self.seed = None
            self.parallel_requests = 1
            self.max_workers = 1
            super().__init__(name=resolved["request_model"], config_root={})

        def _call_model(self, prompt, generations_this_call=1):
            if generations_this_call != 1:
                raise ValueError("Evaluation role calls require one generation")
            model, base, key_env = endpoint(model_key)
            if model != resolved["request_model"] or base != resolved["base_url"]:
                raise ProviderError("Provider configuration changed after preflight")
            messages = [
                {"role": turn.role, "content": turn.content.text}
                for turn in prompt.turns
            ]
            call_id = uuid4().hex
            for attempt_index in range(len(retry_delays) + 1):
                if before_call:
                    before_call()
                event = {
                    "event_id": uuid4().hex,
                    "call_id": call_id,
                    "attempt_number": attempt_index + 1,
                    "timestamp": time.time(),
                    "role": role,
                    "event_type": "role_model_call",
                    "visibility": "online_observation",
                    "model": model,
                    "payload": {
                        "messages": messages,
                        "max_tokens": max_tokens,
                        **sampling,
                        "stream": False,
                    },
                }
                delay = None
                started = time.monotonic()
                if activity:
                    activity("waiting")
                try:
                    response = request_json(
                        base + "/chat/completions",
                        {
                            "model": model,
                            "messages": messages,
                            "max_tokens": max_tokens,
                            **sampling,
                            "stream": False,
                        },
                        key=os.environ.get(key_env, "ollama"),
                        timeout=timeout,
                    )
                    if not isinstance(response, dict):
                        raise ProviderError("Malformed provider response")
                    # Preserve finish reasons and usage even if validation rejects
                    # an empty answer or a response from the wrong model.
                    event["response"] = {
                        field: response[field]
                        for field in ("id", "model", "created", "choices", "usage")
                        if field in response
                    }
                    if response.get("model") != resolved["returned_model"]:
                        raise ProviderError(
                            "Provider returned a different model after preflight"
                        )
                    if any(
                        choice.get("finish_reason") == "length"
                        for choice in response.get("choices", [])
                    ):
                        raise ProviderError(
                            f"{role} response truncated at output-token limit {max_tokens}; "
                            "incomplete plans or judgments cannot be used"
                        )
                    content = _answer_text(response)
                    event.update(status="completed")
                    return [Message(text=content)]
                except (ProviderError, KeyError, IndexError, TypeError) as exc:
                    message = (
                        str(exc)
                        if isinstance(exc, ProviderError)
                        else "Malformed provider response"
                    )
                    http_status = getattr(exc, "http_status", None)
                    if http_status in {429, 500, 502, 503, 504} and attempt_index < len(
                        retry_delays
                    ):
                        delay = max(
                            retry_delays[attempt_index],
                            getattr(exc, "retry_after", None) or 0,
                        )
                        if delay > 30:
                            delay = None
                    event.update(
                        status="retry_scheduled"
                        if delay is not None
                        else "provider_failure",
                        error=type(exc).__name__,
                        error_message=message,
                        http_status=http_status,
                        provider_request_id=getattr(exc, "request_id", None),
                        retry_delay_seconds=delay,
                    )
                    if delay is None:
                        raise ProviderError(message, http_status=http_status) from None
                finally:
                    event["elapsed_seconds"] = time.monotonic() - started
                    recorder(event)
                    if activity:
                        activity("retrying" if delay is not None else "idle")

                time.sleep(delay)

    return RecordedChatGenerator()
