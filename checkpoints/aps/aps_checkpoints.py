from datetime import datetime
from collections import Counter
import json
import re
from pathlib import Path
from urllib.parse import quote

import requests
from fastapi import HTTPException

from logging_utils import get_logger
from checkpoints.optimus.optimus_api import (
    fetch_optimus_ap_inventory,
)

KLOUDSPOT_REQUIRED_IPS = {"65.2.93.38", "3.7.1.72"}
BASE_DIR = Path(__file__).resolve().parents[2]
EXECUTIONS_DIR = BASE_DIR / "executions"
API_RESPONSE_FOLDER_NAME = "API response"
AP_API_RESPONSE_FILE_NAME = "AP.json"

logger = get_logger(__name__)
EXPECTED_OLIO_WEBHOOK_URL = "https://nw-monitor-internet-collector.tatacommunications.com:8443/olio-event/event/oemid/HPAruba?source=FMS"


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
            return Path(api_response_dir) / AP_API_RESPONSE_FILE_NAME

    run_id = str(config.get("run_id") or "").strip()
    if not run_id:
        raise HTTPException(status_code=400, detail="Run ID is missing.")

    execution_dir = _find_execution_dir(run_id)
    return execution_dir / API_RESPONSE_FOLDER_NAME / AP_API_RESPONSE_FILE_NAME


def _load_api_response_cache(config: dict) -> dict:
    cache_file = _get_api_response_file_path(config)
    cache = _read_json_file(cache_file, {})
    if isinstance(cache, dict):
        return cache
    return {}


def _save_api_response_cache(config: dict, cache: dict) -> None:
    cache_file = _get_api_response_file_path(config)
    _write_json_file(cache_file, cache)


def _ensure_ap_api_cache(config: dict) -> dict:
    cache = _load_api_response_cache(config)
    if cache.get("bootstrap_complete") is True and isinstance(cache.get("ap_inventory"), list):
        logger.info(
            "AP API cache hit for run_id=%s",
            str(config.get("run_id") or "").strip() or "-",
        )
        return cache

    logger.info(
        "AP API cache miss for run_id=%s; bootstrapping upstream data",
        str(config.get("run_id") or "").strip() or "-",
    )
    return bootstrap_ap_api_responses(config)


def _extract_ap_batch(payload) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if not isinstance(payload, dict):
        return []

    batch = payload.get("aps") or payload.get("items") or payload.get("data") or []
    if isinstance(batch, list):
        return [item for item in batch if isinstance(item, dict)]

    return []


def _bootstrap_visualrf_context(config: dict, headers: dict) -> dict:
    site_name = str(config.get("site_name") or "").strip()
    context = {
        "site_name": site_name,
        "building": None,
        "floors": [],
        "floor_plan_status_by_floor_id": {},
        "floor_ap_locations_by_floor_id": {},
    }

    if not site_name:
        logger.info("Skipping VisualRF bootstrap because site_name is empty")
        return context

    base_url = config["base_url"]
    campus_url = f"{base_url}/visualrf_api/v1/campus"
    logger.info("Fetching VisualRF campus data for site_name=%s", site_name)
    response = requests.get(campus_url, headers=headers, timeout=60)
    _raise_central_response(response, "fetching VisualRF campuses")
    campus_payload = response.json()
    campus_ids = extract_visualrf_campus_ids(campus_payload)
    logger.debug("VisualRF campus lookup returned %s campus id(s)", len(campus_ids))

    for campus_id in campus_ids:
        campus_details_url = f"{base_url}/visualrf_api/v1/campus/{campus_id}"
        response = requests.get(campus_details_url, headers=headers, timeout=60)
        _raise_central_response(response, "fetching VisualRF campus details")
        campus_details = response.json()

        matched_building = find_visualrf_building_by_name(campus_details, site_name)
        if not matched_building:
            continue

        building_id = matched_building["building_id"]
        building_url = f"{base_url}/visualrf_api/v1/building/{building_id}"
        response = requests.get(building_url, headers=headers, timeout=60)
        _raise_central_response(response, "fetching VisualRF building details")
        building_details = response.json()
        floors = extract_visualrf_floors(building_details)
        logger.debug("VisualRF matched building %s with %s floor(s)", building_id, len(floors))

        context["building"] = matched_building
        context["floors"] = floors

        for floor in floors:
            floor_id = floor["floor_id"]
            floor_image_url = f"{base_url}/visualrf_api/v1/floor/{floor_id}/image"
            floor_image_response = requests.get(floor_image_url, headers=headers, timeout=60)
            if floor_image_response.status_code in (401, 403):
                _raise_central_response(floor_image_response, "fetching VisualRF floor plan")
            context["floor_plan_status_by_floor_id"][floor_id] = floor_image_response.status_code

            floor_locations_url = f"{base_url}/visualrf_api/v1/floor/{floor_id}/access_point_location"
            response = requests.get(floor_locations_url, headers=headers, timeout=60)
            _raise_central_response(response, "fetching VisualRF AP locations")
            context["floor_ap_locations_by_floor_id"][floor_id] = response.json()

        break

    return context


def bootstrap_ap_api_responses(config: dict) -> dict:
    base_url = config["base_url"]
    group_name = str(config.get("group_name") or "").strip()
    headers = _get_central_headers(config)
    greenlake_headers = {
        "Authorization": f"Bearer {config['tokens']['greenlake']}",
        "Accept": "application/json",
    }

    cache = {
        "bootstrap_complete": False,
    }

    logger.info(
        "Bootstrapping AP API responses for group=%s site=%s",
        group_name or "-",
        str(config.get("site_name") or "").strip() or "-",
    )

    aps_data = []
    limit = 100
    offset = 0
    while True:
        url = f"{base_url}/monitoring/v2/aps"
        params = {
            "group": group_name,
            "limit": limit,
            "offset": offset,
        }
        response = requests.get(url, headers=headers, params=params, timeout=30)
        _raise_central_response(response, "fetching AP inventory")
        data = response.json()
        aps_batch = _extract_ap_batch(data)
        aps_data.extend(aps_batch)
        logger.debug(
            "Fetched %s AP record(s) from Central at offset=%s",
            len(aps_batch),
            offset,
        )

        if len(aps_batch) < limit:
            break

        offset += limit

    cache["ap_inventory"] = aps_data

    devices = []
    limit = 2000
    offset = 0
    greenlake_devices_url = "https://global.api.greenlake.hpe.com/devices/v1/devices"
    while True:
        params = {"limit": limit, "offset": offset}
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
            batch = payload.get("items") or payload.get("devices") or payload.get("data") or []

        devices.extend(batch)
        logger.debug(
            "Fetched %s GreenLake device record(s) at offset=%s",
            len(batch),
            offset,
        )

        if len(batch) < limit:
            break

        offset += limit

    cache["greenlake_devices"] = [device for device in devices if isinstance(device, dict)]

    subscriptions = []
    limit = 50
    offset = 0
    greenlake_subscriptions_url = "https://global.api.greenlake.hpe.com/subscriptions/v1/subscriptions"
    while True:
        params = {"limit": limit, "offset": offset}
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

        subscriptions.extend(batch)
        logger.debug(
            "Fetched %s GreenLake subscription record(s) at offset=%s",
            len(batch),
            offset,
        )

        if len(batch) < limit:
            break

        offset += limit

    cache["greenlake_subscriptions"] = [subscription for subscription in subscriptions if isinstance(subscription, dict)]

    groups = []
    limit = 100
    offset = 0
    while True:
        url = f"{base_url}/configuration/v2/groups"
        params = {"limit": limit, "offset": offset}
        response = requests.get(url, headers=headers, params=params, timeout=60)
        _raise_central_response(response, "fetching Central groups")
        payload = response.json()

        if isinstance(payload, list):
            batch = payload
        else:
            batch = payload.get("data") or payload.get("items") or payload.get("groups") or []

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
    cache["central_webhooks"] = response.json()
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

    full_wlan_url = f"{base_url}/configuration/full_wlan/{quote(group_name, safe='')}"
    response = requests.get(full_wlan_url, headers=headers, timeout=60)
    _raise_central_response(response, "fetching WLAN configuration")
    cache["full_wlan_text"] = response.text
    logger.info("Fetched WLAN configuration for group=%s", group_name or "-")

    country_url = f"{base_url}/configuration/v1/{quote(group_name, safe='')}/country"
    response = requests.get(country_url, headers=headers, timeout=60)
    _raise_central_response(response, "fetching group country")
    cache["group_country"] = response.json()
    logger.info("Fetched group country settings")

    cli_url = f"{base_url}/configuration/v1/ap_cli/{quote(group_name, safe='')}"
    response = requests.get(cli_url, headers=headers, timeout=60)
    _raise_central_response(response, "fetching AP CLI lines")
    cache["ap_cli_lines"] = response.json()
    logger.info("Fetched AP CLI lines")

    cache["visualrf_context"] = _bootstrap_visualrf_context(config, headers)

    neighbors_by_serial = {}
    for ap in aps_data:
        ap_serial = str(ap.get("serial") or "").strip()
        if not ap_serial:
            continue

        encoded_serial = quote(ap_serial, safe="")
        neighbors_url = f"{base_url}/topology_external_api/apNeighbors/{encoded_serial}"
        response = requests.get(neighbors_url, headers=headers, timeout=60)
        _raise_central_response(response, "fetching AP neighbours")
        neighbors_by_serial[ap_serial] = response.json()
    logger.debug("Fetched AP neighbours for %s AP(s)", len(neighbors_by_serial))

    cache["ap_neighbors_by_serial"] = neighbors_by_serial
    cache["bootstrap_complete"] = True
    _save_api_response_cache(config, cache)
    logger.info("Completed AP API bootstrap for group=%s", group_name or "-")
    return cache


def fetch_ap_inventory(config: dict) -> list[dict]:
    cache = _ensure_ap_api_cache(config)
    ap_inventory = cache.get("ap_inventory")
    if isinstance(ap_inventory, list):
        return [item for item in ap_inventory if isinstance(item, dict)]
    return []


def fetch_central_groups(config: dict) -> list[str]:
    cache = _ensure_ap_api_cache(config)
    central_groups = cache.get("central_groups")
    if isinstance(central_groups, list):
        return [group for group in central_groups if isinstance(group, str)]
    return []


def fetch_greenlake_devices(config: dict) -> list[dict]:
    cache = _ensure_ap_api_cache(config)
    greenlake_devices = cache.get("greenlake_devices")
    if isinstance(greenlake_devices, list):
        return [device for device in greenlake_devices if isinstance(device, dict)]
    return []


def fetch_greenlake_subscriptions(config: dict) -> list[dict]:
    cache = _ensure_ap_api_cache(config)
    greenlake_subscriptions = cache.get("greenlake_subscriptions")
    if isinstance(greenlake_subscriptions, list):
        return [subscription for subscription in greenlake_subscriptions if isinstance(subscription, dict)]
    return []


def fetch_central_webhooks(config: dict):
    cache = _ensure_ap_api_cache(config)
    webhooks = cache.get("central_webhooks")
    if isinstance(webhooks, dict):
        return webhooks
    return {}


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


def fetch_ap_neighbors(config: dict, ap_serial: str):
    cache = _ensure_ap_api_cache(config)
    neighbors_by_serial = cache.get("ap_neighbors_by_serial")
    if not isinstance(neighbors_by_serial, dict):
        return {}
    return neighbors_by_serial.get(str(ap_serial).strip(), {})


def fetch_group_country(config: dict):
    cache = _ensure_ap_api_cache(config)
    country = cache.get("group_country")
    if isinstance(country, dict):
        return country
    return {}


def fetch_ap_cli_lines(config: dict):
    cache = _ensure_ap_api_cache(config)
    cli_lines = cache.get("ap_cli_lines")
    if isinstance(cli_lines, list):
        return cli_lines
    return []


def fetch_full_wlan_text(config: dict) -> str:
    cache = _ensure_ap_api_cache(config)
    wlan_text = cache.get("full_wlan_text")
    if isinstance(wlan_text, str):
        return wlan_text
    return ""


def _get_visualrf_context(config: dict) -> dict | None:
    cache = _ensure_ap_api_cache(config)
    context = cache.get("visualrf_context")
    if isinstance(context, dict):
        return context
    return None


def _walk_nodes(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_nodes(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_nodes(item)


def extract_visualrf_campus_ids(payload) -> list[str]:
    campus_ids = []
    for node in _walk_nodes(payload):
        for key in ("campus_id", "id"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                campus_ids.append(value.strip())

    unique_ids = []
    seen_ids = set()
    for campus_id in campus_ids:
        if campus_id not in seen_ids:
            seen_ids.add(campus_id)
            unique_ids.append(campus_id)
    return unique_ids


def find_visualrf_building_by_name(payload, site_name: str) -> dict | None:
    normalized_site_name = site_name.strip().lower()
    for node in _walk_nodes(payload):
        building_name = node.get("building_name") or node.get("name")
        building_id = node.get("building_id") or node.get("id")
        if (
            isinstance(building_name, str)
            and isinstance(building_id, str)
            and building_name.strip().lower() == normalized_site_name
        ):
            return {
                "building_name": building_name.strip(),
                "building_id": building_id.strip(),
            }
    return None


def extract_visualrf_floors(payload) -> list[dict[str, str]]:
    floors = []
    for node in _walk_nodes(payload):
        floor_id = node.get("floor_id") or node.get("id")
        floor_name = node.get("floor_name") or node.get("name")
        if isinstance(floor_id, str) and floor_id.strip():
            floors.append(
                {
                    "floor_id": floor_id.strip(),
                    "floor_name": (
                        floor_name.strip() if isinstance(floor_name, str) and floor_name.strip()
                        else floor_id.strip()
                    ),
                }
            )

    unique_floors = []
    seen_ids = set()
    for floor in floors:
        if floor["floor_id"] not in seen_ids:
            seen_ids.add(floor["floor_id"])
            unique_floors.append(floor)
    return unique_floors


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






def ap_checkpoint_1_hostname_serial_mapping(config: dict) -> list[dict[str, str | None]]:
    return [
        {
            "name": ap.get("name"),
            "serial": ap.get("serial"),
        }
        for ap in fetch_ap_inventory(config)
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


def _get_optimus_hostname_serial_pairs(config: dict) -> Counter:
    return _normalize_hostname_serial_pairs(
        [
            {
                "name": ap.get("cpeDeviceName"),
                "serial": ap.get("cpeSerialNumber"),
            }
            for ap in fetch_optimus_ap_inventory(config)
        ]
    )


def ap_checkpoint_1_hostname_serial_comparison_result(config: dict) -> str | list[str]:
    central_rows = ap_checkpoint_1_hostname_serial_mapping(config)
    optimus_pairs = _get_optimus_hostname_serial_pairs(config)

    if not central_rows:
        return ["AP Central inventory is empty."]

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
            f'{name_value} / {serial_value} from AP Central inventory is not present in Optimus.'
        )

    if missing_rows:
        return missing_rows

    return "Compliance"






def ap_checkpoint_2_serial_number_verification(config: dict) -> list[dict[str, str | None]]:
    return [{"serial": ap.get("serial")} for ap in fetch_ap_inventory(config)]


def _normalize_serial_value(serial_value: str | None) -> str:
    return str(serial_value or "").strip().upper()


def ap_checkpoint_2_serial_number_comparison_result(config: dict) -> str | list[str]:
    central_serial_rows = ap_checkpoint_2_serial_number_verification(config)
    optimus_serials = {
        _normalize_serial_value(ap.get("cpeSerialNumber"))
        for ap in fetch_optimus_ap_inventory(config)
        if _normalize_serial_value(ap.get("cpeSerialNumber"))
    }

    if not central_serial_rows:
        return ["AP Central inventory is empty."]

    missing_serials = []
    for row in central_serial_rows:
        serial_value = str(row.get("serial") or "").strip()
        if not serial_value:
            continue

        if _normalize_serial_value(serial_value) in optimus_serials:
            continue

        missing_serials.append(
            f"{serial_value} serial number from AP Central inventory is not present in Optimus."
        )

    if missing_serials:
        return missing_serials

    return "Compliance"






def _normalize_radio_name(radio_name: str | None) -> str:
    normalized = (radio_name or "").strip().lower().replace(" ", "")
    normalized = normalized.removeprefix("radio")
    return normalized


def _is_radio_up(radio: dict) -> bool:
    return str(radio.get("status", "")).strip().lower() == "up"


def ap_checkpoint_3_aps_health(config: dict) -> str | list[str]:
    required_radios = {"5ghz", "2.4ghz"}
    unhealthy_aps = []

    for ap in fetch_ap_inventory(config):
        device_name = ap.get("name") or "Unknown device"
        ap_status_is_up = str(ap.get("status", "")).strip().lower() == "up"
        radios = ap.get("radios") or []

        radio_status_map = {}
        for radio in radios:
            normalized_name = _normalize_radio_name(radio.get("radio_name"))
            if normalized_name in required_radios:
                radio_status_map[normalized_name] = _is_radio_up(radio)

        has_required_radios = required_radios.issubset(radio_status_map.keys())
        all_required_radios_up = has_required_radios and all(
            radio_status_map[radio_name] for radio_name in required_radios
        )

        if not ap_status_is_up or not all_required_radios_up:
            unhealthy_aps.append(f"{device_name} is having issue.")

    if unhealthy_aps:
        return unhealthy_aps

    return "Compliance"






def ap_checkpoint_4_note_presence(config: dict) -> str | list[str]:
    aps_without_notes = []

    for ap in fetch_ap_inventory(config):
        device_name = ap.get("name") or "Unknown device"
        notes = ap.get("notes")

        if not str(notes or "").strip():
            aps_without_notes.append(f"{device_name} notes not present")

    if aps_without_notes:
        return aps_without_notes

    return "Compliance"






def get_ap_subscription_detail_rows(config: dict) -> list[dict[str, str]]:
    ap_serials = [
        ap.get("serial")
        for ap in fetch_ap_inventory(config)
        if str(ap.get("serial") or "").strip()
    ]
    target_serials = set(ap_serials)
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
    for serial_number in ap_serials:
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

        subscription_rows.append(
            row.copy()
        )

    return subscription_rows


def get_unique_ap_subscription_keys(config: dict) -> list[str]:
    unique_keys = []
    seen_keys = set()

    for row in get_ap_subscription_detail_rows(config):
        subscription_key = str(row.get("unique_subscription_key") or "").strip()
        if subscription_key and subscription_key not in seen_keys:
            seen_keys.add(subscription_key)
            unique_keys.append(subscription_key)

    return unique_keys


def ap_checkpoint_5_license_subscription_details(config: dict) -> list[dict[str, str]]:
    return get_ap_subscription_detail_rows(config)






def ap_checkpoint_6_subscription_key_tag_mapping(config: dict) -> str | list[str]:
    target_keys = set(get_unique_ap_subscription_keys(config))
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


def ap_checkpoint_7_group_name_site_name_tcl_service_id(config: dict) -> str:
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


def ap_checkpoint_8_unique_site_unique_group_mapping(config: dict) -> str:
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


def ap_checkpoint_9_floor_plan_presence(config: dict) -> str | list[str]:
    visualrf_context = resolve_visualrf_site_building_and_floors(config)
    if not visualrf_context:
        return "Non-Compliance"

    matched_building = visualrf_context["building"]
    floors = visualrf_context["floors"]

    if not matched_building:
        return [f"{visualrf_context['site_name']} building not found in VisualRF."]

    if not floors:
        return [f"{matched_building['building_name']} has no floors in VisualRF."]

    floor_plan_status_by_floor_id = visualrf_context.get("floor_plan_status_by_floor_id") or {}
    missing_floor_plans = []
    for floor in floors:
        floor_plan_status = floor_plan_status_by_floor_id.get(floor["floor_id"])
        if floor_plan_status != 200:
            missing_floor_plans.append(f"{floor['floor_name']} is having Floor Plan missing.")

    if missing_floor_plans:
        return missing_floor_plans

    return "Compliance"


def resolve_visualrf_site_building_and_floors(config: dict) -> dict | None:
    return _get_visualrf_context(config)


def ap_checkpoint_10_floor_wise_ap_count(config: dict):
    visualrf_context = resolve_visualrf_site_building_and_floors(config)
    if not visualrf_context:
        return {
            "errors": ["Site Name is missing in configuration."],
            "floor_rows": [],
            "total_ap_count": 0,
        }

    matched_building = visualrf_context["building"]
    floors = visualrf_context["floors"]

    if not matched_building:
        return {
            "errors": [f"{visualrf_context['site_name']} building not found in VisualRF."],
            "floor_rows": [],
            "total_ap_count": 0,
        }

    if not floors:
        return {
            "errors": [f"{matched_building['building_name']} has no floors in VisualRF."],
            "floor_rows": [],
            "total_ap_count": 0,
        }

    floor_rows = []
    total_ap_count = 0
    floor_ap_locations_by_floor_id = visualrf_context.get("floor_ap_locations_by_floor_id") or {}

    for floor in floors:
        payload = floor_ap_locations_by_floor_id.get(floor["floor_id"]) or {}
        access_points = payload.get("access_points") or []
        access_point_count = payload.get("access_point_count")
        if access_point_count is None:
            access_point_count = len(access_points)
        try:
            access_point_count = int(access_point_count)
        except (TypeError, ValueError):
            access_point_count = len(access_points)

        total_ap_count += access_point_count
        floor_rows.append(
            {
                "floor_name": floor["floor_name"],
                "ap_count": access_point_count,
            }
        )

    return {
        "errors": [],
        "floor_rows": floor_rows,
        "total_ap_count": total_ap_count,
    }


def ap_checkpoint_11_firmware_version_check(config: dict) -> str | list[str]:
    expected_firmware_version = str(
        (config.get("firmware_versions") or {}).get("ap") or ""
    ).strip()

    if not expected_firmware_version:
        return "Non-Compliance"

    mismatched_aps = []
    for ap in fetch_ap_inventory(config):
        current_firmware_version = str(ap.get("firmware_version") or "").strip()
        if current_firmware_version != expected_firmware_version:
            ap_name = str(ap.get("name") or "Unknown AP").strip()
            mismatched_aps.append(f"{ap_name} firmware not matching")

    if mismatched_aps:
        return mismatched_aps

    return "Compliance"


def ap_checkpoint_12_number_of_ssid(config: dict) -> str:
    expected_ssid_count = config.get("ssid_count")
    try:
        expected_ssid_count = int(expected_ssid_count)
    except (TypeError, ValueError):
        return "Non-Compliance"

    wlan_text = fetch_full_wlan_text(config)
    actual_ssid_count = len(re.findall(r'\\"name\\"', wlan_text))

    if actual_ssid_count == expected_ssid_count:
        return "Compliance"

    return "Non-Compliance"


def ap_checkpoint_13_authentication_method_opmode(config: dict) -> list[dict[str, str]]:
    wlan_text = fetch_full_wlan_text(config)
    normalized_text = wlan_text.replace('\\"', '"')
    pattern = re.compile(
        r'"name"\s*:\s*"(?P<name>[^"]+)"[\s\S]*?"opmode"\s*:\s*"(?P<opmode>[^"]+)"',
        re.IGNORECASE,
    )

    rows = []
    for match in pattern.finditer(normalized_text):
        name = match.group("name").strip()
        opmode = match.group("opmode").strip()
        if name and opmode:
            rows.append({"name": name, "opmode": opmode})

    return rows


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


def ap_checkpoint_14_webhook_integration(config: dict) -> str | dict[str, object]:
    payload = fetch_central_webhooks(config)
    return _evaluate_webhook_integration_payload(payload)


def ap_checkpoint_15_speed_duplex(config: dict) -> str | list[str]:
    ap_serials = [
        str(ap.get("serial") or "").strip()
        for ap in fetch_ap_inventory(config)
        if str(ap.get("serial") or "").strip()
    ]

    non_compliant_serials = []
    for ap_serial in ap_serials:
        payload = fetch_ap_neighbors(config, ap_serial)
        neighbors = payload.get("neighbors") or []

        speed_values = [
            str(neighbor.get("speed") or "").strip().lower()
            for neighbor in neighbors
            if str(neighbor.get("serial") or "").strip() == ap_serial
        ]

        if not speed_values:
            non_compliant_serials.append(f"{ap_serial} device is non-compliant")
            continue

        speed_is_compliant = any(
            "1000" in speed_value and "full duplex" in speed_value
            for speed_value in speed_values
        )
        if not speed_is_compliant:
            non_compliant_serials.append(f"{ap_serial} device is non-compliant")

    if non_compliant_serials:
        return non_compliant_serials

    return "Compliance"


def ap_checkpoint_16_country_timezone_review(config: dict) -> dict[str, str]:
    country_payload = fetch_group_country(config)
    cli_payload = fetch_ap_cli_lines(config)

    country_code = str(country_payload.get("country") or "").strip()
    timezone_line = ""

    if isinstance(cli_payload, list):
        for line in cli_payload:
            if isinstance(line, str) and line.strip().lower().startswith("clock timezone"):
                timezone_line = line.strip()
                break

    return {
        "country": country_code,
        "clock_timezone_line": timezone_line,
    }


def ap_checkpoint_17_kloudspot_without_cppm(config: dict) -> str:
    cli_payload = fetch_ap_cli_lines(config)

    cli_text = ""
    if isinstance(cli_payload, list):
        cli_text = "\n".join(str(line) for line in cli_payload)
    else:
        cli_text = str(cli_payload or "")

    if all(required_ip in cli_text for required_ip in KLOUDSPOT_REQUIRED_IPS):
        return "Compliance"

    return "Non-Compliance"
