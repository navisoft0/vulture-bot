"""Google Sheets writer (ported from v1, prints replaced with logging)."""

import logging
import traceback

import gspread

from . import clients, config

log = logging.getLogger(__name__)


def write_to_sheet(worksheet_name, rows_to_append, clear_sheet=False, header=None):
    """Append rows to a worksheet (tab) of the configured spreadsheet.

    Failures are logged, never raised — a Sheets outage must not kill a scan.
    Returns True on success.
    """
    if not rows_to_append:
        log.info("No data to write to sheet tab '%s'.", worksheet_name)
        return True

    spreadsheet_name = config.get("GOOGLE_SHEET_NAME")
    log.info(
        "Writing %d rows to Google Sheet: %s -> %s",
        len(rows_to_append), spreadsheet_name, worksheet_name,
    )
    try:
        gc = clients.gspread_client()
        spreadsheet = gc.open(spreadsheet_name)
        worksheet = spreadsheet.worksheet(worksheet_name)

        if clear_sheet:
            worksheet.clear()
            if header:
                worksheet.append_row(header)

        worksheet.append_rows(rows_to_append)
        return True
    except gspread.exceptions.SpreadsheetNotFound:
        log.error("Spreadsheet '%s' not found. Check the name and sharing permissions.", spreadsheet_name)
    except gspread.exceptions.WorksheetNotFound:
        log.error("Worksheet (tab) '%s' not found in spreadsheet '%s'.", worksheet_name, spreadsheet_name)
    except gspread.exceptions.APIError as e:
        log.error("Google Sheets API error: %s", e)
    except Exception:
        log.error("Unexpected error writing to Google Sheets:\n%s", traceback.format_exc())
    return False


def read_column(worksheet_name, col=1):
    """Read a column of values from a tab; returns [] on any failure."""
    try:
        gc = clients.gspread_client()
        worksheet = gc.open(config.get("GOOGLE_SHEET_NAME")).worksheet(worksheet_name)
        return [v for v in worksheet.col_values(col) if v]
    except Exception as e:
        log.warning("Could not read sheet tab '%s': %s", worksheet_name, e)
        return []
