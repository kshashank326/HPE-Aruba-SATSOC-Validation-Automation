# SATSOC Validation Tool

SATSOC Validation Tool is a FastAPI-based web application for network assessment and validation workflows. It guides users through a checkpoint-driven process for Aruba Central and GreenLake environments, validates AP and switch data, and generates structured Excel reports for each run.

The application is designed for repeatable SATSOC-style assessments where inputs, API responses, logs, and final reports need to stay organized and traceable. It stores run-specific artifacts on disk, supports token refresh workflows, and provides a browser-based interface for completing the validation flow.

## What It Does

- Validates AP and switch data against predefined checkpoints
- Pulls data from Aruba Central and GreenLake APIs using cached access tokens
- Generates Excel reports for completed validation runs
- Stores run inputs, logs, API responses, and outputs in per-run folders
- Archives generated reports for later review
- Supports a guided web UI for end-to-end validation

## Key Features

- FastAPI web application with Jinja2 templates
- AP and switch checkpoint validation logic
- Excel report generation with formatted output
- Token management and refresh support for Aruba Central and GreenLake
- Audit logging and execution history storage
- Optional database audit insertion for run metadata

## Prerequisites

- Python 3.11 or newer
- Access to the Aruba Central and GreenLake credentials/tokens used by the workflow
- Optional MySQL access if you want audit records to be inserted into a database

## Installation

1. Clone the repository.
2. Create and activate a virtual environment:

```bash
python -m venv .venv
.venv\Scripts\activate
```

3. Install dependencies:

```bash
pip install -r requirements.txt
```

## Configuration

Create a `.env` file in the project root if you need to override the defaults.

```env
BASE_ROUTE=/M-Wifi-LAN/hpe-aruba
DB_HOST=localhost
DB_USER=root
DB_NAME=sdsatsoc
DB_PASSWORD=your_password
```

### Environment Variables

- `BASE_ROUTE` sets the public base path for the app. The default is `/M-Wifi-LAN/hpe-aruba`.
- `DB_HOST`, `DB_USER`, `DB_NAME`, and `DB_PASSWORD` are used for optional audit database inserts.

### Token Files

The application reads and refreshes token records from:

- `AccessTokens/aruba.txt`
- `AccessTokens/greenlake.txt`

Each file should contain the token metadata expected by the app, including API configuration and access token values. The application can refresh these tokens automatically when they are near expiry.

## Running the App

Start the application with:

```bash
python app.py
```

By default, the app runs on:

```text
http://127.0.0.1:5018
```

If `BASE_ROUTE` is set, the app is mounted under that route in deployment mode.

## Typical Workflow

1. Open the app in a browser.
2. Enter the required project, customer, and device details.
3. Complete the AP and switch checkpoint pages.
4. Review validation results and generated output.
5. Download or inspect the final Excel report.

## Project Structure

- `app.py` - main FastAPI application and route definitions
- `checkpoints/` - checkpoint logic for APs, switches, and API bootstrap flows
- `presentation_layer/` - report generation and presentation helpers
- `templates/` - HTML templates for the user interface
- `AccessTokens/` - cached API token records and token logic
- `executions/` - per-run inputs, logs, API responses, and generated outputs
- `backupReports/` - archived Excel reports
- `logos/` - static image assets used by the UI

## Notes

- Generated execution folders and backup reports can grow over time, so they are usually excluded from version control.
- If a token expires during a run, the app shows a token-expired message and expects the token to be refreshed before retrying the workflow.
- The audit database insert is optional and failures in that step do not stop the main validation flow.
