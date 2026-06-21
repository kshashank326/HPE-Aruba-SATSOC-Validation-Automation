from __future__ import annotations

import datetime
import json
import re
from pathlib import Path

import xlsxwriter
# from openpyxl import load_workbook
# from openpyxl.workbook.protection import WorkbookProtection

from logging_utils import get_logger
import os

AP_CHECKPOINT_HEADERS = [
    "Hostname Serial Number Mapping",
    "Serial Number Verification",
    "APs Health",
    "Note Presence",
    "License Subscription tier + Contract details",
    "Subscription Key - Tag Mapping",
    "Group Name-Site Name-TCL Service ID Presence",
    "Unique Site-Unique Group Mapping",
    "Floor Plan Presence",
    "Floor Wise AP Count",
    "Firmware Version Check",
    "Number of SSID",
    "Authentication Method (Opmode)",
    "Webhook Integration",
    "Speed Duplex",
    "Country Timezone",
    "Kloudspot (Value Added Services without CPPM)",
]

SWITCH_CHECKPOINT_HEADERS = [
    "Hostname Serial Number Mapping",
    "Serial Number Verification",
    "Switches Health",
    "Label Presence",
    "License Subscription tier + Contract details",
    "Subscription Key - Tag Mapping",
    "Group Name-Site Name-TCL Service ID Presence",
    "Unique Site-Unique Group Mapping",
    "Firmware Version Check",
    "Stacked and Standalone Switches check",
    "Unused port",
    "Webhook Integration",
    "Country Timezone",
    "Speed Duplex",
]
SATSOC_SHEET_PASSWORD = "A$DS&S3$ATS0C"
SECTION_TITLE_ROW = 7
DEVICE_CONTENT_START_ROW = 8
SUMMARY_HEADER_ROW = 4
SUMMARY_DATA_ROW = 5
SUMMARY_PRODUCT_NAME = "Managed Wi-Fi and LAN Solutions"
SUMMARY_OEM = "HPE Aruba"
logger = get_logger(__name__)

AP_RESULT_KEY_MAP = {
    "Hostname Serial Number Mapping": "Hostname-Serial-Mapping",
    "Serial Number Verification": "Serial-Number-Verification",
    "APs Health": "APs-Health",
    "Note Presence": "Note-Presence",
    "License Subscription tier + Contract details": "License-Subscription-Tier-Contract-Details",
    "Subscription Key - Tag Mapping": "Subscription-Key-Tag-Mapping",
    "Group Name-Site Name-TCL Service ID Presence": "Group-Name-Site-Name-TCL-Service-ID-Mapping",
    "Unique Site-Unique Group Mapping": "Unique-Site-Unique-Group-Mapping",
    "Floor Plan Presence": "Floor-Plan-Presence",
    "Floor Wise AP Count": "Floor-Wise-AP-Count",
    "Firmware Version Check": "Firmware-Version-Check",
    "Number of SSID": "Number-of-SSID",
    "Authentication Method (Opmode)": "Authentication-Method-Opmode",
    "Webhook Integration": "Webhook-Integration",
    "Speed Duplex": "Speed-Duplex",
    "Country Timezone": "Country-Timezone",
    "Kloudspot (Value Added Services without CPPM)": "Kloudspot-(Value-Added-Services-without-CPPM)",
}

SWITCH_RESULT_KEY_MAP = {
    "Hostname Serial Number Mapping": "Hostname-Serial-Mapping",
    "Serial Number Verification": "Serial-Number-Verification",
    "Switches Health": "Switches-Health",
    "Label Presence": "Label-Presence",
    "License Subscription tier + Contract details": "License-Subscription-Tier-Contract-Details",
    "Subscription Key - Tag Mapping": "Subscription-Key-Tag-Mapping",
    "Group Name-Site Name-TCL Service ID Presence": "Group-Name-Site-Name-TCL-Service-ID-Mapping",
    "Unique Site-Unique Group Mapping": "Unique-Site-Unique-Group-Mapping",
    "Firmware Version Check": "Firmware-Version-Check",
    "Stacked and Standalone Switches check": "Stacked-and-Standalone-Switches-Check",
    "Unused port": "Unused-Port",
    "Webhook Integration": "Webhook-Integration",
    "Country Timezone": "Country-Timezone",
    "Speed Duplex": "Speed-Duplex",
}


def sanitize_excel_filename_component(value: str, fallback: str = "Unknown") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", str(value or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or fallback


def build_worksheet_name(group_name: str, suffix: str) -> str:
    cleaned_group_name = re.sub(r"[\[\]:*?/\\]+", " ", str(group_name or "").strip())
    cleaned_group_name = re.sub(r"\s+", " ", cleaned_group_name).strip(" '")
    if not cleaned_group_name:
        cleaned_group_name = "Group"

    worksheet_name = f"{cleaned_group_name} {suffix}".strip()
    return worksheet_name[:31]


def normalize_checkpoint_result(value):
    if isinstance(value, bool):
        return "Compliance" if value else "Non-Compliance"
    if isinstance(value, list):
        return "Compliance" if not value else "Non-Compliance"
    if isinstance(value, dict):
        return "Non-Compliance"

    normalized = str(value or "").strip()
    if not normalized:
        return "Pending"
    return normalized


def is_compliance_value(value) -> bool:
    return normalize_checkpoint_result(value) == "Compliance"


def count_compliance(results_dict: dict, checkpoint_headers: list[str]) -> tuple[int, int]:
    total = len(checkpoint_headers)
    achieved = 0

    for checkpoint_name in checkpoint_headers:
        if is_compliance_value(results_dict.get(checkpoint_name, "Pending")):
            achieved += 1

    return achieved, total


def remap_results(results_dict: dict, key_map: dict[str, str]) -> dict:
    remapped = {}
    for display_key, source_key in key_map.items():
        if source_key in results_dict:
            remapped[display_key] = results_dict[source_key]
    return remapped


def load_inventory_count(api_response_dir: Path, file_name: str, inventory_key: str) -> int | None:
    response_file = api_response_dir / file_name
    if not response_file.exists():
        return None

    try:
        payload = json.loads(response_file.read_text())
    except (OSError, json.JSONDecodeError):
        return None

    inventory = payload.get(inventory_key)
    if isinstance(inventory, list):
        return sum(1 for item in inventory if isinstance(item, dict))

    return None


def format_report_timestamp(timestamp: str | None) -> str:
    if not timestamp:
        return datetime.datetime.now().strftime("%A, %d/%b/%Y %H:%M:%S")

    for fmt in ("%d-%m-%Y_%H-%M-%S", "%d-%m-%Y_%H-%M-%S.%f"):
        try:
            parsed = datetime.datetime.strptime(timestamp, fmt)
            return parsed.strftime("%A, %d/%b/%Y %H:%M:%S")
        except ValueError:
            continue

    return str(timestamp)


def windows_long_path(path: Path) -> str:
    resolved = str(path.resolve())
    if os.name != "nt" or resolved.startswith("\\\\?\\"):
        return resolved
    if resolved.startswith("\\\\"):
        return "\\\\?\\UNC\\" + resolved[2:]
    return "\\\\?\\" + resolved


def resolve_header_logo_path(file_name: str) -> str | None:
    candidate_paths = [
        Path(r"C:\SATSOC WebApp\SATSOC_Home_app\static\images") / file_name,
        Path(__file__).resolve().parent / "assets" / file_name,
    ]

    for candidate in candidate_paths:
        if candidate.exists():
            return str(candidate)

    logger.debug("Header logo not found for %s. Checked: %s", file_name, candidate_paths)
    return None


def build_sheet_customer_group_title(customer_name: str, group_name: str) -> str:
    cleaned_customer_name = str(customer_name or "").strip() or "Customer"
    cleaned_group_name = str(group_name or "").strip() or "Group"
    return f"{cleaned_customer_name} - {cleaned_group_name}"



def read_png_dimensions(image_path: str) -> tuple[int, int] | None:
    try:
        with Path(image_path).open("rb") as image_file:
            header = image_file.read(24)
    except OSError:
        return None

    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        return None

    width = int.from_bytes(header[16:20], "big")
    height = int.from_bytes(header[20:24], "big")
    if width <= 0 or height <= 0:
        return None
    return width, height


def column_width_to_pixels(width: float) -> int:
    return int(width * 7 + 5)


def row_height_to_pixels(height: float) -> int:
    return int(height * 4 / 3)


def build_image_fit_options(
    image_path: str,
    target_width_px: int,
    target_height_px: int,
    preserve_aspect_ratio: bool = False,
) -> dict[str, float | int]:
    image_dimensions = read_png_dimensions(image_path)
    if not image_dimensions:
        return {"x_offset": 0, "y_offset": 0, "object_position": 1}

    image_width, image_height = image_dimensions
    if preserve_aspect_ratio:
        scale = min(target_width_px / image_width, target_height_px / image_height)
        scaled_width = int(image_width * scale)
        scaled_height = int(image_height * scale)
        return {
            "x_scale": scale,
            "y_scale": scale,
            "x_offset": max((target_width_px - scaled_width) // 2, 0),
            "y_offset": max((target_height_px - scaled_height) // 2, 0),
            "object_position": 1,
        }

    return {
        "x_scale": target_width_px / image_width,
        "y_scale": target_height_px / image_height,
        "x_offset": 0,
        "y_offset": 0,
        "object_position": 1,
    }


def add_formats(workbook: xlsxwriter.Workbook) -> dict[str, xlsxwriter.format.Format]:
    return {
        "header_title": workbook.add_format({
            "bold": 1,
            "align": "center",
            "valign": "vcenter",
            "font_size": 18,
            "font_name": "Trebuchet MS",
            "font_color": "#4472C4",
            "bg_color": "white",
            "locked": True,
        }),
        "section_title": workbook.add_format({
            "bold": 1,
            "align": "center",
            "valign": "vcenter",
            "font_size": 16,
            "font_name": "Trebuchet MS",
            "font_color": "#4472C4",
            "bg_color": "white",
            "border": 1,
            "locked": True,
        }),
        "header_subtitle": workbook.add_format({
            "bold": 1,
            "align": "center",
            "valign": "vcenter",
            "font_size": 12,
            "font_name": "Trebuchet MS",
            "font_color": "#4472C4",
            "bg_color": "white",
            "locked": True,
        }),
        "header_meta": workbook.add_format({
            "align": "center",
            "valign": "vcenter",
            "font_size": 11,
            "font_name": "Trebuchet MS",
            "font_color": "#4472C4",
            "bg_color": "white",
            "locked": True,
        }),
        "checkpoint_header": workbook.add_format({
            "bold": 1,
            "align": "center",
            "valign": "vcenter",
            "text_wrap": True,
            "font_name": "Trebuchet MS",
            "font_color": "white",
            "fg_color": "#F4C16B",
            "border": 1,
            "locked": True,
        }),
        "compliance": workbook.add_format({
            "bold": 1,
            "align": "center",
            "valign": "vcenter",
            "text_wrap": True,
            "font_name": "Trebuchet MS",
            "fg_color": "#AAFF00",
            "border": 1,
            "locked": True,
        }),
        "non_compliance": workbook.add_format({
            "bold": 1,
            "align": "center",
            "valign": "vcenter",
            "text_wrap": True,
            "font_name": "Trebuchet MS",
            "fg_color": "#FF0000",
            "border": 1,
            "locked": True,
        }),
        "summary_label": workbook.add_format({
            "bold": 1,
            "align": "center",
            "valign": "vcenter",
            "font_name": "Trebuchet MS",
            "border": 1,
            "locked": True,
        }),
        "summary_value": workbook.add_format({
            "align": "center",
            "valign": "vcenter",
            "text_wrap": True,
            "font_name": "Trebuchet MS",
            "border": 1,
            "locked": True,
        }),
        "summary_link": workbook.add_format({
            "bold": 1,
            "align": "center",
            "valign": "vcenter",
            "text_wrap": True,
            "font_name": "Trebuchet MS",
            "font_color": "#0563C1",
            "underline": 0,
            "border": 1,
            "locked": True,
        }),
        "nav_link": workbook.add_format({
            "bold": 1,
            "align": "left",
            "valign": "vcenter",
            "font_name": "Trebuchet MS",
            "font_color": "#0563C1",
            "underline": 0,
            "locked": True,
        }),
        "raw_content": workbook.add_format({
            "align": "left",
            "valign": "top",
            "text_wrap": True,
            "font_name": "Courier New",
            "border": 1,
            "locked": True,
        }),
    }


def apply_sheet_protection(worksheet):
    worksheet.protect(SATSOC_SHEET_PASSWORD)


def write_sheet_header(
    worksheet,
    customer_name: str,
    site_name: str,
    timestamp: str,
    username: str,
    section_label: str,
    formats: dict[str, xlsxwriter.format.Format],
    middle_column_width: float = 24,
):
    left_logo_column_width = 24
    header_row_heights = {
        0: 24,
        1: 10,
        2: 24,
        3: 24,
        4: 24,
        5: 24,
        6: 10,
        7: 24,
    }

    worksheet.set_column("A:A", left_logo_column_width)
    worksheet.set_column("B:F", middle_column_width)

    for row_index, row_height in header_row_heights.items():
        worksheet.set_row(row_index, row_height)

    worksheet.merge_range(0, 0, 2, 0, "", formats["header_meta"])
    worksheet.merge_range(0, 1, 0, 5, "POSTCHECK REPORT", formats["header_title"])
    worksheet.merge_range(2, 1, 2, 5, format_report_timestamp(timestamp), formats["header_meta"])
    worksheet.merge_range(
        3,
        1,
        3,
        5,
        f"Execution Initiated by {str(username or '').strip() or 'Unknown'}",
        formats["header_meta"],
    )
    worksheet.merge_range(
        4,
        1,
        4,
        5,
        f"Customer Name : {str(customer_name or '').strip() or 'Customer'}",
        formats["header_meta"],
    )
    worksheet.merge_range(
        5,
        1,
        5,
        5,
        f"Site Name : {str(site_name or '').strip() or 'Site'}",
        formats["header_meta"],
    )
    worksheet.write_blank(6, 0, None, formats["header_meta"])

    left_logo_path = resolve_header_logo_path("tata_left_logo.png")
    logger.debug("Resolved left logo path for %s: %s", section_label, left_logo_path or "<missing>")
    header_target_height_px = sum(row_height_to_pixels(height) for height in (24, 10, 24))

    if left_logo_path:
        worksheet.insert_image(
            0,
            0,
            left_logo_path,
            build_image_fit_options(
                left_logo_path,
                column_width_to_pixels(left_logo_column_width),
                header_target_height_px,
                preserve_aspect_ratio=False,
            ),
        )
    else:
        worksheet.merge_range(0, 0, 2, 0, "TATA", formats["header_title"])

    worksheet.merge_range(SECTION_TITLE_ROW, 2, SECTION_TITLE_ROW, 3, section_label, formats["section_title"])


def add_summary_navigation_link(worksheet, formats: dict[str, xlsxwriter.format.Format]) -> None:
    worksheet.write_url(4, 0, "internal:'Summary'!A1", formats["nav_link"], string="Go to Summary")


def write_checkpoint_grid(
    worksheet,
    start_row: int,
    checkpoint_headers: list[str],
    checkpoint_results: dict,
    formats: dict[str, xlsxwriter.format.Format],
):
    worksheet.set_column("A:F", 28)

    current_row = start_row
    for chunk_start in range(0, len(checkpoint_headers), 6):
        chunk = checkpoint_headers[chunk_start:chunk_start + 6]

        for column_index, checkpoint_name in enumerate(chunk):
            worksheet.write(current_row, column_index, checkpoint_name, formats["checkpoint_header"])

        status_row = current_row + 1
        for column_index, checkpoint_name in enumerate(chunk):
            status_value = normalize_checkpoint_result(
                checkpoint_results.get(checkpoint_name, "Pending")
            )
            if status_value == "Compliance":
                cell_format = formats["compliance"]
            else:
                cell_format = formats["non_compliance"]
            worksheet.write(status_row, column_index, status_value, cell_format)

        blank_row = current_row + 2
        for column_index in range(6):
            worksheet.write(blank_row, column_index, "")

        worksheet.set_row(current_row, 32)
        worksheet.set_row(status_row, 32)
        worksheet.set_row(blank_row, 12)
        current_row += 3

    return current_row


def write_summary_sheet_layout(
    worksheet,
    formats: dict[str, xlsxwriter.format.Format],
):
    worksheet.set_column("A:A", 24)
    worksheet.set_column("B:B", 22)
    worksheet.set_column("C:C", 24)
    worksheet.set_column("D:D", 18)
    worksheet.set_column("E:E", 12)
    worksheet.set_column("F:F", 24)
    worksheet.set_column("G:G", 24)
    worksheet.set_column("H:H", 18)
    worksheet.set_column("I:I", 22)

    for row_index, row_height in {
        0: 24,
        1: 10,
        2: 24,
        3: 10,
        4: 24,
    }.items():
        worksheet.set_row(row_index, row_height)

    left_logo_path = resolve_header_logo_path("tata_left_logo.png")
    logger.debug("Resolved summary logo path: %s", left_logo_path or "<missing>")
    header_target_height_px = sum(row_height_to_pixels(height) for height in (24, 10, 24))
    worksheet.write_blank(0, 0, None, formats["header_meta"])
    worksheet.write_blank(1, 0, None, formats["header_meta"])
    worksheet.write_blank(2, 0, None, formats["header_meta"])

    if left_logo_path:
        worksheet.insert_image(
            0,
            0,
            left_logo_path,
            build_image_fit_options(
                left_logo_path,
                column_width_to_pixels(24),
                header_target_height_px,
                preserve_aspect_ratio=False,
            ),
        )
    else:
        worksheet.merge_range(0, 0, 2, 0, "TATA", formats["header_title"])

    worksheet.merge_range(0, 1, 0, 8, "POSTCHECK REPORT", formats["header_title"])
    worksheet.merge_range(2, 1, 2, 8, "Summary", formats["section_title"])

    header_labels = [
        "Customer Name",
        "Report Generated By",
        "Report Generated At",
        "Product Name",
        "OEM",
        "Site Name",
        "Device Type",
        "Device Count",
        "Overall Compliance",
    ]
    for column_index, label in enumerate(header_labels):
        worksheet.write(SUMMARY_HEADER_ROW, column_index, label, formats["checkpoint_header"])


def write_summary_common_columns(
    worksheet,
    start_row: int,
    end_row: int,
    values: list[str],
    cell_format: xlsxwriter.format.Format,
) -> None:
    row_count = end_row - start_row + 1
    merge_rows = row_count > 1

    for column_index, value in enumerate(values):
        if merge_rows:
            worksheet.merge_range(
                start_row,
                column_index,
                end_row,
                column_index,
                value,
                cell_format,
            )
        else:
            worksheet.write(start_row, column_index, value, cell_format)


def create_summary_sheet(
    workbook: xlsxwriter.Workbook,
    customer_name: str,
    group_name: str,
    timestamp: str,
    username: str,
    site_name: str,
    summary_rows: list[dict],
    formats: dict[str, xlsxwriter.format.Format],
):
    worksheet = workbook.add_worksheet("Summary")
    apply_sheet_protection(worksheet)
    write_summary_sheet_layout(worksheet, formats)

    data_row_start = SUMMARY_DATA_ROW
    data_row_count = max(len(summary_rows), 1)
    data_row_end = data_row_start + data_row_count - 1

    common_values = [
        customer_name,
        username,
        format_report_timestamp(timestamp),
        SUMMARY_PRODUCT_NAME,
        SUMMARY_OEM,
        site_name or "",
    ]

    write_summary_common_columns(
        worksheet,
        data_row_start,
        data_row_end,
        common_values,
        formats["summary_value"],
    )

    for row_offset, summary_row in enumerate(summary_rows):
        current_row = data_row_start + row_offset
        worksheet.set_row(current_row, 35)
        target_sheet_name = summary_row.get("target_sheet_name")
        if target_sheet_name:
            worksheet.write_url(
                current_row,
                6,
                f"internal:'{target_sheet_name}'!A1",
                formats["summary_link"],
                string=summary_row["device_type"],
            )
        else:
            worksheet.write(current_row, 6, summary_row["device_type"], formats["summary_value"])
        device_count_value = summary_row.get("device_count")
        worksheet.write(
            current_row,
            7,
            "" if device_count_value is None else device_count_value,
            formats["summary_value"],
        )

        compliance_count = summary_row["compliance_count"]
        total_count = summary_row["total_count"]
        summary_format = (
            formats["compliance"]
            if total_count and compliance_count == total_count
            else formats["non_compliance"]
        )
        worksheet.write(current_row, 8, summary_row["overall_compliance"], summary_format)

    worksheet.activate()


def create_ap_sheet(
    workbook: xlsxwriter.Workbook,
    customer_name: str,
    group_name: str,
    site_name: str,
    ap_results: dict,
    raw_ap_results: dict,
    timestamp: str,
    username: str,
    formats: dict[str, xlsxwriter.format.Format],
):
    worksheet = workbook.add_worksheet("AP")
    apply_sheet_protection(worksheet)
    write_sheet_header(worksheet, customer_name, site_name, timestamp, username, "APs", formats)
    add_summary_navigation_link(worksheet, formats)

    next_row = write_checkpoint_grid(
        worksheet,
        DEVICE_CONTENT_START_ROW,
        AP_CHECKPOINT_HEADERS,
        ap_results,
        formats,
    )

    raw_section_title_row = next_row + 4
    raw_section_content_row = raw_section_title_row + 1
    raw_result_text = json.dumps(raw_ap_results or {}, indent=2)

    worksheet.merge_range(
        raw_section_title_row,
        0,
        raw_section_title_row,
        5,
        "AP Result",
        formats["checkpoint_header"],
    )
    worksheet.merge_range(
        raw_section_content_row,
        0,
        raw_section_content_row + 14,
        5,
        raw_result_text,
        formats["raw_content"],
    )
    for row_index in range(raw_section_content_row, raw_section_content_row + 15):
        worksheet.set_row(row_index, 22)


def create_switch_sheet(
    workbook: xlsxwriter.Workbook,
    customer_name: str,
    group_name: str,
    site_name: str,
    switch_results: dict,
    raw_switch_results: dict,
    timestamp: str,
    username: str,
    formats: dict[str, xlsxwriter.format.Format],
):
    worksheet = workbook.add_worksheet("Switches")
    apply_sheet_protection(worksheet)
    write_sheet_header(worksheet, customer_name, site_name, timestamp, username, "Switches", formats)
    add_summary_navigation_link(worksheet, formats)

    next_row = write_checkpoint_grid(
        worksheet,
        DEVICE_CONTENT_START_ROW,
        SWITCH_CHECKPOINT_HEADERS,
        switch_results,
        formats,
    )

    raw_section_title_row = next_row + 4
    raw_section_content_row = raw_section_title_row + 1
    raw_result_text = json.dumps(raw_switch_results or {}, indent=2)

    worksheet.merge_range(
        raw_section_title_row,
        0,
        raw_section_title_row,
        5,
        "Switches Result",
        formats["checkpoint_header"],
    )
    worksheet.merge_range(
        raw_section_content_row,
        0,
        raw_section_content_row + 14,
        5,
        raw_result_text,
        formats["raw_content"],
    )
    for row_index in range(raw_section_content_row, raw_section_content_row + 15):
        worksheet.set_row(row_index, 22)


# def lock_workbook(report_path: Path):
#     report_file = windows_long_path(report_path)
#     workbook = load_workbook(report_file)
#     workbook.security = WorkbookProtection(lockStructure=True)
#     workbook.security.set_workbook_password(SATSOC_SHEET_PASSWORD)

#     for worksheet in workbook.worksheets:
#         worksheet.protection.sheet = True
#         worksheet.protection.password = SATSOC_SHEET_PASSWORD
#         worksheet.protection.enable()

#     workbook.save(report_file)


def lock_workbook(report_path: Path):
    return











def build_satsoc_presentation_layer(
    base_output_dir,
    customer_name,
    group_name,
    username,
    device_scope,
    site_name=None,
    ap_results=None,
    switch_results=None,
    workbook_timestamp=None,
):
    logger.info(
        "Starting presentation workbook generation for customer=%s group=%s scope=%s",
        customer_name or "Unknown",
        group_name or "Unknown",
        device_scope or "unknown",
    )
    ap_results = ap_results or {}
    switch_results = switch_results or {}
    ap_sheet_results = remap_results(ap_results, AP_RESULT_KEY_MAP)
    switch_sheet_results = remap_results(switch_results, SWITCH_RESULT_KEY_MAP)
    include_ap_sheet = device_scope in ["ap", "both"]
    include_switch_sheet = device_scope in ["switch", "both"]

    timestamp = workbook_timestamp or datetime.datetime.now().strftime("%d-%m-%Y_%H-%M-%S")
    safe_customer_name = sanitize_excel_filename_component(customer_name, "Unknown_Customer")
    safe_group_name = sanitize_excel_filename_component(group_name, "Unknown_Group")
    file_name = f"{safe_customer_name}_{safe_group_name}_HPE Aruba_SATSOC_{timestamp}.xlsx"

    output_dir = Path(base_output_dir).expanduser()
    if not output_dir.is_absolute():
        output_dir = (Path(__file__).resolve().parent.parent / output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / file_name

    workbook = xlsxwriter.Workbook(windows_long_path(report_path))
    formats = add_formats(workbook)

    api_response_dir = output_dir.parent / "API response"
    summary_rows = []

    if include_ap_sheet:
        compliance_count, total_count = count_compliance(ap_sheet_results, AP_CHECKPOINT_HEADERS)
        summary_rows.append(
            {
                "device_type": "AP",
                "target_sheet_name": "AP",
                "device_count": load_inventory_count(api_response_dir, "AP.json", "ap_inventory"),
                "compliance_count": compliance_count,
                "total_count": total_count,
                "overall_compliance": f"{compliance_count}/{total_count}",
            }
        )
    if include_switch_sheet:
        compliance_count, total_count = count_compliance(switch_sheet_results, SWITCH_CHECKPOINT_HEADERS)
        summary_rows.append(
            {
                "device_type": "Switches",
                "target_sheet_name": "Switches",
                "device_count": load_inventory_count(api_response_dir, "switch.json", "switch_inventory"),
                "compliance_count": compliance_count,
                "total_count": total_count,
                "overall_compliance": f"{compliance_count}/{total_count}",
            }
        )
    if summary_rows:
        create_summary_sheet(
            workbook,
            customer_name,
            group_name,
            timestamp,
            username,
            str(site_name or "").strip(),
            summary_rows,
            formats,
        )
    if include_ap_sheet:
        create_ap_sheet(
            workbook,
            customer_name,
            group_name,
            site_name,
            ap_sheet_results,
            ap_results,
            timestamp,
            username,
            formats,
        )
    if include_switch_sheet:
        create_switch_sheet(
            workbook,
            customer_name,
            group_name,
            site_name,
            switch_sheet_results,
            switch_results,
            timestamp,
            username,
            formats,
        )

    try:
        workbook.close()
        lock_workbook(report_path)
    except Exception:
        logger.exception(
            "Failed to generate presentation workbook at %s",
            report_path,
        )
        raise

    logger.info("Presentation workbook generated successfully at %s", report_path)
    return str(report_path)
