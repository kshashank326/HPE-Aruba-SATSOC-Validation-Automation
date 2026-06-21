from __future__ import annotations

import json
from pathlib import Path

import requests
from fastapi import HTTPException

from logging_utils import get_logger

BASE_DIR = Path(__file__).resolve().parents[2]
EXECUTIONS_DIR = BASE_DIR / "executions"
API_RESPONSE_FOLDER_NAME = "API response"
OPTIMUS_API_RESPONSE_FILE_NAME = "optimus.json"
OPTIMUS_AUTHORIZATION_VALUE = (
    "Basic b3B0aW11c19kb2N1c2lnbl9hZG1pbjpKREpoSkRBMEpHTnlhR1pOUWxScVozVTVUVWt2"
    "YnpWTVlsVmtaMlZ5T1RSYVFrVnlVRkJ3VFM0M1prTmtWMUJpV0dwbVZpOVNWQzlaTWk1RA=="
)

logger = get_logger(__name__)


def _read_json_file(file_path: Path, default):
    if not file_path.exists():
        return default

    try:
        return json.loads(file_path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def _write_json_file(file_path: Path, data) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(data, indent=2))


def _find_execution_dir(run_id: str) -> Path:
    for config_file in EXECUTIONS_DIR.glob("*/inputs/config.txt"):
        try:
            config = json.loads(config_file.read_text())
        except (OSError, json.JSONDecodeError):
            continue

        if str(config.get("run_id") or "").strip() == run_id:
            return config_file.parent.parent

    raise HTTPException(status_code=400, detail="Run config not found.")


def _get_api_response_file_path(config: dict) -> Path:
    paths = config.get("paths")
    if isinstance(paths, dict):
        api_response_dir = paths.get("api_response_dir")
        if api_response_dir:
            return Path(api_response_dir) / OPTIMUS_API_RESPONSE_FILE_NAME

    run_id = str(config.get("run_id") or "").strip()
    if not run_id:
        raise HTTPException(status_code=400, detail="Run ID is missing.")

    execution_dir = _find_execution_dir(run_id)
    return execution_dir / API_RESPONSE_FOLDER_NAME / OPTIMUS_API_RESPONSE_FILE_NAME


def _load_api_response_cache(config: dict) -> dict:
    cache_file = _get_api_response_file_path(config)
    cache = _read_json_file(cache_file, {})
    if isinstance(cache, dict):
        return cache
    return {}


def _save_api_response_cache(config: dict, cache: dict) -> None:
    cache_file = _get_api_response_file_path(config)
    _write_json_file(cache_file, cache)


def _raise_optimus_response(response: requests.Response, context: str) -> None:
    if response.status_code in (401, 403):
        logger.error(
            "Optimus request failed while %s with HTTP %s",
            context,
            response.status_code,
        )
        raise HTTPException(
            status_code=response.status_code,
            detail=(
                f"Optimus rejected the request while {context}. "
                "Please verify the hardcoded authorization value and service ID."
            ),
        )

    if response.status_code >= 400:
        logger.error(
            "Optimus request failed while %s with HTTP %s",
            context,
            response.status_code,
        )
        raise HTTPException(
            status_code=response.status_code,
            detail=f"Failed while {context}: {response.text[:500]}",
        )


def _extract_optimus_device_list(payload) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if not isinstance(payload, dict):
        return []

    payload_nodes = [payload]
    data_node = payload.get("data")
    if isinstance(data_node, dict):
        payload_nodes.append(data_node)

    for node in payload_nodes:
        for key in ("deviceList", "devices", "items", "data"):
            batch = node.get(key)
            if isinstance(batch, list):
                return [item for item in batch if isinstance(item, dict)]

    return []


def _normalize_device_type(device: dict) -> str:
    return str(device.get("deviceType") or device.get("device_type") or "").strip().lower()


def _ensure_optimus_api_cache(config: dict) -> dict:
    cache = _load_api_response_cache(config)
    service_id = str(config.get("optimus_service_id") or "").strip()
    if (
        cache.get("bootstrap_complete") is True
        and isinstance(cache.get("device_inventory"), list)
        and str(cache.get("optimus_service_id") or "").strip() == service_id
    ):
        logger.info(
            "Optimus API cache hit for run_id=%s",
            str(config.get("run_id") or "").strip() or "-",
        )
        return cache

    logger.info(
        "Optimus API cache miss for run_id=%s; bootstrapping upstream data",
        str(config.get("run_id") or "").strip() or "-",
    )
    return bootstrap_optimus_api_responses(config)


def bootstrap_optimus_api_responses(config: dict) -> dict:
    service_id = str(config.get("optimus_service_id") or "").strip()
    if not service_id:
        raise HTTPException(status_code=400, detail="Optimus service ID is missing.")

    url = f"https://customer.tatacommunications.com/optimus-webhook/v1/mwifi/attributes/{service_id}"
    headers = {
        "Authorization": OPTIMUS_AUTHORIZATION_VALUE,
        "Accept": "application/json",
    }

    logger.info(
        "Bootstrapping Optimus API response for service_id=%s",
        service_id,
    )
    response = requests.get(url, headers=headers, timeout=60)
    _raise_optimus_response(response, "fetching Optimus device inventory")

    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=500,
            detail="Optimus response was not valid JSON.",
        ) from exc

    cache = {
        "bootstrap_complete": True,
        "optimus_service_id": service_id,
        "raw_response": payload,
        "device_inventory": _extract_optimus_device_list(payload),
    }
    _save_api_response_cache(config, cache)
    logger.info(
        "Completed Optimus API bootstrap for service_id=%s with %s device(s)",
        service_id,
        len(cache["device_inventory"]),
    )
    return cache


def fetch_optimus_device_inventory(config: dict) -> list[dict]:
    cache = _ensure_optimus_api_cache(config)
    device_inventory = cache.get("device_inventory")
    if isinstance(device_inventory, list):
        return [device for device in device_inventory if isinstance(device, dict)]
    return []


def fetch_optimus_ap_inventory(config: dict) -> list[dict]:
    ap_devices = []
    for device in fetch_optimus_device_inventory(config):
        device_type = _normalize_device_type(device)
        if (
            "wireless lan ap" in device_type
            or "access point" in device_type
            or "access points" in device_type
        ):
            ap_devices.append(device)
    return ap_devices


def fetch_optimus_switch_inventory(config: dict) -> list[dict]:
    switch_devices = []
    for device in fetch_optimus_device_inventory(config):
        device_type = _normalize_device_type(device)
        if "switch" in device_type:
            switch_devices.append(device)
    return switch_devices
