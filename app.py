from datetime import datetime
import os
from pathlib import Path
import shutil
from uuid import uuid4
import html
import json
import logging
import re
from urllib.parse import quote

import requests
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler as fastapi_http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from checkpoints.aps.aps_checkpoints import (
    ap_checkpoint_1_hostname_serial_comparison_result,
    ap_checkpoint_2_serial_number_comparison_result,
    ap_checkpoint_3_aps_health,
    ap_checkpoint_4_note_presence,
    ap_checkpoint_5_license_subscription_details,
    ap_checkpoint_6_subscription_key_tag_mapping,
    ap_checkpoint_7_group_name_site_name_tcl_service_id,
    ap_checkpoint_8_unique_site_unique_group_mapping,
    ap_checkpoint_9_floor_plan_presence,
    ap_checkpoint_10_floor_wise_ap_count,
    ap_checkpoint_11_firmware_version_check,
    ap_checkpoint_12_number_of_ssid,
    ap_checkpoint_13_authentication_method_opmode,
    ap_checkpoint_14_webhook_integration,
    ap_checkpoint_15_speed_duplex,
    ap_checkpoint_16_country_timezone_review,
    ap_checkpoint_17_kloudspot_without_cppm,
)
from checkpoints.optimus.optimus_api import bootstrap_optimus_api_responses
from checkpoints.switches.switches_checkpoints import (
    bootstrap_switch_api_responses,
    switch_checkpoint_1_hostname_serial_comparison_result,
    switch_checkpoint_2_serial_number_comparison_result,
    switch_checkpoint_3_switches_health,
    switch_checkpoint_4_label_presence,
    switch_checkpoint_5_license_subscription_details,
    switch_checkpoint_6_subscription_key_tag_mapping,
    switch_checkpoint_7_group_name_site_name_tcl_service_id,
    switch_checkpoint_8_unique_site_unique_group_mapping,
    switch_checkpoint_9_firmware_version_check,
    switch_checkpoint_10_stacked_and_standalone_switches_check,
    switch_checkpoint_11_unused_port,
    switch_checkpoint_12_webhook_integration,
    switch_checkpoint_14_country_timezone_review,
    switch_checkpoint_15_speed_duplex,
)
from presentation_layer.satsoc_presentation import (
    build_satsoc_presentation_layer,
    sanitize_excel_filename_component,
)
from AccessTokens.logic import get_current_tokens as load_cached_tokens
from logging_utils import (
    configure_logging,
    get_logger,
    reset_current_run_id,
    set_current_run_id,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def normalize_base_route(value: str) -> str:
    route = str(value or "").strip()
    if not route or route == "/":
        return ""
    route = f"/{route.strip('/')}"
    return route.rstrip("/")


BASE_ROUTE = normalize_base_route(os.getenv("BASE_ROUTE", "/M-Wifi-LAN/hpe-aruba"))


def get_public_path(path: str) -> str:
    normalized_path = "/" if not path or path == "/" else f"/{str(path).lstrip('/')}"
    return f"{BASE_ROUTE}{normalized_path}" if BASE_ROUTE else normalized_path


def get_request_path(request: Request) -> str:
    return str(request.scope.get("path") or request.url.path or "/")


def is_api_request(request: Request) -> bool:
    return get_request_path(request).startswith("/api/")


def redirect_to(path: str, status_code: int = 303) -> RedirectResponse:
    return RedirectResponse(get_public_path(path), status_code=status_code)


app = FastAPI(title="SATSOC Validation Tool")
templates = Jinja2Templates(directory="templates")
templates.env.globals["base_route"] = BASE_ROUTE
templates.env.globals["public_path"] = get_public_path
logger = get_logger(__name__)

app.mount("/logos", StaticFiles(directory=BASE_DIR / "logos"), name="logos")
EXECUTIONS_DIR = BASE_DIR / "executions"
BACKUP_REPORTS_DIR = BASE_DIR / "backupReports"
REPORTS_ROUTE = "/reports"
RUN_COOKIE_NAME = "satsoc_run_id"
FLOW_LOCK_COOKIE_NAME = "satsoc_flow_lock"


def get_current_tokens() -> dict[str, str]:
    return load_cached_tokens()

FLOW_ROUTE_ORDER = [
    "/",
    "/step2",
    "/ap/checkpoint1",
    "/ap/checkpoint2",
    "/ap/checkpoint3",
    "/ap/checkpoint4",
    "/ap/checkpoint5",
    "/ap/checkpoint6",
    "/ap/checkpoint7",
    "/ap/checkpoint8",
    "/ap/checkpoint9",
    "/ap/checkpoint10",
    "/ap/checkpoint11",
    "/ap/checkpoint12",
    "/ap/checkpoint13",
    "/ap/checkpoint14",
    "/ap/checkpoint15",
    "/ap/checkpoint16",
    "/ap/checkpoint17",
    "/switch/checkpoint1",
    "/switch/checkpoint2",
    "/switch/checkpoint3",
    "/switch/checkpoint4",
    "/switch/checkpoint5",
    "/switch/checkpoint6",
    "/switch/checkpoint7",
    "/switch/checkpoint8",
    "/switch/checkpoint9",
    "/switch/checkpoint10",
    "/switch/checkpoint11",
    "/switch/checkpoint12",
    "/switch/checkpoint14",
    "/switch/checkpoint15",
    "/validation/complete",
]
FLOW_ROUTE_INDEX = {path: index for index, path in enumerate(FLOW_ROUTE_ORDER)}
FLOW_ROUTE_BY_INDEX = {index: path for path, index in FLOW_ROUTE_INDEX.items()}

EXECUTIONS_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_REPORTS_DIR.mkdir(parents=True, exist_ok=True)






# ERROR HANDLING UTILS

def _build_token_expired_response(request: Request, status_code: int, detail: object = None):
    detail_raw = "" if detail is None else str(detail).strip()
    detail_text = html.escape(detail_raw)

    detail_lower = detail_raw.lower()
    if "greenlake" in detail_lower:
        message = "Oops!!! Seems your GreenLake API access token is expired"
        if status_code == 403:
            message = "Oops!!! Seems your GreenLake API access token is expired or access has been denied"
    elif "central" in detail_lower:
        message = "Oops!!! Seems your Aruba Central API access token is expired"
        if status_code == 403:
            message = "Oops!!! Seems your Aruba Central API access token is expired or access has been denied"
    else:
        message = "Oops!!! Seems your API access token is expired"
        if status_code == 403:
            message = "Oops!!! Seems your API access token is expired or access has been denied"

    if is_api_request(request):
        payload = {"detail": message}
        if detail_text:
            payload["error"] = detail_text
        return JSONResponse(status_code=status_code, content=payload)

    return HTMLResponse(
        content=f"""
        <!doctype html>
        <html lang="en">
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>Access Token Expired</title>
            <style>
                :root {{
                    color-scheme: dark;
                    --bg: #08111d;
                    --panel: #0d1726;
                    --panel-border: #22314a;
                    --text: #f4f7fb;
                    --muted: #a7b4c6;
                    --accent: #5ea1ff;
                }}
                * {{ box-sizing: border-box; }}
                body {{
                    margin: 0;
                    min-height: 100vh;
                    display: grid;
                    place-items: center;
                    background:
                        radial-gradient(circle at top, rgba(94, 161, 255, 0.18), transparent 42%),
                        linear-gradient(180deg, #0a1220 0%, var(--bg) 100%);
                    color: var(--text);
                    font-family: Arial, Helvetica, sans-serif;
                }}
                .card {{
                    width: min(680px, calc(100vw - 32px));
                    padding: 32px 28px;
                    border: 1px solid var(--panel-border);
                    border-radius: 18px;
                    background: rgba(13, 23, 38, 0.94);
                    box-shadow: 0 24px 80px rgba(0, 0, 0, 0.35);
                }}
                .eyebrow {{
                    margin: 0 0 10px;
                    color: var(--accent);
                    font-size: 12px;
                    font-weight: 700;
                    letter-spacing: 0.18em;
                    text-transform: uppercase;
                }}
                h1 {{
                    margin: 0 0 14px;
                    font-size: clamp(24px, 3vw, 38px);
                    line-height: 1.15;
                }}
                p {{
                    margin: 0 0 12px;
                    color: var(--muted);
                    line-height: 1.6;
                    font-size: 15px;
                }}
                .detail {{
                    margin-top: 18px;
                    padding: 14px 16px;
                    border-radius: 12px;
                    background: rgba(255, 255, 255, 0.04);
                    border: 1px solid rgba(255, 255, 255, 0.08);
                    color: #d7e2f1;
                    font-family: Consolas, Menlo, monospace;
                    font-size: 13px;
                    overflow-wrap: anywhere;
                }}
            </style>
        </head>
        <body>
            <main class="card" role="alert" aria-live="assertive">
                <div class="eyebrow">Authentication Error</div>
                <h1>Oops!!! Seems your API access token is expired</h1>
                <p>The current token was rejected by the API while the workflow was running.</p>
                <p>Please refresh the token and start the validation flow again.</p>
                {f'<div class="detail">{detail_text}</div>' if detail_text else ''}
            </main>
        </body>
        </html>
        """.strip(),
        status_code=status_code,
    )


def _build_unexpected_error_response(request: Request):
    detail = "An unexpected error occurred while processing the request."

    if is_api_request(request):
        return JSONResponse(status_code=500, content={"detail": detail})

    return HTMLResponse(
        content=(
            "<h1>Internal Server Error</h1>"
            "<p>An unexpected error occurred while processing the request.</p>"
        ),
        status_code=500,
    )


@app.exception_handler(HTTPException)
async def http_exception_router(request: Request, exc: HTTPException):
    run_id = get_run_id_from_request(request)
    if exc.status_code in (401, 403):
        logger.error(
            "Request failed with HTTP %s on %s: %s",
            exc.status_code,
            request.url.path,
            exc.detail,
            extra={"run_id": run_id or "-"},
        )
    else:
        logger.error(
            "HTTPException on %s: %s",
            request.url.path,
            exc.detail,
            extra={"run_id": run_id or "-"},
        )

    if exc.status_code in (401, 403):
        return _build_token_expired_response(request, exc.status_code, exc.detail)

    return await fastapi_http_exception_handler(request, exc)


@app.exception_handler(requests.HTTPError)
async def requests_http_error_router(request: Request, exc: requests.HTTPError):
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", 500) or 500
    detail_preview = getattr(response, "text", "")[:500] if response is not None else str(exc)

    logger.error(
        "Upstream request failed on %s with HTTP %s: %s",
        request.url.path,
        status_code,
        detail_preview,
        extra={"run_id": get_run_id_from_request(request) or "-"},
    )

    if status_code in (401, 403):
        detail = None
        if response is not None:
            detail = response.text[:500]
        return _build_token_expired_response(request, status_code, detail)

    detail = "The API request failed unexpectedly."
    if response is not None and getattr(response, "text", None):
        detail = response.text[:500]

    if is_api_request(request):
        return JSONResponse(status_code=status_code, content={"detail": detail})

    return HTMLResponse(
        content=f"<h1>Internal Server Error</h1><p>{html.escape(str(detail))}</p>",
        status_code=status_code,
    )


@app.exception_handler(Exception)
async def unhandled_exception_router(request: Request, exc: Exception):
    logger.exception(
        "Unhandled exception on %s %s: %s",
        request.method,
        request.url.path,
        exc,
        extra={"run_id": get_run_id_from_request(request) or "-"},
    )
    return _build_unexpected_error_response(request)






# Core utilities

def sanitize_path_component(value: str, fallback: str = "unknown") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", (value or "").strip())
    cleaned = re.sub(r"\s+", "_", cleaned)
    cleaned = cleaned.strip("._")
    return cleaned or fallback


def create_run_timestamp() -> str:
    return datetime.now().strftime("%d-%m-%Y_%H-%M-%S")


def parse_run_timestamp(timestamp: str) -> datetime:
    timestamp_value = str(timestamp or "").strip()
    for fmt in ("%d-%m-%Y_%H-%M-%S", "%d-%m-%Y"):
        try:
            return datetime.strptime(timestamp_value, fmt)
        except ValueError:
            continue
    return datetime.now()


def create_run_id() -> str:
    return uuid4().hex





#DB Insertion for audit purposes

def insert_audit_db(executed_by: str, service_id: str, network_name: str, org_name: str) -> None:
    db_host = os.getenv("DB_HOST", "localhost")
    db_user = os.getenv("DB_USER", "root")
    db_name = os.getenv("DB_NAME", "sdsatsoc")

    logger.debug(
        "Preparing audit DB insert for executed_by=%s service_id=%s network_name=%s org_name=%s db=%s@%s",
        executed_by or "Unknown",
        service_id or "N/A",
        network_name or "",
        org_name or "",
        db_name,
        db_host,
    )
    try:
        import mysql.connector

        connection = mysql.connector.connect(
            host=db_host,
            user=db_user,
            password=os.getenv("DB_PASSWORD", ""),
            database=db_name,
        )
        cursor = connection.cursor()
        cursor.execute(
            (
                "INSERT INTO satsoc_hpearuba "
                "(Executed_by, Service_id, `NW-ID`, `org-name`) "
                "VALUES (%s, %s, %s, %s)"
            ),
            (
                executed_by or "Unknown",
                service_id or "N/A",
                network_name,
                org_name,
            ),
        )
        connection.commit()
        cursor.close()
        connection.close()

        logger.info(
            "Successfully inserted audit record for %s into DB in the table named satsoc_hpearuba",
            executed_by or "Unknown",
        )
    except Exception as exc:
        logger.exception("DB insert failed: %s", exc)
        logger.error(
            "Continuing workflow despite audit DB insert failure for service_id=%s",
            service_id or "N/A",
        )








# Run/Session/Config Helpers

def get_run_id_from_request(request: Request) -> str | None:
    return request.cookies.get(RUN_COOKIE_NAME)


def get_flow_lock_from_request(request: Request) -> tuple[int, str] | None:
    raw_lock = request.cookies.get(FLOW_LOCK_COOKIE_NAME)
    if not raw_lock:
        return None

    raw_index, _, locked_path = raw_lock.partition(":")
    try:
        locked_index = int(raw_index)
    except ValueError:
        return None

    if FLOW_ROUTE_INDEX.get(locked_path) != locked_index:
        locked_path = FLOW_ROUTE_BY_INDEX.get(locked_index, "")

    if not locked_path:
        return None

    return locked_index, locked_path


def set_flow_lock_cookie(response, path: str) -> None:
    route_index = FLOW_ROUTE_INDEX.get(path)
    if route_index is None:
        return
    response.set_cookie(
        FLOW_LOCK_COOKIE_NAME,
        f"{route_index}:{path}",
        httponly=True,
        samesite="lax",
    )


@app.middleware("http")
async def lock_flow_navigation(request: Request, call_next):
    path = get_request_path(request)
    run_id = request.cookies.get(RUN_COOKIE_NAME)
    request_index = FLOW_ROUTE_INDEX.get(path)
    run_token = set_current_run_id(run_id)

    try:
        if not path.startswith("/logos"):
            logger.info(
                "Request started %s %s",
                request.method,
                path,
                extra={"run_id": run_id or "-"},
            )

        if run_id and request.method == "GET" and request_index is not None and path != "/":
            flow_lock = get_flow_lock_from_request(request)
            if flow_lock:
                locked_index, locked_path = flow_lock
                if locked_index > request_index:
                    logger.info(
                        "Redirected backward navigation from %s to %s",
                        path,
                        locked_path,
                        extra={"run_id": run_id},
                    )
                    response = redirect_to(locked_path, status_code=303)
                    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
                    response.headers["Pragma"] = "no-cache"
                    return response

        response = await call_next(request)

        if not path.startswith("/logos"):
            logger.info(
                "Request completed %s %s -> %s",
                request.method,
                path,
                response.status_code,
                extra={"run_id": run_id or "-"},
            )

        if request.method == "GET" and request_index is not None and response.status_code < 400:
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"

            if path == "/":
                response.delete_cookie(RUN_COOKIE_NAME)
                response.delete_cookie(FLOW_LOCK_COOKIE_NAME)
            elif run_id:
                flow_lock = get_flow_lock_from_request(request)
                locked_index = flow_lock[0] if flow_lock else -1
                if request_index >= locked_index:
                    set_flow_lock_cookie(response, path)

        return response
    finally:
        reset_current_run_id(run_token)


def get_execution_dir_by_run_id(run_id: str) -> Path:
    for config_file in EXECUTIONS_DIR.glob("*/inputs/config.txt"):
        try:
            config = json.loads(config_file.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if str(config.get("run_id") or "").strip() == run_id:
            return config_file.parent.parent
    raise HTTPException(status_code=400, detail="Run config not found.")






configure_logging(get_execution_dir_by_run_id)
logger.info(
    "Logging initialized at level=%s",
    logging.getLevelName(logger.level),
)






def get_run_config_file(run_id: str) -> Path:
    return get_execution_dir_by_run_id(run_id) / "inputs" / "config.txt"


def require_run_id(request: Request) -> str:
    run_id = get_run_id_from_request(request)
    if not run_id:
        raise HTTPException(status_code=400, detail="Run session not found. Start again from Step 1.")
    return run_id


def load_config_from_run_id(run_id: str) -> dict:
    config_file = get_run_config_file(run_id)
    if not config_file.exists():
        raise HTTPException(status_code=400, detail="Run config not found.")
    config = json.loads(config_file.read_text())
    config.pop("paths", None)
    config["tokens"] = get_current_tokens()
    execution_dir = get_execution_dir(config)
    config["paths"] = {
        "execution_dir": str(execution_dir),
        "api_response_dir": str(execution_dir / "API response"),
    }
    return config


def load_config(request: Request) -> dict:
    return load_config_from_run_id(require_run_id(request))


def save_config_for_run(run_id: str, data: dict) -> None:
    config_file = get_run_config_file(run_id)
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_to_save = dict(data)
    config_to_save.pop("paths", None)
    config_to_save.pop("tokens", None)
    config_file.write_text(json.dumps(config_to_save, indent=2))
    logger.info(
        "Saved run configuration to %s",
        config_file,
        extra={"run_id": run_id},
    )


def save_config(request: Request, data: dict) -> None:
    save_config_for_run(require_run_id(request), data)


def write_json_file(file_path: Path, data: dict | list | str) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(data, indent=2))


def read_json_file(file_path: Path, default):
    if not file_path.exists():
        return default
    return json.loads(file_path.read_text())


def start_new_run(username: str, device_scope: str) -> tuple[str, dict]:
    run_timestamp = create_run_timestamp()
    run_id = create_run_id()
    execution_folder_name = f"{sanitize_path_component(username)}_{run_timestamp}"
    execution_dir = EXECUTIONS_DIR / execution_folder_name
    input_dir = execution_dir / "inputs"
    output_dir = execution_dir / "outputs"
    log_dir = execution_dir / "logs"
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    config = {
        "run_id": run_id,
        "username": username.strip(),
        "device_scope": device_scope,
        "run_timestamp": run_timestamp,
    }
    write_json_file(input_dir / "config.txt", config)
    logger.info(
        "Started new run for username=%s device_scope=%s at %s",
        username.strip(),
        device_scope,
        execution_dir,
        extra={"run_id": run_id},
    )
    return run_id, config






# Execution Path Helpers

def get_execution_folder_name(config: dict) -> str:
    username = sanitize_path_component(config.get("username", "unknown_user"))
    customer_name = sanitize_path_component(
        config.get("customer_name") or config.get("tenant_id", "unknown_customer")
    )
    group_name = sanitize_path_component(config.get("group_name", "unknown_group"))
    run_timestamp = config.get("run_timestamp") or create_run_timestamp()
    return f"{username}_{customer_name}_{group_name}_{run_timestamp}"


def get_execution_dir(config: dict) -> Path:
    return EXECUTIONS_DIR / get_execution_folder_name(config)


def get_output_dir(config: dict) -> Path:
    return get_execution_dir(config) / "outputs"


def sync_execution_structure(config: dict) -> Path:
    run_id = str(config.get("run_id") or "").strip()
    if not run_id:
        raise HTTPException(status_code=400, detail="Run ID is missing.")

    current_execution_dir = get_execution_dir_by_run_id(run_id)
    desired_execution_dir = get_execution_dir(config)

    if current_execution_dir != desired_execution_dir:
        if desired_execution_dir.exists():
            raise HTTPException(status_code=400, detail="Target execution directory already exists.")
        logger.info(
            "Renaming execution directory from %s to %s",
            current_execution_dir,
            desired_execution_dir,
            extra={"run_id": run_id},
        )
        current_execution_dir.rename(desired_execution_dir)

    (desired_execution_dir / "inputs").mkdir(parents=True, exist_ok=True)
    (desired_execution_dir / "outputs").mkdir(parents=True, exist_ok=True)
    (desired_execution_dir / "API response").mkdir(parents=True, exist_ok=True)
    (desired_execution_dir / "logs").mkdir(parents=True, exist_ok=True)
    return desired_execution_dir






# Checkpoint Applicability Helpers

def should_run_ap_checkpoints(config: dict) -> bool:
    return config.get("device_scope") in ["ap", "both"]


def should_run_switch_checkpoints(config: dict) -> bool:
    return config.get("device_scope") in ["switch", "both"]






# AP/Switch result persistence functions

def get_ap_result_file_path(config: dict) -> Path | None:
    if not should_run_ap_checkpoints(config):
        return None
    return get_output_dir(config) / "resultAP.txt"


def get_switch_result_file_path(config: dict) -> Path | None:
    if not should_run_switch_checkpoints(config):
        return None
    return get_output_dir(config) / "resultSwitches.txt"


def get_ap_results(config: dict) -> dict:
    ap_result_file = get_ap_result_file_path(config)
    if not ap_result_file:
        return {}
    return read_json_file(ap_result_file, {})


def save_ap_results(config: dict, results: dict) -> None:
    ap_result_file = get_ap_result_file_path(config)
    if ap_result_file:
        write_json_file(ap_result_file, results)


def update_ap_result(config: dict, checkpoint_name: str, value) -> dict:
    results = get_ap_results(config)
    results[checkpoint_name] = value
    save_ap_results(config, results)
    logger.info(
        "AP checkpoint %s result recorded as %s",
        checkpoint_name,
        value,
        extra={"run_id": str(config.get("run_id") or "").strip() or "-"},
    )
    return results


def get_switch_results(config: dict) -> dict:
    switch_result_file = get_switch_result_file_path(config)
    if not switch_result_file:
        return {}
    return read_json_file(switch_result_file, {})


def save_switch_results(config: dict, results: dict) -> None:
    switch_result_file = get_switch_result_file_path(config)
    if switch_result_file:
        write_json_file(switch_result_file, results)


def update_switch_result(config: dict, checkpoint_name: str, value) -> dict:
    results = get_switch_results(config)
    results[checkpoint_name] = value
    save_switch_results(config, results)
    logger.info(
        "Switch checkpoint %s result recorded as %s",
        checkpoint_name,
        value,
        extra={"run_id": str(config.get("run_id") or "").strip() or "-"},
    )
    return results


def initialize_result_files(config: dict) -> None:
    if should_run_ap_checkpoints(config):
        save_ap_results(config, get_ap_results(config))
    if should_run_switch_checkpoints(config):
        save_switch_results(config, get_switch_results(config))
    logger.info(
        "Initialized result files for device_scope=%s",
        config.get("device_scope"),
        extra={"run_id": str(config.get("run_id") or "").strip() or "-"},
    )






# Backup/Report Section on Header helpers

def get_presentation_report_path(config: dict) -> Path | None:
    customer_name = str(config.get("customer_name") or config.get("tenant_id") or "").strip()
    group_name = str(config.get("group_name") or "").strip()
    if not customer_name or not group_name:
        return None

    timestamp = config.get("run_timestamp") or create_run_timestamp()
    safe_customer_name = sanitize_excel_filename_component(customer_name, "Unknown_Customer")
    safe_group_name = sanitize_excel_filename_component(group_name, "Unknown_Group")
    file_name = f"{safe_customer_name}_{safe_group_name}_HPE Aruba_SATSOC_{timestamp}.xlsx"
    return get_output_dir(config) / file_name


def get_backup_report_destination(report_path: Path, config: dict) -> Path:
    run_timestamp = str(config.get("run_timestamp") or "").strip() or create_run_timestamp()
    run_dt = parse_run_timestamp(run_timestamp)
    month_folder = run_dt.strftime("%Y-%m")
    date_folder = run_dt.strftime("%Y-%m-%d")
    archive_dir = BACKUP_REPORTS_DIR / month_folder / date_folder
    archive_dir.mkdir(parents=True, exist_ok=True)

    destination_path = archive_dir / report_path.name
    if destination_path.exists():
        run_id = str(config.get("run_id") or "").strip()
        suffix = f"_{run_id[:8]}" if run_id else f"_{uuid4().hex[:8]}"
        destination_path = archive_dir / f"{report_path.stem}{suffix}{report_path.suffix}"

    return destination_path


def archive_validation_report(report_path: Path, config: dict) -> Path:
    destination_path = get_backup_report_destination(report_path, config)
    shutil.copy2(report_path, destination_path)
    logger.info(
        "Archived validation report to %s",
        destination_path,
        extra={"run_id": str(config.get("run_id") or "").strip() or "-"},
    )
    return destination_path


def parse_backup_folder_datetime(folder_name: str, patterns: tuple[str, ...]) -> tuple[datetime | None, str]:
    folder_value = str(folder_name or "").strip()
    for fmt in patterns:
        try:
            parsed = datetime.strptime(folder_value, fmt)
            if "%d" in fmt:
                label = parsed.strftime("%d %B %Y")
            else:
                label = parsed.strftime("%B %Y")
            return parsed, label
        except ValueError:
            continue
    return None, folder_value.replace("_", " ") or folder_value


def build_backup_reports_index() -> dict:
    if not BACKUP_REPORTS_DIR.exists():
        return {"months": []}

    month_entries: list[dict] = []
    for month_dir in sorted((path for path in BACKUP_REPORTS_DIR.iterdir() if path.is_dir()), key=lambda item: item.name):
        month_dt, month_label = parse_backup_folder_datetime(
            month_dir.name,
            ("%Y-%m", "%B_%Y", "%b_%Y", "%m-%Y", "%m_%Y"),
        )
        date_entries: list[dict] = []

        for date_dir in sorted((path for path in month_dir.iterdir() if path.is_dir()), key=lambda item: item.name):
            date_dt, date_label = parse_backup_folder_datetime(
                date_dir.name,
                ("%Y-%m-%d", "%d-%m-%Y", "%d_%m_%Y", "%Y_%m_%d"),
            )
            files = [
                {
                    "name": report_file.name,
                    "path": get_public_path(
                        f"/reports/{quote(month_dir.name)}/{quote(date_dir.name)}/{quote(report_file.name)}"
                    ),
                }
                for report_file in sorted(date_dir.glob("*.xlsx"), key=lambda item: item.name.lower())
            ]

            date_entries.append(
                {
                    "label": date_label,
                    "path": date_dir.name,
                    "files": files,
                    "_sort": date_dt or datetime.min,
                }
            )

        date_entries.sort(key=lambda item: item["_sort"])
        for date_entry in date_entries:
            date_entry.pop("_sort", None)
        month_entries.append(
            {
                "label": month_label,
                "path": month_dir.name,
                "dates": date_entries,
                "_sort": month_dt or datetime.min,
            }
        )

    month_entries.sort(key=lambda item: item["_sort"])
    for month_entry in month_entries:
        month_entry.pop("_sort", None)

    return {"months": month_entries}












# Checkpoint Runtime Helpers

def get_config_run_id(config: dict) -> str:
    return str(config.get("run_id") or "").strip() or "-"


def log_checkpoint_runtime_error(config: dict, checkpoint_name: str, exc: Exception) -> None:
    logger.exception(
        "Checkpoint %s failed at runtime: %s",
        checkpoint_name,
        exc,
        extra={"run_id": get_config_run_id(config)},
    )


def process_checkpoint_with_fallback(
    config: dict,
    checkpoint_name: str,
    next_route: str,
    evaluator,
    result_updater,
):
    run_id = get_config_run_id(config)
    try:
        checkpoint_result = evaluator(config)
        result_updater(config, checkpoint_name, checkpoint_result)
    except Exception as exc:
        log_checkpoint_runtime_error(config, checkpoint_name, exc)
        try:
            result_updater(config, checkpoint_name, "Execution Error")
        except Exception as update_exc:
            logger.exception(
                "Failed to save fallback result for %s: %s",
                checkpoint_name,
                update_exc,
                extra={"run_id": run_id},
            )
        logger.error(
            "Continuing workflow after checkpoint failure for %s",
            checkpoint_name,
            extra={"run_id": run_id},
        )
    return redirect_to(next_route, status_code=303)


def load_checkpoint_data_with_fallback(
    config: dict,
    checkpoint_name: str,
    loader,
    fallback_data,
):
    try:
        return loader(config)
    except Exception as exc:
        log_checkpoint_runtime_error(config, checkpoint_name, exc)
        logger.error(
            "Continuing workflow with fallback data for %s",
            checkpoint_name,
            extra={"run_id": get_config_run_id(config)},
        )
        return fallback_data


# Report-Generation helpers
def ensure_output_structure(config: dict) -> Path:
    output_folder = get_output_dir(config)
    output_folder.mkdir(parents=True, exist_ok=True)
    return output_folder


def generate_satsoc_presentation(config: dict) -> str | None:
    output_dir = ensure_output_structure(config)
    group_name = str(config.get("group_name") or "").strip()
    run_id = str(config.get("run_id") or "").strip() or "-"
    if not group_name:
        logger.info(
            "Skipping report generation because group_name is missing",
            extra={"run_id": run_id},
        )
        return None

    try:
        report_path = build_satsoc_presentation_layer(
            base_output_dir=output_dir,
            customer_name=str(config.get("customer_name") or config.get("tenant_id") or "").strip(),
            group_name=group_name,
            username=str(config.get("username") or "").strip(),
            device_scope=config.get("device_scope"),
            site_name=str(config.get("site_name") or "").strip(),
            ap_results=get_ap_results(config) if should_run_ap_checkpoints(config) else {},
            switch_results=get_switch_results(config) if should_run_switch_checkpoints(config) else {},
            workbook_timestamp=config.get("run_timestamp"),
        )
        if report_path:
            try:
                archive_validation_report(Path(report_path), config)
            except Exception as archive_exc:
                logger.exception(
                    "Archiving validation report failed: %s",
                    archive_exc,
                    extra={"run_id": run_id},
                )
                logger.error(
                    "Continuing workflow without backup archive for run_id=%s",
                    run_id,
                    extra={"run_id": run_id},
                )
        save_config_for_run(config["run_id"], config)
        logger.info(
            "Built SATSOC presentation workbook at %s",
            report_path,
            extra={"run_id": run_id},
        )
        return report_path
    except Exception as exc:
        logger.exception(
            "Validation report generation failed: %s",
            exc,
            extra={"run_id": run_id},
        )
        logger.error(
            "Continuing workflow without validation report for run_id=%s",
            run_id,
            extra={"run_id": run_id},
        )
        return None





# Flow-Routing Helpers

def get_first_validation_route(config: dict) -> str:
    if should_run_ap_checkpoints(config):
        return "/ap/checkpoint1"
    if should_run_switch_checkpoints(config):
        return "/switch/checkpoint1"
    raise RuntimeError("Unsupported device scope.")


def get_route_after_ap_checkpoints(config: dict) -> str:
    if should_run_switch_checkpoints(config):
        return "/switch/checkpoint1"
    return "/validation/complete"


def get_route_after_ap_checkpoint16(config: dict) -> str:
    if should_run_ap_checkpoints(config):
        return "/ap/checkpoint17"
    return get_route_after_ap_checkpoints(config)


def get_route_after_switch_checkpoints(config: dict) -> str:
    return "/validation/complete"





#  Data Extraction Helpers

def extract_customers(payload) -> list[dict[str, str]]:
    customers = []

    def visit(node):
        if isinstance(node, dict):
            customer_id = node.get("customer_id")
            customer_name = node.get("customer_name")
            if customer_id and customer_name:
                customers.append(
                    {
                        "customer_id": str(customer_id),
                        "customer_name": str(customer_name),
                    }
                )
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(payload)

    unique_customers = []
    seen_ids = set()
    for customer in customers:
        if customer["customer_id"] not in seen_ids:
            seen_ids.add(customer["customer_id"])
            unique_customers.append(customer)

    return unique_customers


def extract_groups(payload) -> list[str]:
    groups = []

    def visit(node):
        if isinstance(node, dict):
            for key in ("group", "group_name", "name"):
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    groups.append(value.strip())
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            if len(node) == 1 and isinstance(node[0], str) and node[0].strip():
                groups.append(node[0].strip())
            for item in node:
                visit(item)

    visit(payload)

    unique_groups = []
    seen = set()
    for group_name in groups:
        if group_name not in seen:
            seen.add(group_name)
            unique_groups.append(group_name)

    return unique_groups


def extract_sites(payload) -> list[str]:
    sites = []

    def visit(node):
        if isinstance(node, dict):
            for key in ("site_name", "name"):
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    sites.append(value.strip())
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            if len(node) == 1 and isinstance(node[0], str) and node[0].strip():
                sites.append(node[0].strip())
            for item in node:
                visit(item)

    visit(payload)

    unique_sites = []
    seen = set()
    for site_name in sites:
        if site_name not in seen:
            seen.add(site_name)
            unique_sites.append(site_name)

    return unique_sites







# Reports archive utilities

def render_reports_page(
    request: Request,
    selected_month: str = "",
    selected_date: str = "",
):
    return templates.TemplateResponse(
        "reports_archive.html",
        {
            "request": request,
            "backup_reports_index": build_backup_reports_index(),
            "reports_initial_selection": {
                "month": selected_month,
                "date": selected_date,
            },
        },
    )


def get_backup_reports_month_dir(month_folder: str) -> Path:
    month_dir = BACKUP_REPORTS_DIR / month_folder
    if not month_dir.exists() or not month_dir.is_dir():
        raise HTTPException(status_code=404, detail="Month archive not found.")
    return month_dir


def get_backup_reports_date_dir(month_folder: str, date_folder: str) -> Path:
    month_dir = get_backup_reports_month_dir(month_folder)
    date_dir = month_dir / date_folder
    if not date_dir.exists() or not date_dir.is_dir():
        raise HTTPException(status_code=404, detail="Date archive not found.")
    return date_dir


def resolve_backup_report_file(month_folder: str, date_folder: str, filename: str) -> Path:
    file_path = get_backup_reports_date_dir(month_folder, date_folder) / filename
    resolved_path = file_path.resolve()
    backup_root = BACKUP_REPORTS_DIR.resolve()
    if resolved_path != backup_root and backup_root not in resolved_path.parents:
        raise HTTPException(status_code=404, detail="Report file not found.")
    if not resolved_path.exists() or not resolved_path.is_file():
        raise HTTPException(status_code=404, detail="Report file not found.")
    return resolved_path




# All route handlers together in user-flow order

# Start point of the application
@app.get("/", response_class=HTMLResponse)
def step1(request: Request):
    if request.query_params.get("view") == "reports":
        return render_reports_page(request)
    return templates.TemplateResponse("step1_device_scope.html", {"request": request})

# Input handling routes
@app.post("/step1")
def handle_step1(
    username: str = Form(...),
    device_scope: str = Form(...),
):
    run_id, _config = start_new_run(username, device_scope)
    logger.info(
        "User submitted step 1 with device_scope=%s",
        device_scope,
        extra={"run_id": run_id},
    )
    response = redirect_to("/step2", status_code=302)
    response.set_cookie(RUN_COOKIE_NAME, run_id, httponly=True, samesite="lax")
    set_flow_lock_cookie(response, "/step2")
    return response


@app.get("/step2", response_class=HTMLResponse)
def step2(request: Request):
    config = load_config(request)
    return templates.TemplateResponse(
        "step2_inputs.html",
        {
            "request": request,
            "device_scope": config.get("device_scope"),
            "username": config.get("username", ""),
        },
    )


@app.post("/step2")
def handle_step2(
    request: Request,
    base_url: str = Form(...),
    tenant_id: str = Form(...),
    customer_name: str = Form(""),
    group_name: str = Form(...),
    site_name: str = Form(...),
    optimus_service_id: str = Form(...),
    ssid_count: int | None = Form(None),
    kloudspot_applicable: str = Form(""),
    ap_firmware_version: str = Form(""),
    switch_firmware_version: str = Form(""),
    stacked_switches: int = Form(None),
    standalone_switches: int = Form(None),
):
    config = load_config(request)
    run_ap_checkpoints = should_run_ap_checkpoints(config)
    run_id = str(config.get("run_id") or "").strip()

    config.update(
        {
            "base_url": base_url,
            "tenant_id": tenant_id,
            "customer_name": customer_name.strip() or tenant_id,
            "group_name": group_name,
            "site_name": site_name,
            "optimus_service_id": optimus_service_id,
            "ssid_count": ssid_count if run_ap_checkpoints else None,
            "kloudspot_applicable": kloudspot_applicable == "yes" if run_ap_checkpoints else False,
            "firmware_versions": {
                "ap": ap_firmware_version.strip(),
                "switch": switch_firmware_version.strip(),
            },
            "switch_counts": {
                "stacked": stacked_switches,
                "standalone": standalone_switches,
            },
            "tokens": get_current_tokens(),
        }
    )

    execution_dir = sync_execution_structure(config)
    config["paths"] = {
        "execution_dir": str(execution_dir),
        "api_response_dir": str(execution_dir / "API response"),
    }
    ensure_output_structure(config)
    save_config(request, config)
    initialize_result_files(config)
    try:
        bootstrap_optimus_api_responses(config)
    except Exception as exc:
        log_checkpoint_runtime_error(config, "Optimus-Attributes", exc)
        logger.error(
            "Continuing workflow without cached Optimus API response",
            extra={"run_id": run_id or "-"},
        )
    insert_audit_db(
        executed_by=str(config.get("username") or "").strip(),
        service_id=str(config.get("optimus_service_id") or "").strip(),
        network_name=str(config.get("site_name") or "").strip(),
        org_name=str(config.get("customer_name") or config.get("tenant_id") or "").strip(),
    )
    logger.info(
        "Saved step 2 configuration for customer=%s group=%s site=%s",
        config.get("customer_name") or config.get("tenant_id") or "",
        config.get("group_name") or "",
        config.get("site_name") or "",
        extra={"run_id": run_id or "-"},
    )

    return redirect_to(get_first_validation_route(config), status_code=303)


#Using tokens to fetch customers, groups, sites from central and save customer/group/site selection
@app.get("/api/fetch-customers")
def fetch_customers_from_central(base_url: str):
    tokens = get_current_tokens()
    central_token = tokens.get("central", "")
    if not base_url:
        raise HTTPException(
            status_code=400,
            detail="base_url is required to fetch customers",
        )
    if not central_token:
        raise HTTPException(
            status_code=400,
            detail="Aruba access token is unavailable.",
        )

    logger.info("Fetching customers from Aruba Central")
    url = f"{base_url}/msp_api/v1/customers?offset=0&limit=100"
    headers = {
        "Authorization": f"Bearer {central_token}",
        "Accept": "application/json",
    }

    response = requests.get(url, headers=headers, timeout=30)

    if response.status_code != 200:
        logger.error(
            "Customer fetch failed with HTTP %s",
            response.status_code,
        )
        raise HTTPException(
            status_code=response.status_code,
            detail=f"Failed to fetch customers from Aruba Central: {response.text[:500]}",
        )

    try:
        payload = response.json()
    except ValueError as exc:
        logger.error("Customer response from Aruba Central was not valid JSON")
        raise HTTPException(
            status_code=500,
            detail="Customer response from Aruba Central was not valid JSON",
        ) from exc

    customers = extract_customers(payload)

    return {"customers": customers}


@app.post("/api/save-customer-selection")
def save_customer_selection(
    request: Request,
    tenant_id: str = Form(...),
    base_url: str = Form(""),
):
    config = load_config(request)
    config["tenant_id"] = tenant_id

    if base_url:
        config["base_url"] = base_url

    save_config(request, config)
    logger.info(
        "Saved customer selection for tenant_id=%s",
        tenant_id,
        extra={"run_id": str(config.get("run_id") or "").strip() or "-"},
    )

    return {"status": "success"}


@app.post("/api/save-group-selection")
def save_group_selection(request: Request, group_name: str = Form(...)):
    config = load_config(request)
    config["group_name"] = group_name
    save_config(request, config)
    logger.info(
        "Saved group selection: %s",
        group_name,
        extra={"run_id": str(config.get("run_id") or "").strip() or "-"},
    )
    return {"status": "success"}


@app.post("/api/save-site-selection")
def save_site_selection(request: Request, site_name: str = Form(...)):
    config = load_config(request)
    config["site_name"] = site_name
    save_config(request, config)
    logger.info(
        "Saved site selection: %s",
        site_name,
        extra={"run_id": str(config.get("run_id") or "").strip() or "-"},
    )
    return {"status": "success"}


@app.get("/api/fetch-groups")
def fetch_groups_from_central(base_url: str, tenant_id: str):
    tokens = get_current_tokens()
    central_token = tokens.get("central", "")
    if not base_url or not tenant_id:
        raise HTTPException(
            status_code=400,
            detail="base_url and tenant_id are required to fetch groups",
        )
    if not central_token:
        raise HTTPException(
            status_code=400,
            detail="Aruba access token is unavailable.",
        )

    logger.info("Fetching groups from Aruba Central")
    url = f"{base_url}/configuration/v2/groups?limit=100&offset=0"
    headers = {
        "Authorization": f"Bearer {central_token}",
        "TenantID": tenant_id,
        "Accept": "application/json",
    }

    response = requests.get(url, headers=headers, timeout=30)

    if response.status_code != 200:
        logger.error("Group fetch failed with HTTP %s", response.status_code)
        raise HTTPException(
            status_code=response.status_code,
            detail=f"Failed to fetch groups from Aruba Central: {response.text[:500]}",
        )

    try:
        payload = response.json()
    except ValueError as exc:
        logger.error("Group response from Aruba Central was not valid JSON")
        raise HTTPException(
            status_code=500,
            detail="Group response from Aruba Central was not valid JSON",
        ) from exc

    groups = extract_groups(payload)

    return {"groups": groups}


@app.get("/api/fetch-sites")
def fetch_sites_from_central(base_url: str, tenant_id: str):
    tokens = get_current_tokens()
    central_token = tokens.get("central", "")
    if not base_url or not tenant_id:
        raise HTTPException(
            status_code=400,
            detail="base_url and tenant_id are required to fetch sites",
        )
    if not central_token:
        raise HTTPException(
            status_code=400,
            detail="Aruba access token is unavailable.",
        )

    logger.info("Fetching sites from Aruba Central")
    url = f"{base_url}/central/v2/sites"
    headers = {
        "Authorization": f"Bearer {central_token}",
        "TenantID": tenant_id,
        "Accept": "application/json",
    }

    response = requests.get(url, headers=headers, timeout=30)

    if response.status_code != 200:
        logger.error("Site fetch failed with HTTP %s", response.status_code)
        raise HTTPException(
            status_code=response.status_code,
            detail=f"Failed to fetch sites from Aruba Central: {response.text[:500]}",
        )

    try:
        payload = response.json()
    except ValueError as exc:
        logger.error("Site response from Aruba Central was not valid JSON")
        raise HTTPException(
            status_code=500,
            detail="Site response from Aruba Central was not valid JSON",
        ) from exc

    sites = extract_sites(payload)

    return {"sites": sites}


# AP Checkpoint routes
@app.get("/ap/checkpoint1")
def ap_checkpoint1_page(request: Request):
    config = load_config(request)

    if not should_run_ap_checkpoints(config):
        return PlainTextResponse("AP Checkpoint 1 not applicable.", status_code=400)

    return process_checkpoint_with_fallback(
        config,
        "Hostname-Serial-Mapping",
        "/ap/checkpoint2",
        ap_checkpoint_1_hostname_serial_comparison_result,
        update_ap_result,
    )


@app.get("/ap/checkpoint2")
def ap_checkpoint2_page(request: Request):
    config = load_config(request)

    if not should_run_ap_checkpoints(config):
        return PlainTextResponse("AP Checkpoint 2 not applicable.", status_code=400)

    return process_checkpoint_with_fallback(
        config,
        "Serial-Number-Verification",
        "/ap/checkpoint3",
        ap_checkpoint_2_serial_number_comparison_result,
        update_ap_result,
    )


@app.get("/ap/checkpoint3")
def ap_checkpoint3_process(request: Request):
    config = load_config(request)

    if not should_run_ap_checkpoints(config):
        return PlainTextResponse("AP Checkpoint 3 not applicable.", status_code=400)

    return process_checkpoint_with_fallback(
        config,
        "APs-Health",
        "/ap/checkpoint4",
        ap_checkpoint_3_aps_health,
        update_ap_result,
    )


@app.get("/ap/checkpoint4")
def ap_checkpoint4_process(request: Request):
    config = load_config(request)

    if not should_run_ap_checkpoints(config):
        return PlainTextResponse("AP Checkpoint 4 not applicable.", status_code=400)

    return process_checkpoint_with_fallback(
        config,
        "Note-Presence",
        "/ap/checkpoint5",
        ap_checkpoint_4_note_presence,
        update_ap_result,
    )


@app.get("/ap/checkpoint5")
def ap_checkpoint5_page(request: Request):
    config = load_config(request)

    if not should_run_ap_checkpoints(config):
        return PlainTextResponse("AP Checkpoint 5 not applicable.", status_code=400)

    subscription_rows = load_checkpoint_data_with_fallback(
        config,
        "License-Subscription-Tier-Contract-Details",
        ap_checkpoint_5_license_subscription_details,
        [],
    )

    return templates.TemplateResponse(
        "ap_checkpoint5_license_subscription_details.html",
        {
            "request": request,
            "subscription_rows": subscription_rows,
        },
    )


@app.post("/ap/checkpoint5/submit")
def submit_ap_checkpoint5_result(request: Request, result: str = Form(...)):
    config = load_config(request)
    update_ap_result(config, "License-Subscription-Tier-Contract-Details", result == "true")
    return redirect_to("/ap/checkpoint6", status_code=303)


@app.get("/ap/checkpoint6")
def ap_checkpoint6_process(request: Request):
    config = load_config(request)

    if not should_run_ap_checkpoints(config):
        return PlainTextResponse("AP Checkpoint 6 not applicable.", status_code=400)

    return process_checkpoint_with_fallback(
        config,
        "Subscription-Key-Tag-Mapping",
        "/ap/checkpoint7",
        ap_checkpoint_6_subscription_key_tag_mapping,
        update_ap_result,
    )


@app.get("/ap/checkpoint7")
def ap_checkpoint7_process(request: Request):
    config = load_config(request)

    if not should_run_ap_checkpoints(config):
        return PlainTextResponse("AP Checkpoint 7 not applicable.", status_code=400)

    return process_checkpoint_with_fallback(
        config,
        "Group-Name-Site-Name-TCL-Service-ID-Mapping",
        "/ap/checkpoint8",
        ap_checkpoint_7_group_name_site_name_tcl_service_id,
        update_ap_result,
    )


@app.get("/ap/checkpoint8")
def ap_checkpoint8_process(request: Request):
    config = load_config(request)

    if not should_run_ap_checkpoints(config):
        return PlainTextResponse("AP Checkpoint 8 not applicable.", status_code=400)

    return process_checkpoint_with_fallback(
        config,
        "Unique-Site-Unique-Group-Mapping",
        "/ap/checkpoint9",
        ap_checkpoint_8_unique_site_unique_group_mapping,
        update_ap_result,
    )


@app.get("/ap/checkpoint9")
def ap_checkpoint9_process(request: Request):
    config = load_config(request)

    if not should_run_ap_checkpoints(config):
        return PlainTextResponse("AP Checkpoint 9 not applicable.", status_code=400)

    return process_checkpoint_with_fallback(
        config,
        "Floor-Plan-Presence",
        "/ap/checkpoint10",
        ap_checkpoint_9_floor_plan_presence,
        update_ap_result,
    )


@app.get("/ap/checkpoint10")
def ap_checkpoint10_page(request: Request):
    config = load_config(request)

    if not should_run_ap_checkpoints(config):
        return PlainTextResponse("AP Checkpoint 10 not applicable.", status_code=400)

    checkpoint_result = load_checkpoint_data_with_fallback(
        config,
        "Floor-Wise-AP-Count",
        ap_checkpoint_10_floor_wise_ap_count,
        {
            "errors": ["Unable to load floor-wise AP count due to runtime error."],
            "floor_rows": [],
            "total_ap_count": 0,
        },
    )
    return templates.TemplateResponse(
        "ap_checkpoint10_floor_wise_ap_count.html",
        {
            "request": request,
            "errors": checkpoint_result["errors"],
            "floor_rows": checkpoint_result["floor_rows"],
            "total_ap_count": checkpoint_result["total_ap_count"],
        },
    )


@app.post("/ap/checkpoint10/submit")
def submit_ap_checkpoint10_result(request: Request, result: str = Form(...)):
    config = load_config(request)
    checkpoint_result = "Compliance" if result == "true" else "Non-Compliance"
    update_ap_result(config, "Floor-Wise-AP-Count", checkpoint_result)
    return redirect_to("/ap/checkpoint11", status_code=303)


@app.get("/ap/checkpoint11")
def ap_checkpoint11_process(request: Request):
    config = load_config(request)

    if not should_run_ap_checkpoints(config):
        return PlainTextResponse("AP Checkpoint 11 not applicable.", status_code=400)

    return process_checkpoint_with_fallback(
        config,
        "Firmware-Version-Check",
        "/ap/checkpoint12",
        ap_checkpoint_11_firmware_version_check,
        update_ap_result,
    )


@app.get("/ap/checkpoint12")
def ap_checkpoint12_process(request: Request):
    config = load_config(request)

    if not should_run_ap_checkpoints(config):
        return PlainTextResponse("AP Checkpoint 12 not applicable.", status_code=400)

    return process_checkpoint_with_fallback(
        config,
        "Number-of-SSID",
        "/ap/checkpoint13",
        ap_checkpoint_12_number_of_ssid,
        update_ap_result,
    )


@app.get("/ap/checkpoint13")
def ap_checkpoint13_page(request: Request):
    config = load_config(request)

    if not should_run_ap_checkpoints(config):
        return PlainTextResponse("AP Checkpoint 13 not applicable.", status_code=400)

    wlan_entries = load_checkpoint_data_with_fallback(
        config,
        "Authentication-Method-Opmode",
        ap_checkpoint_13_authentication_method_opmode,
        [],
    )
    return templates.TemplateResponse(
        "ap_checkpoint13_authentication_method_opmode.html",
        {
            "request": request,
            "wlan_entries": wlan_entries,
        },
    )


@app.post("/ap/checkpoint13/submit")
def submit_ap_checkpoint13_result(request: Request, result: str = Form(...)):
    config = load_config(request)
    checkpoint_result = "Compliance" if result == "true" else "Non-Compliance"
    update_ap_result(config, "Authentication-Method-Opmode", checkpoint_result)
    return redirect_to("/ap/checkpoint14", status_code=303)


@app.get("/ap/checkpoint14")
def ap_checkpoint14_process(request: Request):
    config = load_config(request)

    if not should_run_ap_checkpoints(config):
        return PlainTextResponse("AP Checkpoint 14 not applicable.", status_code=400)

    return process_checkpoint_with_fallback(
        config,
        "Webhook-Integration",
        "/ap/checkpoint15",
        ap_checkpoint_14_webhook_integration,
        update_ap_result,
    )


@app.get("/ap/checkpoint15")
def ap_checkpoint15_process(request: Request):
    config = load_config(request)

    if not should_run_ap_checkpoints(config):
        return PlainTextResponse("AP Checkpoint 15 not applicable.", status_code=400)

    return process_checkpoint_with_fallback(
        config,
        "Speed-Duplex",
        "/ap/checkpoint16",
        ap_checkpoint_15_speed_duplex,
        update_ap_result,
    )


@app.get("/ap/checkpoint16")
def ap_checkpoint16_page(request: Request):
    config = load_config(request)

    if not should_run_ap_checkpoints(config):
        return PlainTextResponse("AP Checkpoint 16 not applicable.", status_code=400)

    checkpoint_data = load_checkpoint_data_with_fallback(
        config,
        "Country-Timezone",
        ap_checkpoint_16_country_timezone_review,
        {
            "country": "Unavailable",
            "clock_timezone_line": "Unavailable",
        },
    )
    return templates.TemplateResponse(
        "ap_checkpoint16_country_timezone.html",
        {
            "request": request,
            "country": checkpoint_data["country"],
            "clock_timezone_line": checkpoint_data["clock_timezone_line"],
        },
    )


@app.post("/ap/checkpoint16/submit")
def submit_ap_checkpoint16_result(request: Request, result: str = Form(...)):
    config = load_config(request)
    checkpoint_result = "Compliance" if result == "true" else "Non-Compliance"
    update_ap_result(config, "Country-Timezone", checkpoint_result)
    return redirect_to(get_route_after_ap_checkpoint16(config), status_code=303)


@app.get("/ap/checkpoint17")
def ap_checkpoint17_process(request: Request):
    config = load_config(request)

    if not should_run_ap_checkpoints(config):
        return PlainTextResponse("AP Checkpoint 17 not applicable.", status_code=400)

    if not config.get("kloudspot_applicable"):
        update_ap_result(
            config,
            "Kloudspot-(Value-Added-Services-without-CPPM)",
            "Kloudspot is not opted by Customer",
        )
        return redirect_to(get_route_after_ap_checkpoints(config), status_code=303)

    return process_checkpoint_with_fallback(
        config,
        "Kloudspot-(Value-Added-Services-without-CPPM)",
        get_route_after_ap_checkpoints(config),
        ap_checkpoint_17_kloudspot_without_cppm,
        update_ap_result,
    )


# Switches checkpoints routes
@app.get("/switch/checkpoint1")
def switch_checkpoint1_page(request: Request):
    config = load_config(request)

    if not should_run_switch_checkpoints(config):
        return PlainTextResponse("Switch Checkpoint 1 not applicable.", status_code=400)

    return process_checkpoint_with_fallback(
        config,
        "Hostname-Serial-Mapping",
        "/switch/checkpoint2",
        switch_checkpoint_1_hostname_serial_comparison_result,
        update_switch_result,
    )


@app.get("/switch/checkpoint2")
def switch_checkpoint2_page(request: Request):
    config = load_config(request)

    if not should_run_switch_checkpoints(config):
        return PlainTextResponse("Switch Checkpoint 2 not applicable.", status_code=400)

    return process_checkpoint_with_fallback(
        config,
        "Serial-Number-Verification",
        "/switch/checkpoint3",
        switch_checkpoint_2_serial_number_comparison_result,
        update_switch_result,
    )


@app.get("/switch/checkpoint3")
def switch_checkpoint3_process(request: Request):
    config = load_config(request)

    if not should_run_switch_checkpoints(config):
        return PlainTextResponse("Switch Checkpoint 3 not applicable.", status_code=400)

    return process_checkpoint_with_fallback(
        config,
        "Switches-Health",
        "/switch/checkpoint4",
        switch_checkpoint_3_switches_health,
        update_switch_result,
    )


@app.get("/switch/checkpoint4")
def switch_checkpoint4_process(request: Request):
    config = load_config(request)

    if not should_run_switch_checkpoints(config):
        return PlainTextResponse("Switch Checkpoint 4 not applicable.", status_code=400)

    return process_checkpoint_with_fallback(
        config,
        "Label-Presence",
        "/switch/checkpoint5",
        switch_checkpoint_4_label_presence,
        update_switch_result,
    )


@app.get("/switch/checkpoint5")
def switch_checkpoint5_page(request: Request):
    config = load_config(request)

    if not should_run_switch_checkpoints(config):
        return PlainTextResponse("Switch Checkpoint 5 not applicable.", status_code=400)

    subscription_rows = load_checkpoint_data_with_fallback(
        config,
        "License-Subscription-Tier-Contract-Details",
        switch_checkpoint_5_license_subscription_details,
        [],
    )
    return templates.TemplateResponse(
        "switch_checkpoint5_license_subscription_details.html",
        {
            "request": request,
            "subscription_rows": subscription_rows,
        },
    )


@app.post("/switch/checkpoint5/submit")
def submit_switch_checkpoint5_result(request: Request, result: str = Form(...)):
    config = load_config(request)
    update_switch_result(config, "License-Subscription-Tier-Contract-Details", result == "true")
    return redirect_to("/switch/checkpoint6", status_code=303)


@app.get("/switch/checkpoint6")
def switch_checkpoint6_process(request: Request):
    config = load_config(request)

    if not should_run_switch_checkpoints(config):
        return PlainTextResponse("Switch Checkpoint 6 not applicable.", status_code=400)

    return process_checkpoint_with_fallback(
        config,
        "Subscription-Key-Tag-Mapping",
        "/switch/checkpoint7",
        switch_checkpoint_6_subscription_key_tag_mapping,
        update_switch_result,
    )


@app.get("/switch/checkpoint7")
def switch_checkpoint7_process(request: Request):
    config = load_config(request)

    if not should_run_switch_checkpoints(config):
        return PlainTextResponse("Switch Checkpoint 7 not applicable.", status_code=400)

    return process_checkpoint_with_fallback(
        config,
        "Group-Name-Site-Name-TCL-Service-ID-Mapping",
        "/switch/checkpoint8",
        switch_checkpoint_7_group_name_site_name_tcl_service_id,
        update_switch_result,
    )


@app.get("/switch/checkpoint8")
def switch_checkpoint8_process(request: Request):
    config = load_config(request)

    if not should_run_switch_checkpoints(config):
        return PlainTextResponse("Switch Checkpoint 8 not applicable.", status_code=400)

    return process_checkpoint_with_fallback(
        config,
        "Unique-Site-Unique-Group-Mapping",
        "/switch/checkpoint9",
        switch_checkpoint_8_unique_site_unique_group_mapping,
        update_switch_result,
    )


@app.get("/switch/checkpoint9")
def switch_checkpoint9_process(request: Request):
    config = load_config(request)

    if not should_run_switch_checkpoints(config):
        return PlainTextResponse("Switch Checkpoint 9 not applicable.", status_code=400)

    return process_checkpoint_with_fallback(
        config,
        "Firmware-Version-Check",
        "/switch/checkpoint10",
        switch_checkpoint_9_firmware_version_check,
        update_switch_result,
    )


@app.get("/switch/checkpoint10")
def switch_checkpoint10_process(request: Request):
    config = load_config(request)

    if not should_run_switch_checkpoints(config):
        return PlainTextResponse("Switch Checkpoint 10 not applicable.", status_code=400)

    return process_checkpoint_with_fallback(
        config,
        "Stacked-and-Standalone-Switches-Check",
        "/switch/checkpoint11",
        switch_checkpoint_10_stacked_and_standalone_switches_check,
        update_switch_result,
    )


@app.get("/switch/checkpoint11")
def switch_checkpoint11_process(request: Request):
    config = load_config(request)

    if not should_run_switch_checkpoints(config):
        return PlainTextResponse("Switch Checkpoint 11 not applicable.", status_code=400)

    return process_checkpoint_with_fallback(
        config,
        "Unused-Port",
        "/switch/checkpoint12",
        switch_checkpoint_11_unused_port,
        update_switch_result,
    )


@app.get("/switch/checkpoint12")
def switch_checkpoint12_process(request: Request):
    config = load_config(request)

    if not should_run_switch_checkpoints(config):
        return PlainTextResponse("Switch Checkpoint 12 not applicable.", status_code=400)

    return process_checkpoint_with_fallback(
        config,
        "Webhook-Integration",
        "/switch/checkpoint14",
        switch_checkpoint_12_webhook_integration,
        update_switch_result,
    )


@app.get("/switch/checkpoint14")
def switch_checkpoint14_page(request: Request):
    config = load_config(request)

    if not should_run_switch_checkpoints(config):
        return PlainTextResponse("Switch Checkpoint 14 not applicable.", status_code=400)

    checkpoint_data = load_checkpoint_data_with_fallback(
        config,
        "Country-Timezone",
        switch_checkpoint_14_country_timezone_review,
        {
            "country": "Unavailable",
            "time_zone": "Unavailable",
        },
    )
    return templates.TemplateResponse(
        "switch_checkpoint14_country_timezone.html",
        {
            "request": request,
            "country": checkpoint_data["country"],
            "time_zone": checkpoint_data["time_zone"],
        },
    )


@app.post("/switch/checkpoint14/submit")
def submit_switch_checkpoint14_result(request: Request, result: str = Form(...)):
    config = load_config(request)
    checkpoint_result = "Compliance" if result == "true" else "Non-Compliance"
    update_switch_result(config, "Country-Timezone", checkpoint_result)
    return redirect_to("/switch/checkpoint15", status_code=303)


@app.get("/switch/checkpoint15")
def switch_checkpoint15_process(request: Request):
    config = load_config(request)

    if not should_run_switch_checkpoints(config):
        return PlainTextResponse("Switch Checkpoint 15 not applicable.", status_code=400)

    return process_checkpoint_with_fallback(
        config,
        "Speed-Duplex",
        get_route_after_switch_checkpoints(config),
        switch_checkpoint_15_speed_duplex,
        update_switch_result,
    )


# Validation completion and Excel report download routes
@app.get("/validation/complete")
def validation_complete(request: Request):
    config = load_config(request)
    logger.info(
        "Generating validation report",
        extra={"run_id": str(config.get("run_id") or "").strip() or "-"},
    )
    report_path = generate_satsoc_presentation(config)
    report_file_name = Path(report_path).name if report_path else ""
    if report_path:
        logger.info(
            "Validation report generated at %s",
            report_path,
            extra={"run_id": str(config.get("run_id") or "").strip() or "-"},
        )
    else:
        logger.info(
            "Validation report could not be generated because required data is missing",
            extra={"run_id": str(config.get("run_id") or "").strip() or "-"},
        )
    return templates.TemplateResponse(
        "validation_complete.html",
        {
            "request": request,
            "report_path": report_path,
            "report_file_name": report_file_name,
        },
    )


@app.get("/validation/report/download")
def download_validation_report(request: Request):
    config = load_config(request)
    report_path = get_presentation_report_path(config)
    if not report_path:
        logger.error(
            "Validation report download requested but report path is unavailable",
            extra={"run_id": str(config.get("run_id") or "").strip() or "-"},
        )
        raise HTTPException(status_code=404, detail="Validation report not found.")

    if not report_path.exists():
        logger.error(
            "Validation report download requested but file is missing at %s",
            report_path,
            extra={"run_id": str(config.get("run_id") or "").strip() or "-"},
        )
        raise HTTPException(status_code=404, detail="Validation report file is missing.")

    logger.info(
        "Downloading validation report from %s",
        report_path,
        extra={"run_id": str(config.get("run_id") or "").strip() or "-"},
    )
    return FileResponse(
        path=report_path,
        filename=report_path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )







# Report Section on Header routes

@app.get("/reports", response_class=HTMLResponse)
def reports_root(request: Request):
    return render_reports_page(request)


@app.get("/reports/{month_folder}", response_class=HTMLResponse)
def reports_month(request: Request, month_folder: str):
    get_backup_reports_month_dir(month_folder)
    return render_reports_page(request, selected_month=month_folder)


@app.get("/reports/{month_folder}/{date_folder}", response_class=HTMLResponse)
def reports_date(request: Request, month_folder: str, date_folder: str):
    get_backup_reports_date_dir(month_folder, date_folder)
    return render_reports_page(request, selected_month=month_folder, selected_date=date_folder)


@app.get("/reports/{month_folder}/{date_folder}/{filename}")
def download_backup_report(month_folder: str, date_folder: str, filename: str):
    report_path = resolve_backup_report_file(month_folder, date_folder, filename)
    return FileResponse(
        path=report_path,
        filename=report_path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )







if BASE_ROUTE:
    deployment_app = FastAPI(
        title="SATSOC Validation Tool",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @deployment_app.get("/", include_in_schema=False)
    def deployment_root():
        return RedirectResponse(get_public_path("/"), status_code=307)

    deployment_app.mount(BASE_ROUTE, app)
    app = deployment_app


if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=5018, reload=True)
