from __future__ import annotations

import base64
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import requests
from requests.auth import HTTPBasicAuth
from fastapi import HTTPException

from logging_utils import get_logger


TOKEN_REFRESH_BUFFER_SECONDS = 240
TOKEN_LIFETIMES = {
    "aruba": timedelta(hours=2),
    "greenlake": timedelta(minutes=15),
}
TOKEN_FILES = {
    "aruba": "aruba.txt",
    "greenlake": "greenlake.txt",
}
TOKEN_ALIASES = {
    "aruba": "central",
    "greenlake": "greenlake",
}
PROVIDER_CONFIG_FIELDS = {
    "aruba": ("token_url", "client_id", "client_secret"),
    "greenlake": ("token_url", "client_id", "client_secret"),
}

BASE_DIR = Path(__file__).resolve().parents[1]
TOKEN_DIR = BASE_DIR / "AccessTokens"

logger = get_logger(__name__)
_locks = {provider: threading.Lock() for provider in TOKEN_FILES}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_epoch() -> int:
    return int(_utc_now().timestamp())





def _token_file(provider: str) -> Path:
    normalized = _normalize_provider(provider)
    return TOKEN_DIR / TOKEN_FILES[normalized]


def _normalize_provider(provider: str) -> str:
    value = str(provider or "").strip().lower()
    if value in TOKEN_FILES:
        return value
    raise HTTPException(status_code=400, detail=f"Unsupported token provider: {provider}")


def _parse_iso_utc(value: str) -> datetime | None:
    raw_value = str(value or "").strip()
    if not raw_value:
        return None

    try:
        if raw_value.endswith("Z"):
            raw_value = raw_value[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw_value)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)





def _decode_jwt_expiry(access_token: str) -> int | None:
    token = str(access_token or "").strip()
    if not token:
        return None

    parts = token.split(".")
    if len(parts) < 2:
        return None

    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode((payload + padding).encode("ascii"))
        payload_json = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None

    expiry = payload_json.get("exp")
    try:
        return int(expiry)
    except (TypeError, ValueError):
        return None


def _provider_config(payload: dict[str, Any], provider: str) -> dict[str, str]:
    provider = _normalize_provider(provider)
    required_fields = PROVIDER_CONFIG_FIELDS[provider]
    config: dict[str, str] = {}

    for field_name in required_fields:
        value = str(payload.get(field_name) or "").strip()
        if not value:
            raise HTTPException(
                status_code=400,
                detail=f"{field_name} is missing for {provider}.",
            )
        config[field_name] = value

    return config


def _read_json_file(file_path: Path) -> dict[str, Any]:
    if not file_path.exists():
        return {}

    try:
        raw_text = file_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Unable to read token file: {file_path.name}",
        ) from exc

    if not raw_text:
        return {}

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Token file is not valid JSON: {file_path.name}",
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail=f"Token file must contain a JSON object: {file_path.name}",
        )

    return payload


def _normalize_record(provider: str, payload: dict[str, Any], fallback_access_token: str | None = None) -> dict[str, Any]:
    provider = _normalize_provider(provider)
    access_token = str(payload.get("access_token") or fallback_access_token or "").strip()
    if not access_token:
        raise HTTPException(
            status_code=400,
            detail=f"Access token is missing for {provider}.",
        )

    refresh_token = str(payload.get("refresh_token") or "").strip() or None
    generated_at_utc = str(payload.get("generated_at_utc") or "").strip()
    generated_dt = _parse_iso_utc(generated_at_utc)

    expires_at_epoch = payload.get("expires_at_epoch")
    try:
        expires_at_epoch_int = int(expires_at_epoch) if expires_at_epoch not in (None, "") else None
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"expires_at_epoch is invalid for {provider}.",
        ) from exc

    decoded_expires_at_epoch = _decode_jwt_expiry(access_token)
    if decoded_expires_at_epoch is not None:
        expires_at_epoch_int = decoded_expires_at_epoch

    if expires_at_epoch_int is None:
        if generated_dt is None:
            generated_dt = _utc_now()
        expires_at_epoch_int = int((generated_dt + TOKEN_LIFETIMES[provider]).timestamp())

    if expires_at_epoch_int is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unable to determine token expiry for {provider}.",
        )

    refresh_before_epoch_int = max(0, expires_at_epoch_int - TOKEN_REFRESH_BUFFER_SECONDS)

    if decoded_expires_at_epoch is not None or not generated_at_utc:
        generated_dt = datetime.fromtimestamp(
            max(0, expires_at_epoch_int - int(TOKEN_LIFETIMES[provider].total_seconds())),
            tz=timezone.utc,
        )
        generated_at_utc = generated_dt.isoformat().replace("+00:00", "Z")

    provider_config = _provider_config(payload, provider)

    normalized = {
        "provider": provider,
        **provider_config,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "generated_at_utc": generated_at_utc,
        "expires_at_epoch": expires_at_epoch_int,
        "refresh_before_epoch": refresh_before_epoch_int,
    }
    return normalized


def _normalized_record_needs_persisting(payload: dict[str, Any], normalized: dict[str, Any], provider: str) -> bool:
    provider = _normalize_provider(provider)
    keys_to_check = [
        "provider",
        *PROVIDER_CONFIG_FIELDS[provider],
        "access_token",
        "refresh_token",
        "generated_at_utc",
        "expires_at_epoch",
        "refresh_before_epoch",
    ]

    for key in keys_to_check:
        payload_value = payload.get(key)
        normalized_value = normalized.get(key)

        if key in {"expires_at_epoch", "refresh_before_epoch"}:
            try:
                payload_value = int(payload_value)
            except (TypeError, ValueError):
                payload_value = None

        if str(payload_value or "").strip() != str(normalized_value or "").strip():
            return True

    return False


def _raise_provider_http_error(response: requests.Response, provider_name: str, context: str) -> None:
    if response.status_code == 401:
        logger.error("%s request failed while %s with HTTP %s", provider_name, context, response.status_code)
        raise HTTPException(
            status_code=401,
            detail=f"{provider_name} rejected the token while {context}.",
        )

    if response.status_code == 403:
        logger.error("%s request failed while %s with HTTP %s", provider_name, context, response.status_code)
        raise HTTPException(
            status_code=403,
            detail=f"{provider_name} denied access while {context}.",
        )

    if response.status_code >= 400:
        logger.error("%s request failed while %s with HTTP %s", provider_name, context, response.status_code)
        raise HTTPException(
            status_code=response.status_code,
            detail=f"Failed while {context}: {response.text[:500]}",
        )


def _response_preview(response: requests.Response | None) -> str:
    if response is None:
        return ""

    body = ""
    try:
        body = response.text or ""
    except Exception:
        body = ""

    if not body:
        return ""

    return body[:500]


def _build_record_from_token_response(
    provider: str,
    access_token: str,
    refresh_token: str | None = None,
    source_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    provider = _normalize_provider(provider)
    generated_dt = _utc_now()
    expires_at_epoch = int((generated_dt + TOKEN_LIFETIMES[provider]).timestamp())
    source_record = source_record or {}
    return {
        "provider": provider,
        **{field: str(source_record.get(field) or "").strip() for field in PROVIDER_CONFIG_FIELDS[provider]},
        "access_token": access_token.strip(),
        "refresh_token": str(refresh_token or "").strip() or None,
        "generated_at_utc": generated_dt.isoformat().replace("+00:00", "Z"),
        "expires_at_epoch": expires_at_epoch,
        "refresh_before_epoch": max(0, expires_at_epoch - TOKEN_REFRESH_BUFFER_SECONDS),
    }


def refresh_aruba_token_record(record: dict[str, Any]) -> dict[str, Any]:
    refresh_token = str(record.get("refresh_token") or "").strip()
    if not refresh_token:
        raise HTTPException(
            status_code=400,
            detail="Aruba refresh token is missing from aruba.txt.",
        )

    config = _provider_config(record, "aruba")

    logger.info("Refreshing Aruba token using the stored refresh token")
    request_variants = [
        {
            "name": "query params",
            "kwargs": {
                "params": {
                    "client_id": config["client_id"],
                    "client_secret": config["client_secret"],
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                }
            },
        },
        {
            "name": "form body",
            "kwargs": {
                "data": {
                    "client_id": config["client_id"],
                    "client_secret": config["client_secret"],
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                }
            },
        },
        {
            "name": "basic auth + body",
            "kwargs": {
                "data": {
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                "auth": HTTPBasicAuth(config["client_id"], config["client_secret"]),
            },
        },
    ]

    response = None
    last_error_preview = ""
    for attempt_index, variant in enumerate(request_variants, start=1):
        logger.info("Aruba token refresh attempt %s using %s", attempt_index, variant["name"])
        response = requests.post(
            config["token_url"],
            headers={
                "accept": "application/json",
                "content-type": "application/x-www-form-urlencoded",
            },
            timeout=30,
            **variant["kwargs"],
        )

        if response.status_code < 400:
            break

        last_error_preview = _response_preview(response)
        logger.warning(
            "Aruba token refresh attempt %s failed with HTTP %s: %s",
            attempt_index,
            response.status_code,
            last_error_preview or response.reason,
        )

        if attempt_index < len(request_variants):
            continue

    if response is None:
        raise HTTPException(
            status_code=500,
            detail="Aruba token refresh did not return any response.",
        )

    _raise_provider_http_error(response, "Aruba Central", "refreshing the Aruba token")

    try:
        payload = response.json()
    except ValueError as exc:
        logger.error("Aruba token refresh response was not valid JSON")
        raise HTTPException(
            status_code=500,
            detail="Aruba token refresh response was not valid JSON.",
        ) from exc

    access_token = str(payload.get("access_token") or "").strip()
    new_refresh_token = str(payload.get("refresh_token") or "").strip()
    if not access_token or not new_refresh_token:
        raise HTTPException(
            status_code=500,
            detail=f"Aruba token refresh response did not include the new access and refresh tokens. {last_error_preview}".strip(),
        )

    logger.info("Aruba token refresh succeeded; a new token pair was received")
    return _build_record_from_token_response("aruba", access_token, new_refresh_token, source_record=record)


def refresh_greenlake_token_record(record: dict[str, Any] | None = None) -> dict[str, Any]:
    record = record or {}
    config = _provider_config(record, "greenlake")

    logger.info("Refreshing GreenLake token using client credentials")

    request_variants = [
        {
            "name": "form-body client_id/client_secret",
            "kwargs": {
                "data": {
                    "grant_type": "client_credentials",
                    "client_id": config["client_id"],
                    "client_secret": config["client_secret"],
                }
            },
        },
        {
            "name": "basic-auth client_credentials body",
            "kwargs": {
                "data": {
                    "grant_type": "client_credentials",
                },
                "auth": HTTPBasicAuth(config["client_id"], config["client_secret"]),
            },
        },
    ]

    response = None
    last_error_preview = ""
    for attempt_index, variant in enumerate(request_variants, start=1):
        logger.info("GreenLake token refresh attempt %s using %s", attempt_index, variant["name"])
        response = requests.post(
            config["token_url"],
            headers={
                "accept": "application/json",
                "content-type": "application/x-www-form-urlencoded",
            },
            timeout=30,
            **variant["kwargs"],
        )

        if response.status_code < 400:
            break

        last_error_preview = _response_preview(response)
        logger.warning(
            "GreenLake token refresh attempt %s failed with HTTP %s: %s",
            attempt_index,
            response.status_code,
            last_error_preview or response.reason,
        )

        if attempt_index < len(request_variants):
            continue

    if response is None:
        raise HTTPException(
            status_code=500,
            detail="GreenLake token refresh did not return any response.",
        )

    _raise_provider_http_error(response, "GreenLake", "refreshing the GreenLake token")

    try:
        payload = response.json()
    except ValueError as exc:
        logger.error("GreenLake token refresh response was not valid JSON")
        raise HTTPException(
            status_code=500,
            detail="GreenLake token refresh response was not valid JSON.",
        ) from exc

    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        raise HTTPException(
            status_code=500,
            detail=f"GreenLake token refresh response did not include an access token. {last_error_preview}".strip(),
        )

    logger.info("GreenLake token refresh succeeded")
    return _build_record_from_token_response("greenlake", access_token, source_record=record)


def load_or_refresh_greenlake_token_record() -> dict[str, Any]:
    provider = "greenlake"
    with _locks[provider]:
        token_file = _token_file(provider)
        payload = _read_json_file(token_file)

        if not payload or not str(payload.get("access_token") or "").strip():
            logger.info("GreenLake token file is empty or missing access token; refreshing now")
            refreshed_record = refresh_greenlake_token_record(payload)
            save_token_record(provider, refreshed_record)
            return refreshed_record

        record = _normalize_record(provider, payload)
        if _normalized_record_needs_persisting(payload, record, provider):
            save_token_record(provider, record)
        if token_refresh_due(record):
            refreshed_record = refresh_greenlake_token_record(record)
            save_token_record(provider, refreshed_record)
            return refreshed_record

        return record


def load_token_record(provider: str, fallback_access_token: str | None = None) -> dict[str, Any]:
    provider = _normalize_provider(provider)
    payload = _read_json_file(_token_file(provider))
    record = _normalize_record(provider, payload, fallback_access_token=fallback_access_token)
    if _normalized_record_needs_persisting(payload, record, provider):
        save_token_record(provider, record)
    return record


def save_token_record(provider: str, record: dict[str, Any]) -> Path:
    provider = _normalize_provider(provider)
    token_file = _token_file(provider)
    token_file.parent.mkdir(parents=True, exist_ok=True)

    normalized = _normalize_record(provider, dict(record))
    serializable = {
        "provider": normalized["provider"],
        **{field: normalized[field] for field in PROVIDER_CONFIG_FIELDS[provider]},
        "access_token": normalized["access_token"],
        "refresh_token": normalized["refresh_token"],
        "generated_at_utc": normalized["generated_at_utc"],
        "expires_at_epoch": normalized["expires_at_epoch"],
        "refresh_before_epoch": normalized["refresh_before_epoch"],
    }
    serializable = {key: value for key, value in serializable.items() if value not in (None, "")}

    temp_path = token_file.with_suffix(token_file.suffix + ".tmp")
    temp_path.write_text(json.dumps(serializable, indent=2), encoding="utf-8")
    temp_path.replace(token_file)

    logger.info("Saved %s token record to %s", provider, token_file)
    return token_file


def token_refresh_due(record: dict[str, Any]) -> bool:
    refresh_before_epoch = record.get("refresh_before_epoch")
    try:
        refresh_before_epoch_int = int(refresh_before_epoch)
    except (TypeError, ValueError):
        return False
    return _utc_epoch() >= refresh_before_epoch_int


def refresh_token_record(
    provider: str,
    refresh_callback: Callable[[dict[str, Any]], dict[str, Any]],
    fallback_access_token: str | None = None,
) -> dict[str, Any]:
    provider = _normalize_provider(provider)
    with _locks[provider]:
        record = load_token_record(provider, fallback_access_token=fallback_access_token)
        if not token_refresh_due(record):
            return record

        logger.info("Refreshing %s token because refresh window has started", provider)
        updated_record = refresh_callback(record)
        normalized = _normalize_record(provider, dict(updated_record))
        save_token_record(provider, normalized)
        return normalized


def get_current_tokens(defaults: dict[str, str] | None = None) -> dict[str, str]:
    defaults = defaults or {}
    aruba_fallback = defaults.get("aruba")

    try:
        aruba_record = refresh_token_record(
            "aruba",
            refresh_aruba_token_record,
            fallback_access_token=aruba_fallback,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Unexpected failure while loading Aruba token.",
        ) from exc

    try:
        greenlake_record = load_or_refresh_greenlake_token_record()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Unexpected failure while loading GreenLake token.",
        ) from exc

    return {
        TOKEN_ALIASES["aruba"]: aruba_record["access_token"],
        TOKEN_ALIASES["greenlake"]: greenlake_record["access_token"],
    }
