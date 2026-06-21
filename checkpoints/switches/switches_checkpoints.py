from datetime import datetime
from collections import Counter
import json
import re
from pathlib import Path
from urllib.parse import quote

import requests
from fastapi import HTTPException

from logging_utils import get_logger
from checkpoints.optimus.optimus_api import fetch_optimus_switch_inventory






BASE_DIR = Path(__file__).resolve().parents[2]
EXECUTIONS_DIR = BASE_DIR / "executions"
API_RESPONSE_FOLDER_NAME = "API response"
SWITCH_API_RESPONSE_FILE_NAME = "switch.json"

logger = get_logger(__name__)
EXPECTED_OLIO_WEBHOOK_URL = "https://nw-monitor-internet-collector.tatacommunications.com:8443/olio-event/event/oemid/HPAruba?source=FMS"






def _raise_central_response(response: requests.Response, context: str) -> None:
    if response.status_code == 401:
        logger.error(
            "Aruba Central request failed while %s with HTTP %s",
            context,
            response.status_code,
        )
        raise HTTPException(
            status_code=401,
            detail=(
                f"Aruba Central rejected the token while {context}. "
                "Please update the Central API token in AccessTokens/aruba.txt."
            ),
        )

    if response.status_code == 403:
        logger.error(
            "Aruba Central request failed while %s with HTTP %s",
            context,
            response.status_code,
        )
        raise HTTPException(
            status_code=403,
            detail=(
                f"Aruba Central denied access while {context}. "
                "Please confirm the token and tenant permissions."
            ),
        )

    if response.status_code >= 400:
        logger.error(
            "Aruba Central request failed while %s with HTTP %s",
            context,
            response.status_code,
        )
        raise HTTPException(
            status_code=response.status_code,
            detail=f"Failed while {context}: {response.text[:500]}",
        )


def _raise_greenlake_response(response: requests.Response, context: str) -> None:
    if response.status_code == 401:
        logger.error(
            "GreenLake request failed while %s with HTTP %s",
            context,
            response.status_code,
        )
        raise HTTPException(
            status_code=401,
            detail=(
                f"GreenLake rejected the token while {context}. "
                "Please update the GreenLake API token in AccessTokens/greenlake.txt."
            ),
        )

    if response.status_code == 403:
        logger.error(
            "GreenLake request failed while %s with HTTP %s",
            context,
            response.status_code,
        )
        raise HTTPException(
            status_code=403,
            detail=(
                f"GreenLake denied access while {context}. "
                "Please confirm the token and tenant permissions."
            ),
        )

    if response.status_code >= 400:
        logger.error(
            "GreenLake request failed while %s with HTTP %s",
            context,
            response.status_code,
        )
        raise HTTPException(
            status_code=response.status_code,
            detail=f"Failed while {context}: {response.text[:500]}",
        )


def _get_central_headers(config: dict) -> dict:
    return {
        "Authorization": f"Bearer {config['tokens']['central']}",
        "TenantID": config["tenant_id"],
        "Accept": "application/json",
    }


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
            return Path(api_response_dir) / SWITCH_API_RESPONSE_FILE_NAME

    run_id = str(config.get("run_id") or "").strip()
    if not run_id:
        raise HTTPException(status_code=400, detail="Run ID is missing.")

    execution_dir = _find_execution_dir(run_id)
    return execution_dir / API_RESPONSE_FOLDER_NAME / SWITCH_API_RESPONSE_FILE_NAME


def _load_api_response_cache(config: dict) -> dict:
    cache_file = _get_api_response_file_path(config)
    cache = _read_json_file(cache_file, {})
    if isinstance(cache, dict):
        return cache
    return {}


def _save_api_response_cache(config: dict, cache: dict) -> None:
    cache_file = _get_api_response_file_path(config)
    _write_json_file(cache_file, cache)


def _ensure_switch_api_cache(config: dict) -> dict:
    cache = _load_api_response_cache(config)
    if (
        cache.get("bootstrap_complete") is True
        and isinstance(cache.get("switch_inventory"), list)
        and isinstance(cache.get("switch_ports"), dict)
        and isinstance(cache.get("switch_neighbors"), dict)
    ):
        logger.info(
            "Switch API cache hit for run_id=%s",
            str(config.get("run_id") or "").strip() or "-",
        )
        return cache

    logger.info(
        "Switch API cache miss for run_id=%s; bootstrapping upstream data",
        str(config.get("run_id") or "").strip() or "-",
    )
    return bootstrap_switch_api_responses(config)


def _normalize_switch_record(switch: dict) -> dict:
    labels = switch.get("labels") or []
    normalized_labels = [
        str(label).strip()
        for label in labels
        if str(label or "").strip()
    ]

    return {
        "name": switch.get("name"),
        "serial": switch.get("serial"),
        "status": switch.get("status"),
        "labels": normalized_labels,
        "firmware_version": switch.get("firmware_version"),
        "stack_id": switch.get("stack_id"),
        "site_id": switch.get("site_id"),
    }


def _normalize_port_record(port: dict) -> dict:
    return {
        "port_number": port.get("port_number"),
        "port": port.get("port"),
        "admin_state": port.get("admin_state"),
        "status": port.get("status"),
        "mode": port.get("mode"),
        "duplex_mode": port.get("duplex_mode"),
        "speed": port.get("speed"),
    }


def _normalize_switch_neighbor_record(neighbor: dict) -> dict:
    return {
        "name": neighbor.get("name"),
        "port": neighbor.get("port"),
        "port_number": neighbor.get("port_number"),
    }


def _normalize_greenlake_device(device: dict) -> dict:
    subscriptions = device.get("subscription") or []
    normalized_subscriptions = []

    for subscription in subscriptions:
        if not isinstance(subscription, dict):
            continue
        normalized_subscriptions.append(
            {
                "key": subscription.get("key"),
                "startTime": subscription.get("startTime"),
                "endTime": subscription.get("endTime"),
                "tier": subscription.get("tier"),
            }
        )

    return {
        "serialNumber": device.get("serialNumber"),
        "subscription": normalized_subscriptions,
    }


def _normalize_greenlake_subscription(subscription: dict) -> dict:
    return {
        "key": subscription.get("key"),
        "tags": subscription.get("tags"),
    }





def _normalize_webhook_settings(payload: dict) -> dict:
    settings = payload.get("settings") or []
    if isinstance(settings, dict):
        settings = [settings]

    normalized_settings = []

    for item in settings:
        if not isinstance(item, dict):
            continue

        retry_policy = item.get("retry_policy")
        if isinstance(retry_policy, dict):
            retry_policy = retry_policy.get("policy")

        secure_token = item.get("secure_token") or {}
        if not isinstance(secure_token, dict):
            secure_token = {}

        urls = item.get("urls") or []
        if isinstance(urls, str):
            normalized_urls = [urls.strip()] if urls.strip() else []
        elif isinstance(urls, (list, tuple, set)):
            normalized_urls = [str(url).strip() for url in urls if str(url).strip()]
        else:
            normalized_urls = [str(urls).strip()] if str(urls).strip() else []

        normalized_settings.append(
            {
                "name": item.get("name"),
                "retry_policy": retry_policy,
                "secure_token": {
                    "token": secure_token.get("token"),
                },
                "urls": normalized_urls,
                "wid": item.get("wid"),
            }
        )

    return {"settings": normalized_settings}


def _extract_webhook_ping_status(payload) -> int | None:
    if isinstance(payload, dict):
        if "status" in payload:
            try:
                return int(payload.get("status"))
            except (TypeError, ValueError):
                return None

        result = payload.get("result")
        if isinstance(result, list) and result:
            first_item = result[0]
            if isinstance(first_item, dict):
                try:
                    return int(first_item.get("status"))
                except (TypeError, ValueError):
                    return None

    if isinstance(payload, list) and payload:
        first_item = payload[0]
        if isinstance(first_item, dict):
            try:
                return int(first_item.get("status"))
            except (TypeError, ValueError):
                return None

    return None


def _fetch_webhook_ping_status(base_url: str, headers: dict, wid: str) -> int | None:
    webhook_id = str(wid or "").strip()
    if not webhook_id:
        return None

    ping_url = f"{base_url}/central/v1/webhooks/{quote(webhook_id, safe='')}/ping"
    response = requests.get(ping_url, headers=headers, timeout=60)
    _raise_central_response(response, f"pinging webhook wid={webhook_id}")

    try:
        payload = response.json()
    except ValueError:
        return response.status_code

    ping_status = _extract_webhook_ping_status(payload)
    return ping_status if ping_status is not None else response.status_code





def _normalize_site_details(payload: dict) -> dict:
    site_details = payload.get("site_details") or {}
    if not isinstance(site_details, dict):
        site_details = {}

    return {
        "country": payload.get("country"),
        "site_details": {
            "country": site_details.get("country"),
        },
    }


def _normalize_switch_inventory_payload(payload) -> list[dict]:
    switch_batch = _extract_switch_batch(payload)
    return [
        _normalize_switch_record(switch)
        for switch in switch_batch
        if isinstance(switch, dict)
    ]


def _extract_switch_batch(payload) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if not isinstance(payload, dict):
        return []

    for key in ("switches", "devices", "items", "data"):
        batch = payload.get(key)
        if isinstance(batch, list):
            return [item for item in batch if isinstance(item, dict)]

    return []


def bootstrap_switch_api_responses(config: dict) -> dict:
    base_url = config["base_url"]
    group_name = str(config.get("group_name") or "").strip()
    headers = _get_central_headers(config)

    cache = {
        "bootstrap_complete": False,
    }

    logger.info(
        "Bootstrapping switch API responses for group=%s",
        group_name or "-",
    )

    switches = []
    limit = 100
    offset = 0

    while True:
        params = {
            "group": group_name,
            "limit": limit,
            "offset": offset,
        }

        response = requests.get(
            f"{base_url}/monitoring/v1/switches",
            headers=headers,
            params=params,
            timeout=30,
        )
        _raise_central_response(response, "fetching switch inventory")

        payload = response.json()
        switch_batch = _normalize_switch_inventory_payload(payload)
        switches.extend(switch_batch)
        logger.debug(
            "Fetched %s switch record(s) from Central at offset=%s",
            len(switch_batch),
            offset,
        )

        if len(switch_batch) < limit:
            break

        offset += limit

    cache["switch_inventory"] = switches





    neighbors_by_serial = {}
    ports_by_serial = {}
    seen_serials = set()
    for switch in switches:
        switch_serial = str(switch.get("serial") or "").strip()
        if not switch_serial or switch_serial in seen_serials:
            continue
        seen_serials.add(switch_serial)

        encoded_serial = quote(switch_serial, safe="")
        neighbors_url = f"{base_url}/monitoring/v1/cx_switches/{encoded_serial}/neighbors"
        response = requests.get(
            neighbors_url,
            headers=headers,
            timeout=60,
        )
        _raise_central_response(response, "fetching switch neighbors")
        payload = response.json()

        neighbors = []
        if isinstance(payload, dict):
            neighbors = payload.get("neighbors") or payload.get("items") or payload.get("data") or []
        elif isinstance(payload, list):
            neighbors = payload

        neighbors_by_serial[switch_serial] = [
            _normalize_switch_neighbor_record(neighbor)
            for neighbor in neighbors
            if isinstance(neighbor, dict)
        ]
        logger.debug(
            "Fetched %s neighbor record(s) for switch serial=%s",
            len(neighbors_by_serial[switch_serial]),
            switch_serial,
        )

        ports_url = f"{base_url}/monitoring/v1/cx_switches/{encoded_serial}/ports"
        response = requests.get(
            ports_url,
            headers=headers,
            timeout=60,
        )
        _raise_central_response(response, "fetching switch ports")
        payload = response.json()

        ports = []
        if isinstance(payload, dict):
            ports = payload.get("ports") or payload.get("items") or payload.get("data") or []
        elif isinstance(payload, list):
            ports = payload

        ports_by_serial[switch_serial] = [
            _normalize_port_record(port)
            for port in ports
            if isinstance(port, dict)
        ]
        logger.debug(
            "Fetched %s port record(s) for switch serial=%s",
            len(ports_by_serial[switch_serial]),
            switch_serial,
        )

    cache["switch_neighbors"] = neighbors_by_serial
    cache["switch_ports"] = ports_by_serial







    token = config["tokens"]["greenlake"]
    greenlake_headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    devices = []
    limit = 2000
    offset = 0
    greenlake_devices_url = "https://global.api.greenlake.hpe.com/devices/v1/devices"

    while True:
        params = {
            "limit": limit,
            "offset": offset,
        }

        response = requests.get(
            greenlake_devices_url,
            headers=greenlake_headers,
            params=params,
            timeout=60,
        )
        _raise_greenlake_response(response, "fetching GreenLake devices")
        payload = response.json()

        if isinstance(payload, list):
            batch = payload
        else:
            batch = (
                payload.get("items")
                or payload.get("devices")
                or payload.get("data")
                or []
            )

        devices.extend(
            _normalize_greenlake_device(device)
            for device in batch
            if isinstance(device, dict)
        )
        logger.debug(
            "Fetched %s GreenLake device record(s) at offset=%s",
            len(batch),
            offset,
        )

        if len(batch) < limit:
            break

        offset += limit

    cache["greenlake_devices"] = devices








    subscriptions = []
    limit = 50
    offset = 0
    greenlake_subscriptions_url = "https://global.api.greenlake.hpe.com/subscriptions/v1/subscriptions"

    while True:
        params = {
            "limit": limit,
            "offset": offset,
        }

        response = requests.get(
            greenlake_subscriptions_url,
            headers=greenlake_headers,
            params=params,
            timeout=60,
        )
        _raise_greenlake_response(response, "fetching GreenLake subscriptions")
        payload = response.json()

        if isinstance(payload, list):
            batch = payload
        else:
            batch = (
                payload.get("items")
                or payload.get("subscriptions")
                or payload.get("data")
                or []
            )

        subscriptions.extend(
            _normalize_greenlake_subscription(subscription)
            for subscription in batch
            if isinstance(subscription, dict)
        )
        logger.debug(
            "Fetched %s GreenLake subscription record(s) at offset=%s",
            len(batch),
            offset,
        )

        if len(batch) < limit:
            break

        offset += limit

    cache["greenlake_subscriptions"] = subscriptions







    groups = []
    limit = 100
    offset = 0
    groups_url = f"{base_url}/configuration/v2/groups"

    while True:
        params = {
            "limit": limit,
            "offset": offset,
        }

        response = requests.get(groups_url, headers=headers, params=params, timeout=60)
        _raise_central_response(response, "fetching Central groups")
        payload = response.json()

        if isinstance(payload, list):
            batch = payload
        else:
            batch = (
                payload.get("data")
                or payload.get("items")
                or payload.get("groups")
                or []
            )

        batch_group_names = []
        for item in batch:
            if (
                isinstance(item, list)
                and len(item) == 1
                and isinstance(item[0], str)
                and item[0].strip()
            ):
                batch_group_names.append(item[0].strip())
                continue
            if isinstance(item, str) and item.strip():
                batch_group_names.append(item.strip())
                continue
            if isinstance(item, dict):
                group_name_value = item.get("group") or item.get("group_name") or item.get("name")
                if isinstance(group_name_value, str) and group_name_value.strip():
                    batch_group_names.append(group_name_value.strip())

        groups.extend(batch_group_names)
        logger.debug(
            "Fetched %s Central group record(s) at offset=%s",
            len(batch),
            offset,
        )

        if len(batch) < limit:
            break

        offset += limit

    unique_groups = []
    seen_groups = set()
    for group_name_value in groups:
        if group_name_value not in seen_groups:
            seen_groups.add(group_name_value)
            unique_groups.append(group_name_value)

    cache["central_groups"] = unique_groups






    webhooks_url = f"{base_url}/central/v1/webhooks"
    response = requests.get(webhooks_url, headers=headers, timeout=60)
    _raise_central_response(response, "fetching Central webhooks")
    cache["central_webhooks"] = _normalize_webhook_settings(response.json())
    webhook_settings = cache["central_webhooks"].get("settings") or []
    if isinstance(webhook_settings, list):
        for setting in webhook_settings:
            if not isinstance(setting, dict):
                continue
            setting["ping_status"] = _fetch_webhook_ping_status(
                base_url,
                headers,
                setting.get("wid"),
            )
    logger.info("Fetched Central webhooks settings")






    system_time_url = (
        f"{base_url}/configuration/v1/aos_switch/system_time/groups/"
        f"{quote(group_name, safe='')}"
    )
    response = requests.get(system_time_url, headers=headers, timeout=60)
    _raise_central_response(response, "fetching switch system time")
    payload = response.json()
    cache["switch_system_time"] = {
        "time_zone": str(payload.get("time_zone") or "").strip()
    }
    logger.info("Fetched switch system time settings")







    site_id = ""
    for switch in switches:
        switch_site_id = switch.get("site_id")
        if switch_site_id not in [None, ""]:
            site_id = str(switch_site_id).strip()
            break

    cache["central_site_details"] = {}
    if site_id:
        site_url = f"{base_url}/central/v2/sites/{quote(site_id, safe='')}"
        response = requests.get(site_url, headers=headers, timeout=60)
        _raise_central_response(response, "fetching site details")
        cache["central_site_details"][site_id] = _normalize_site_details(response.json())
        logger.info("Fetched site details for site_id=%s", site_id)





    cache["bootstrap_complete"] = True
    _save_api_response_cache(config, cache)
    logger.info("Completed switch API bootstrap for group=%s", group_name or "-")
    return cache








def fetch_switch_inventory(config: dict) -> list[dict]:
    cache = _ensure_switch_api_cache(config)
    cached_switches = cache.get("switch_inventory")
    if isinstance(cached_switches, list):
        return [item for item in cached_switches if isinstance(item, dict)]
    return []


def _iter_unique_switch_serials(config: dict):
    seen_serials = set()
    for switch in fetch_switch_inventory(config):
        switch_serial = str(switch.get("serial") or "").strip()
        if not switch_serial or switch_serial in seen_serials:
            continue
        seen_serials.add(switch_serial)
        yield switch_serial


def fetch_central_groups(config: dict) -> list[str]:
    cache = _ensure_switch_api_cache(config)
    cached_groups = cache.get("central_groups")
    if isinstance(cached_groups, list):
        return [group for group in cached_groups if isinstance(group, str)]
    return []


def fetch_greenlake_devices(config: dict) -> list[dict]:
    cache = _ensure_switch_api_cache(config)
    cached_devices = cache.get("greenlake_devices")
    if isinstance(cached_devices, list):
        return [device for device in cached_devices if isinstance(device, dict)]
    return []


def fetch_greenlake_subscriptions(config: dict) -> list[dict]:
    cache = _ensure_switch_api_cache(config)
    cached_subscriptions = cache.get("greenlake_subscriptions")
    if isinstance(cached_subscriptions, list):
        return [subscription for subscription in cached_subscriptions if isinstance(subscription, dict)]
    return []


def fetch_central_webhooks(config: dict):
    cache = _ensure_switch_api_cache(config)
    cached_webhooks = cache.get("central_webhooks")
    if isinstance(cached_webhooks, dict):
        return cached_webhooks
    return {"settings": []}


def fetch_switch_system_time(config: dict):
    cache = _ensure_switch_api_cache(config)
    cached_system_time = cache.get("switch_system_time")
    if isinstance(cached_system_time, dict):
        return cached_system_time
    return {"time_zone": ""}


def fetch_central_site_details(config: dict, site_id: str | int):
    cache = _ensure_switch_api_cache(config)
    site_details_cache = cache.get("central_site_details")
    if not isinstance(site_details_cache, dict):
        return {}

    site_cache_key = str(site_id).strip()
    if site_cache_key in site_details_cache and isinstance(site_details_cache[site_cache_key], dict):
        return site_details_cache[site_cache_key]
    return {}


def fetch_switch_ports(config: dict, switch_serial: str) -> list[dict]:
    cache = _ensure_switch_api_cache(config)
    ports_cache = cache.get("switch_ports")
    if not isinstance(ports_cache, dict):
        return []

    switch_cache_key = str(switch_serial).strip()
    cached_ports = ports_cache.get(switch_cache_key)
    if isinstance(cached_ports, list):
        return [port for port in cached_ports if isinstance(port, dict)]
    return []


def fetch_switch_neighbors(config: dict, switch_serial: str) -> list[dict]:
    cache = _ensure_switch_api_cache(config)
    neighbors_cache = cache.get("switch_neighbors")
    if not isinstance(neighbors_cache, dict):
        return []

    switch_cache_key = str(switch_serial).strip()
    cached_neighbors = neighbors_cache.get(switch_cache_key)
    if isinstance(cached_neighbors, list):
        return [neighbor for neighbor in cached_neighbors if isinstance(neighbor, dict)]
    return []


def format_contract_term(start_time: str, end_time: str) -> str:
    if not start_time or not end_time:
        return ""

    try:
        start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
    except ValueError:
        return ""

    contract_days = (end_dt - start_dt).days
    if contract_days < 0:
        return ""

    contract_years = contract_days / 365
    if contract_years.is_integer():
        return f"{int(contract_years)} years"

    return f"{contract_years:.2f} years"














def switch_checkpoint_1_hostname_serial_mapping(config: dict) -> list[dict[str, str | None]]:
    return [
        {
            "name": switch.get("name"),
            "serial": switch.get("serial"),
        }
        for switch in fetch_switch_inventory(config)
    ]


def _normalize_hostname_serial_pairs(rows: list[dict[str, str | None]]) -> Counter:
    normalized_pairs = []
    for row in rows:
        name = str(row.get("name") or "").strip()
        serial = str(row.get("serial") or "").strip()
        if not name or not serial:
            continue
        normalized_pairs.append((name.lower(), serial.upper()))
    return Counter(normalized_pairs)


def _get_optimus_switch_hostname_serial_pairs(config: dict) -> Counter:
    return _normalize_hostname_serial_pairs(
        [
            {
                "name": switch.get("cpeDeviceName"),
                "serial": switch.get("cpeSerialNumber"),
            }
            for switch in fetch_optimus_switch_inventory(config)
        ]
    )


def switch_checkpoint_1_hostname_serial_comparison_result(config: dict) -> str | list[str]:
    central_rows = switch_checkpoint_1_hostname_serial_mapping(config)
    optimus_pairs = _get_optimus_switch_hostname_serial_pairs(config)

    if not central_rows:
        return ["Switch Central inventory is empty."]

    missing_rows = []
    for row in central_rows:
        name_value = str(row.get("name") or "").strip()
        serial_value = str(row.get("serial") or "").strip()
        if not name_value or not serial_value:
            continue

        normalized_pair = (name_value.lower(), serial_value.upper())
        if normalized_pair in optimus_pairs:
            continue

        missing_rows.append(
            f'{name_value} / {serial_value} from Switch Central inventory is not present in Optimus.'
        )

    if missing_rows:
        return missing_rows

    return "Compliance"


def switch_checkpoint_2_serial_number_verification(config: dict) -> list[dict[str, str | None]]:
    return [
        {
            "serial": switch.get("serial"),
        }
        for switch in fetch_switch_inventory(config)
    ]


def _normalize_serial_value(serial_value: str | None) -> str:
    return str(serial_value or "").strip().upper()


def switch_checkpoint_2_serial_number_comparison_result(config: dict) -> str | list[str]:
    central_serial_rows = switch_checkpoint_2_serial_number_verification(config)
    optimus_serials = {
        _normalize_serial_value(switch.get("cpeSerialNumber"))
        for switch in fetch_optimus_switch_inventory(config)
        if _normalize_serial_value(switch.get("cpeSerialNumber"))
    }

    if not central_serial_rows:
        return ["Switch Central inventory is empty."]

    missing_serials = []
    for row in central_serial_rows:
        serial_value = str(row.get("serial") or "").strip()
        if not serial_value:
            continue

        if _normalize_serial_value(serial_value) in optimus_serials:
            continue

        missing_serials.append(
            f"{serial_value} serial number from Switch Central inventory is not present in Optimus."
        )

    if missing_serials:
        return missing_serials

    return "Compliance"


def switch_checkpoint_3_switches_health(config: dict) -> str | list[str]:
    unhealthy_switches = []

    for switch in fetch_switch_inventory(config):
        switch_serial = str(switch.get("serial") or "").strip() or "Unknown serial"
        switch_status_is_up = str(switch.get("status", "")).strip().lower() == "up"

        if not switch_status_is_up:
            unhealthy_switches.append(f"{switch_serial} is having issue.")

    if unhealthy_switches:
        return unhealthy_switches

    return "Compliance"


def switch_checkpoint_4_label_presence(config: dict) -> str | list[str]:
    switches_without_labels = []

    for switch in fetch_switch_inventory(config):
        switch_serial = str(switch.get("serial") or "").strip() or "Unknown serial"
        labels = switch.get("labels") or []
        normalized_labels = [
            str(label).strip()
            for label in labels
            if str(label or "").strip()
        ]

        if not normalized_labels:
            switches_without_labels.append(f"{switch_serial} labels not present")

    if switches_without_labels:
        return switches_without_labels

    return "Compliance"





def get_switch_subscription_detail_rows(config: dict) -> list[dict[str, str]]:
    switch_serials = [
        str(switch.get("serial") or "").strip()
        for switch in fetch_switch_inventory(config)
        if str(switch.get("serial") or "").strip()
    ]
    target_serials = set(switch_serials)
    greenlake_devices = fetch_greenlake_devices(config)
    subscription_by_serial = {}

    for device in greenlake_devices:
        serial_number = str(device.get("serialNumber") or "").strip()
        if not serial_number or serial_number not in target_serials:
            continue

        subscriptions = device.get("subscription") or []
        first_subscription = subscriptions[0] if subscriptions else {}
        subscription_key = str(first_subscription.get("key") or "")
        subscription_start_time = str(first_subscription.get("startTime") or "")
        subscription_end_time = str(first_subscription.get("endTime") or "")
        subscription_by_serial[serial_number] = {
            "serial_number": serial_number,
            "subscription_start_time": subscription_start_time,
            "subscription_end_time": subscription_end_time,
            "subscription_tier": str(first_subscription.get("tier") or ""),
            "subscription_key": subscription_key,
            "contract_term": format_contract_term(
                subscription_start_time,
                subscription_end_time,
            ),
        }

    subscription_rows = []
    seen_subscription_keys = set()
    for serial_number in switch_serials:
        row = subscription_by_serial.get(
            serial_number,
            {
                "serial_number": serial_number,
                "subscription_start_time": "",
                "subscription_end_time": "",
                "subscription_tier": "",
                "subscription_key": "",
                "contract_term": "",
            },
        )
        subscription_key = row.get("subscription_key", "")
        row["unique_subscription_key"] = ""
        if subscription_key and subscription_key not in seen_subscription_keys:
            seen_subscription_keys.add(subscription_key)
            row["unique_subscription_key"] = subscription_key

        subscription_rows.append(row.copy())

    return subscription_rows


def switch_checkpoint_5_license_subscription_details(config: dict) -> list[dict[str, str]]:
    return get_switch_subscription_detail_rows(config)





def get_unique_switch_subscription_keys(config: dict) -> list[str]:
    unique_keys = []
    seen_keys = set()

    for row in get_switch_subscription_detail_rows(config):
        subscription_key = str(row.get("unique_subscription_key") or "").strip()
        if subscription_key and subscription_key not in seen_keys:
            seen_keys.add(subscription_key)
            unique_keys.append(subscription_key)

    return unique_keys


def switch_checkpoint_6_subscription_key_tag_mapping(config: dict) -> str | list[str]:
    target_keys = set(get_unique_switch_subscription_keys(config))
    subscription_records = fetch_greenlake_subscriptions(config)
    tags_by_key = {}

    for subscription in subscription_records:
        subscription_key = str(subscription.get("key") or "").strip()
        if not subscription_key or subscription_key not in target_keys:
            continue
        tags_by_key[subscription_key] = subscription.get("tags") or {}

    non_compliant_keys = []
    for subscription_key in sorted(target_keys):
        if not tags_by_key.get(subscription_key):
            non_compliant_keys.append(subscription_key)

    if non_compliant_keys:
        return non_compliant_keys

    return "Compliance"





def switch_checkpoint_7_group_name_site_name_tcl_service_id(config: dict) -> str:
    group_name = str(config.get("group_name") or "")
    site_name = str(config.get("site_name") or "")
    optimus_service_id = str(config.get("optimus_service_id") or "").strip()

    if (
        optimus_service_id
        and optimus_service_id in group_name
        and optimus_service_id in site_name
    ):
        return "Compliance"

    return "Non-Compliance"


def switch_checkpoint_8_unique_site_unique_group_mapping(config: dict) -> str:
    group_names = fetch_central_groups(config)
    optimus_service_id = str(config.get("optimus_service_id") or "").strip()

    if not optimus_service_id:
        return "Non-Compliance"

    matching_groups = [
        group_name for group_name in group_names if optimus_service_id in group_name
    ]

    if len(matching_groups) == 1:
        return "Compliance"

    return "Non-Compliance"


def switch_checkpoint_9_firmware_version_check(config: dict) -> str | list[str]:
    expected_firmware_version = str(
        (config.get("firmware_versions") or {}).get("switch") or ""
    ).strip()

    if not expected_firmware_version:
        return "Non-Compliance"

    mismatched_switches = []
    for switch in fetch_switch_inventory(config):
        current_firmware_version = str(switch.get("firmware_version") or "").strip()
        if current_firmware_version != expected_firmware_version:
            switch_name = str(switch.get("name") or "Unknown Switch").strip()
            mismatched_switches.append(f"{switch_name} firmware not matching")

    if mismatched_switches:
        return mismatched_switches

    return "Compliance"


def switch_checkpoint_10_stacked_and_standalone_switches_check(config: dict) -> str:
    expected_counts = config.get("switch_counts") or {}

    try:
        expected_stacked = int(expected_counts.get("stacked"))
        expected_standalone = int(expected_counts.get("standalone"))
    except (TypeError, ValueError):
        return "Number of Stacked and Standalone Switches Miss Matched"

    actual_stacked = 0
    actual_standalone = 0

    for switch in fetch_switch_inventory(config):
        stack_id = switch.get("stack_id")
        if stack_id in [None, ""]:
            actual_standalone += 1
        else:
            actual_stacked += 1

    if (
        actual_stacked == expected_stacked
        and actual_standalone == expected_standalone
    ):
        return "Compliance"

    return "Number of Stacked and Standalone Switches Miss Matched"


def switch_checkpoint_11_unused_port(config: dict) -> str | list[str]:
    port_issues = []

    for switch in fetch_switch_inventory(config):
        switch_serial = str(switch.get("serial") or "").strip()
        if not switch_serial:
            continue

        for port in fetch_switch_ports(config, switch_serial):
            admin_state = str(port.get("admin_state") or "").strip()
            status = str(port.get("status") or "").strip()
            if status.lower() != "down":
                continue

            if admin_state.lower() != "down":
                port_number = str(port.get("port_number") or port.get("port") or "").strip()
                port_issues.append(
                    f'Switch serial number "{switch_serial}" port number "{port_number}" is having port status "{status}" but Admin state is "{admin_state}".'
                )

    if port_issues:
        return port_issues

    return "Compliance"





def _extract_webhook_rows(payload: dict) -> list[dict[str, str]]:
    settings = payload.get("settings") or []
    if isinstance(settings, dict):
        settings = [settings]

    rows = []

    for item in settings:
        retry_policy = item.get("retry_policy")

        secure_token = item.get("secure_token") or {}
        if not isinstance(secure_token, dict):
            secure_token = {}

        urls = item.get("urls") or []

        name_value = item.get("name")
        name = "" if name_value is None else str(name_value).strip()
        if isinstance(retry_policy, dict):
            retry_policy_value_raw = retry_policy.get("policy")
        else:
            retry_policy_value_raw = retry_policy
        retry_policy_value = (
            "" if retry_policy_value_raw is None else str(retry_policy_value_raw).strip()
        )
        token_value = secure_token.get("token")
        token = "" if token_value is None else str(token_value).strip()
        wid_value = item.get("wid")
        wid = "" if wid_value is None else str(wid_value).strip()
        ping_status_value = item.get("ping_status")
        try:
            ping_status = int(ping_status_value) if str(ping_status_value).strip() else None
        except (TypeError, ValueError, AttributeError):
            ping_status = None
        if isinstance(urls, str):
            url_values = [urls.strip()] if urls.strip() else []
        elif isinstance(urls, (list, tuple, set)):
            url_values = [str(url).strip() for url in urls if str(url).strip()]
        else:
            url_values = [str(urls).strip()] if str(urls).strip() else []

        rows.append(
            {
                "name": name,
                "retry_policy": retry_policy_value,
                "token": token,
                "urls": ", ".join(url_values),
                "wid": wid,
                "ping_status": ping_status,
            }
        )

    return rows


def _evaluate_webhook_integration_payload(payload: dict) -> str | dict[str, object]:
    rows = _extract_webhook_rows(payload)

    for row in rows:
        if (
            row["name"].lower() == "olio"
            and row["retry_policy"] == "0"
            and row["token"]
            and any(
                url == EXPECTED_OLIO_WEBHOOK_URL
                for url in row["urls"].split(", ")
                if url
            )
            and row["wid"]
            and row["ping_status"] == 200
        ):
            return "Compliance"

    return {
        "result": "Non-Compliance",
        "webhook_rows": rows,
    }


def switch_checkpoint_12_webhook_integration(config: dict) -> str | dict[str, object]:
    payload = fetch_central_webhooks(config)
    return _evaluate_webhook_integration_payload(payload)






def switch_checkpoint_14_country_timezone_review(config: dict) -> dict[str, str]:
    system_time_payload = fetch_switch_system_time(config)
    time_zone = str(system_time_payload.get("time_zone") or "").strip()

    site_id = ""
    for switch in fetch_switch_inventory(config):
        switch_site_id = switch.get("site_id")
        if switch_site_id not in [None, ""]:
            site_id = str(switch_site_id).strip()
            break

    country = ""
    if site_id:
        site_payload = fetch_central_site_details(config, site_id)
        country = str(site_payload.get("country") or "").strip()
        if not country:
            site_details = site_payload.get("site_details") or {}
            country = str(site_details.get("country") or "").strip()

    return {
        "country": country,
        "time_zone": time_zone,
    }


def switch_checkpoint_15_speed_duplex(config: dict) -> str | list[str]:
    port_issues = []

    for switch_serial in _iter_unique_switch_serials(config):
        ports_by_identifier: dict[str, dict] = {}
        for port in fetch_switch_ports(config, switch_serial):
            port_number = str(port.get("port_number") or "").strip()
            port_id = str(port.get("port") or "").strip()
            if port_number:
                ports_by_identifier[port_number] = port
            if port_id and port_id not in ports_by_identifier:
                ports_by_identifier[port_id] = port

        for neighbor in fetch_switch_neighbors(config, switch_serial):
            connected_name = str(neighbor.get("name") or "").strip()
            if not re.search(r"-AP-", connected_name):
                continue

            neighbor_port_raw = str(
                neighbor.get("port_number") or neighbor.get("port") or ""
            ).strip()
            neighbor_port = neighbor_port_raw or "Unavailable"
            port_record = ports_by_identifier.get(neighbor_port_raw)

            duplex_mode = "Unavailable"
            speed = "Unavailable"
            if isinstance(port_record, dict):
                duplex_mode = str(port_record.get("duplex_mode") or "").strip() or "Unavailable"
                speed = str(port_record.get("speed") or "").strip() or "Unavailable"

            if duplex_mode.lower() == "full" and speed == "1000":
                continue

            port_issues.append(
                f'Switch serial "{switch_serial}" port "{neighbor_port}" connected to "{connected_name}" duplex_mode="{duplex_mode}" speed="{speed}"'
            )

    if port_issues:
        return port_issues

    return "Compliance"
