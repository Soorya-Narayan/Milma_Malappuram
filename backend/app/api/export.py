"""GET /api/export.xlsx, /api/export.csv, /api/export.pdf

All three exports share the same pivoted shape as the on-screen History view:

    Timestamp | <CH1 name> | <CH2 name> | ... | <CHN name>

Only enabled channels are included. Timestamps are formatted in the
configured local timezone (``Settings.tz``, default Asia/Kolkata).

- XLSX: openpyxl write-only, header ``#104861`` fill, row cap 500 000.
- CSV:  pure stdlib ``csv``, streamed row-by-row, no cap.
- PDF:  fpdf2 landscape A4, row cap 5 000 (larger ranges should use CSV/XLSX).
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Font, NamedStyle, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import text

from app.config import get_settings
from app.db import AsyncSessionLocal

router = APIRouter(prefix="/api", tags=["export"])

# Goose logo, co-located with the rest of the static assets. Used in the
# top-right of both the XLSX and PDF exports for brand consistency with
# the on-screen dashboard nav bar.
_LOGO_PATH = Path(__file__).resolve().parent.parent / "static" / "img" / "goose-logo.png"

ROW_CAP = 500_000
PDF_ROW_CAP = 5_000
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_CSV_MIME = "text/csv; charset=utf-8"
_PDF_MIME = "application/pdf"

# aRGB hex — leading "FF" is full opacity.
_HEADER_FONT = Font(bold=True, color="FFFFFFFF")
_HEADER_FILL = PatternFill("solid", fgColor="FF104861")
_CENTER = Alignment(horizontal="center", vertical="center")
_BODY_ALIGN = Alignment(horizontal="center", vertical="center")


def _latin1(s: str) -> str:
    """Coerce arbitrary text to latin-1 for core PDF fonts.

    fpdf2's Helvetica/Times/Courier core fonts accept latin-1 only.
    Operator-entered channel names or device names may contain emoji or
    other non-latin chars; replace anything unrepresentable with "?" so
    the export doesn't 500. For a Unicode-perfect PDF we'd ship a TTF
    (DejaVu) and register it with add_font, but that's ~1 MB per export
    — overkill for what the operator actually needs to read.
    """
    return s.encode("latin-1", errors="replace").decode("latin-1")


def _local_tz() -> ZoneInfo:
    try:
        return ZoneInfo(get_settings().tz)
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


async def _load_device_name(device_id: int) -> str:
    async with AsyncSessionLocal() as session:
        r = await session.execute(
            text("SELECT name FROM devices WHERE id = :id"), {"id": device_id}
        )
        name = r.scalar_one_or_none()
        if name is None:
            raise HTTPException(status_code=404, detail="device not found")
        return str(name)


async def _load_channel_headers(device_id: int) -> tuple[list[int], list[str]]:
    """Return (enabled_channels, labels) — only channels the operator has enabled.

    Disabled channels are excluded from the export entirely, matching the
    History page behaviour. Ordering is ascending by channel number.
    """
    async with AsyncSessionLocal() as session:
        exists = await session.execute(
            text("SELECT name FROM devices WHERE id = :id"), {"id": device_id}
        )
        if exists.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="device not found")

        rows = (
            await session.execute(
                text(
                    "SELECT channel, name, unit, enabled FROM channels "
                    "WHERE device_id = :id ORDER BY channel"
                ),
                {"id": device_id},
            )
        ).all()

    enabled_channels: list[int] = []
    labels: list[str] = []
    for r in rows:
        if not r.enabled:
            continue
        label = r.name
        if r.unit:
            label = f"{label} ({r.unit})"
        enabled_channels.append(r.channel)
        labels.append(label)
    return enabled_channels, labels


async def _stream_pivot(device_id: int, from_ms: int, to_ms: int):
    """Yield one pivoted row per timestamp (ts, ch1..ch8) in ascending order."""
    sql = text(
        """
        SELECT ts,
            MAX(CASE WHEN channel=1 THEN pv END) AS ch1,
            MAX(CASE WHEN channel=2 THEN pv END) AS ch2,
            MAX(CASE WHEN channel=3 THEN pv END) AS ch3,
            MAX(CASE WHEN channel=4 THEN pv END) AS ch4,
            MAX(CASE WHEN channel=5 THEN pv END) AS ch5,
            MAX(CASE WHEN channel=6 THEN pv END) AS ch6,
            MAX(CASE WHEN channel=7 THEN pv END) AS ch7,
            MAX(CASE WHEN channel=8 THEN pv END) AS ch8
        FROM readings
        WHERE device_id = :d
          AND ts >= :f AND ts < :t
        GROUP BY ts
        ORDER BY ts ASC
        """
    )
    async with AsyncSessionLocal() as session:
        result = await session.stream(
            sql, {"d": device_id, "f": from_ms, "t": to_ms}
        )
        async for row in result:
            yield row


def _base_filename(from_: datetime, to: datetime) -> str:
    """Local-time filename stub shared by all export formats."""
    tz = _local_tz()
    from_local = from_.astimezone(tz)
    to_local = to.astimezone(tz)
    return (
        f"Temperature From {from_local.strftime('%Y-%m-%d %H-%M')} "
        f"to {to_local.strftime('%Y-%m-%d %H-%M')}"
    )


# =========================================================================
# XLSX
# =========================================================================

def _styled_header_cell(ws, value: str) -> WriteOnlyCell:
    c = WriteOnlyCell(ws, value=value)
    c.font = _HEADER_FONT
    c.fill = _HEADER_FILL
    c.alignment = _CENTER
    return c


# Two named styles registered once on the workbook and referenced by name
# for every body cell. openpyxl stores these as shared XF records in the
# .xlsx, so writing 100 000+ rows doesn't serialise 100 000 × 8 alignment
# objects — it just writes a style index. This is the single biggest
# perf win for the export: 24h of 1-s data went from ~15 s to ~1 s.
_STYLE_NUM = "aime_num"
_STYLE_TS = "aime_ts"


def _register_body_styles(wb: Workbook) -> None:
    if _STYLE_NUM not in wb.named_styles:
        ns = NamedStyle(name=_STYLE_NUM)
        ns.number_format = "0.00"
        ns.alignment = _BODY_ALIGN
        wb.add_named_style(ns)
    if _STYLE_TS not in wb.named_styles:
        ns = NamedStyle(name=_STYLE_TS)
        ns.alignment = _BODY_ALIGN
        wb.add_named_style(ns)


def _num_cell(ws, value: float | None) -> WriteOnlyCell:
    c = WriteOnlyCell(ws, value=value)
    c.style = _STYLE_NUM
    return c


def _ts_cell(ws, value: str) -> WriteOnlyCell:
    c = WriteOnlyCell(ws, value=value)
    c.style = _STYLE_TS
    return c


async def _build_workbook(
    device_id: int,
    from_: datetime,
    to: datetime,
) -> tuple[io.BytesIO, int, bool]:
    """Build the pivoted workbook. Returns (buf, rows_written, truncated).

    Layout:
        A1 = "From: <from date>"
        A2 = "To:   <to date>"
        Row 3 = header row starting at A3 ("Timestamp" | <channel headers>...)
        Row 4+ = data rows starting at A4
        Goose logo anchored at cell F1.
    """
    from_ms = int(from_.timestamp() * 1000)
    to_ms = int(to.timestamp() * 1000)

    enabled_channels, channel_headers = await _load_channel_headers(device_id)

    wb = Workbook(write_only=True)
    _register_body_styles(wb)
    ws = wb.create_sheet("History")

    # In openpyxl write-only mode, freeze_panes must be set BEFORE any
    # rows are appended — assigning it after ws.append() silently drops
    # it on save, leaving the output unfrozen. Pin rows 1-3 (range
    # labels + header) and column A (timestamp) so they stay visible
    # while the operator scrolls.
    ws.freeze_panes = "B4"

    tz = _local_tz()
    from_text = f"From: {from_.astimezone(tz).strftime('%Y-%m-%d %H:%M:%S')}"
    to_text = f"To:   {to.astimezone(tz).strftime('%Y-%m-%d %H:%M:%S')}"

    # Data grid starts at column A row 3. A is the Timestamp column.
    ws.column_dimensions["A"].width = 22           # timestamp column
    for i in range(len(enabled_channels)):
        ws.column_dimensions[get_column_letter(2 + i)].width = 14

    # Row heights: rows 1/2 normal (range labels), row 3 taller (header).
    ws.row_dimensions[3].height = 30

    range_font = Font(bold=True)

    def _range_cell(text_: str) -> WriteOnlyCell:
        c = WriteOnlyCell(ws, value=text_)
        c.font = range_font
        c.alignment = Alignment(horizontal="left", vertical="center")
        return c

    # ---------- Row 1: from date ----------
    ws.append([_range_cell(from_text)])

    # ---------- Row 2: to date ----------
    ws.append([_range_cell(to_text)])

    # ---------- Row 3: header row at A3 ----------
    header_row: list = [_styled_header_cell(ws, "Timestamp")]
    for h in channel_headers:
        header_row.append(_styled_header_cell(ws, h))
    ws.append(header_row)

    # ---------- Rows 4+: data starting at A4 ----------
    rows_written = 0
    truncated = False

    async for row in _stream_pivot(device_id, from_ms, to_ms):
        if rows_written >= ROW_CAP:
            truncated = True
            break
        dt_local = datetime.fromtimestamp(row.ts / 1000, tz=timezone.utc).astimezone(tz)
        cells: list = [_ts_cell(ws, dt_local.strftime("%Y-%m-%d %H:%M:%S"))]
        for ch in enabled_channels:
            cells.append(_num_cell(ws, getattr(row, f"ch{ch}")))
        ws.append(cells)
        rows_written += 1

    data_last_row = 3 + rows_written  # header is row 3
    last_col_letter = get_column_letter(1 + len(enabled_channels))
    ws.auto_filter.ref = f"A3:{last_col_letter}{max(data_last_row, 3)}"

    # ---------- Goose logo at F1 ----------
    if _LOGO_PATH.exists():
        try:
            img = XLImage(str(_LOGO_PATH))
            # Scale to ~120 px wide preserving aspect ratio — logo is
            # ~960×320 in the static dir, so height ≈ 40 px fits row 1.
            scale = 120 / max(img.width, 1)
            img.width = int(img.width * scale)
            img.height = int(img.height * scale)
            img.anchor = "F1"
            ws.add_image(img)
        except Exception:  # noqa: BLE001
            # Logo is cosmetic — never fail the export because of it.
            logging.getLogger(__name__).exception("xlsx logo embed failed")

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf, rows_written, truncated


def _chunked(buf: io.BytesIO, chunk_size: int = 65536):
    """Yield a BytesIO in fixed-size chunks.

    StreamingResponse's default iteration over a BytesIO splits on
    newlines, which is wrong for binary content — this gives
    deterministic 64 KiB chunks instead.
    """
    while True:
        data = buf.read(chunk_size)
        if not data:
            return
        yield data


@router.get("/export.xlsx")
async def export_xlsx(
    device_id: int = Query(..., gt=0),
    from_: datetime = Query(..., alias="from"),
    to: datetime = Query(...),
):
    if to <= from_:
        raise HTTPException(status_code=400, detail="`to` must be after `from`")

    buf, rows, truncated = await _build_workbook(device_id, from_, to)

    filename = _base_filename(from_, to) + ".xlsx"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-Row-Count": str(rows),
        "X-Truncated": "true" if truncated else "false",
    }
    logging.getLogger(__name__).info(
        "xlsx export device=%d rows=%d truncated=%s", device_id, rows, truncated
    )
    return StreamingResponse(_chunked(buf), media_type=_XLSX_MIME, headers=headers)


# =========================================================================
# CSV
# =========================================================================

@router.get("/export.csv")
async def export_csv(
    device_id: int = Query(..., gt=0),
    from_: datetime = Query(..., alias="from"),
    to: datetime = Query(...),
):
    """Stream the same pivoted view as a CSV.

    Much cheaper than XLSX (no workbook serialisation, no shared styles)
    and streamed row-by-row so the process RSS stays flat regardless of
    range size. No hard row cap — CSV is an append-only stream.
    """
    if to <= from_:
        raise HTTPException(status_code=400, detail="`to` must be after `from`")

    from_ms = int(from_.timestamp() * 1000)
    to_ms = int(to.timestamp() * 1000)
    enabled_channels, channel_headers = await _load_channel_headers(device_id)
    tz = _local_tz()

    async def _gen():
        # Write into an in-memory buffer and flush after each row — this
        # lets csv.writer handle quoting / escaping for us while still
        # streaming a chunk at a time to the client.
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(["Timestamp", *channel_headers])
        yield buf.getvalue().encode("utf-8")
        buf.seek(0)
        buf.truncate(0)

        rows = 0
        async for row in _stream_pivot(device_id, from_ms, to_ms):
            dt_local = datetime.fromtimestamp(row.ts / 1000, tz=timezone.utc).astimezone(tz)
            out = [dt_local.strftime("%Y-%m-%d %H:%M:%S")]
            for ch in enabled_channels:
                v = getattr(row, f"ch{ch}")
                out.append("" if v is None else f"{v:.2f}")
            w.writerow(out)
            rows += 1
            # Flush every 500 rows so large exports don't build a big
            # StringIO — keeps peak memory tiny.
            if rows % 500 == 0:
                yield buf.getvalue().encode("utf-8")
                buf.seek(0)
                buf.truncate(0)

        if buf.tell():
            yield buf.getvalue().encode("utf-8")
        logging.getLogger(__name__).info("csv export device=%d rows=%d", device_id, rows)

    filename = _base_filename(from_, to) + ".csv"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(_gen(), media_type=_CSV_MIME, headers=headers)


# =========================================================================
# PDF
# =========================================================================

async def _build_pdf(
    device_id: int,
    from_: datetime,
    to: datetime,
) -> tuple[bytes, int, bool, str]:
    """Build a landscape A4 PDF. Returns (pdf_bytes, rows, truncated, device_name)."""
    # Local import so the app still starts if fpdf2 isn't installed yet.
    from fpdf import FPDF

    from_ms = int(from_.timestamp() * 1000)
    to_ms = int(to.timestamp() * 1000)
    device_name = await _load_device_name(device_id)
    enabled_channels, channel_headers = await _load_channel_headers(device_id)
    tz = _local_tz()

    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.set_margins(left=10, top=10, right=10)
    pdf.add_page()

    # ---- Goose logo top-right ----
    # Landscape A4 page width = 297 mm. Place a ~40 mm logo flush with the
    # right margin, 6 mm below the top edge. Errors here are non-fatal —
    # the export works without the logo.
    if _LOGO_PATH.exists():
        try:
            logo_w = 40
            page_w = 297
            right_margin = 10
            pdf.image(str(_LOGO_PATH), x=page_w - right_margin - logo_w, y=6, w=logo_w)
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception("pdf logo embed failed")

    # ---- title block ----
    pdf.set_font("Helvetica", "B", 14)
    # Helvetica core font is latin-1 only, so keep punctuation ASCII-safe
    # (no em-dash). "°" is in latin-1 and is fine.
    pdf.cell(0, 7, f"Milma Malappuram - {_latin1(device_name)}", ln=1)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(90, 90, 90)
    from_local = from_.astimezone(tz)
    to_local = to.astimezone(tz)
    pdf.cell(
        0, 5,
        f"Range: {from_local.strftime('%Y-%m-%d %H:%M')}  to  "
        f"{to_local.strftime('%Y-%m-%d %H:%M')}  ({tz.key})",
        ln=1,
    )
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)

    # ---- table ----
    # 297mm usable width - 20mm margins = 277mm. First col 38mm, rest even.
    usable = 277
    ts_w = 38
    ch_w = (usable - ts_w) / max(len(enabled_channels), 1)
    row_h = 6

    def _draw_header():
        pdf.set_fill_color(16, 72, 97)         # #104861 — same as xlsx header
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(ts_w, row_h, "Timestamp", border=1, align="C", fill=True)
        for h in channel_headers:
            pdf.cell(ch_w, row_h, _latin1(h), border=1, align="C", fill=True)
        pdf.ln(row_h)
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Helvetica", "", 8)

    _draw_header()

    rows_written = 0
    truncated = False
    zebra = False

    async for row in _stream_pivot(device_id, from_ms, to_ms):
        if rows_written >= PDF_ROW_CAP:
            truncated = True
            break

        # Repeat the header after each page break.
        if pdf.will_page_break(row_h):
            pdf.add_page()
            _draw_header()

        pdf.set_fill_color(245, 247, 250) if zebra else pdf.set_fill_color(255, 255, 255)
        zebra = not zebra

        dt_local = datetime.fromtimestamp(row.ts / 1000, tz=timezone.utc).astimezone(tz)
        pdf.cell(ts_w, row_h, dt_local.strftime("%Y-%m-%d %H:%M:%S"),
                 border=1, align="C", fill=True)
        for ch in enabled_channels:
            v = getattr(row, f"ch{ch}")
            pdf.cell(ch_w, row_h, "-" if v is None else f"{v:.2f}",
                     border=1, align="C", fill=True)
        pdf.ln(row_h)
        rows_written += 1

    if truncated:
        pdf.ln(2)
        pdf.set_font("Helvetica", "I", 8)
        pdf.set_text_color(160, 60, 60)
        pdf.cell(
            0, 5,
            f"Output truncated at {PDF_ROW_CAP:,} rows. "
            f"For the full range, use CSV or XLSX export.",
            ln=1,
        )

    # fpdf2 returns str in 1.x and bytes in 2.x; output() with no dest is bytes.
    data = pdf.output()
    if isinstance(data, bytearray):
        data = bytes(data)
    return data, rows_written, truncated, device_name


@router.get("/export.pdf")
async def export_pdf(
    device_id: int = Query(..., gt=0),
    from_: datetime = Query(..., alias="from"),
    to: datetime = Query(...),
):
    if to <= from_:
        raise HTTPException(status_code=400, detail="`to` must be after `from`")

    data, rows, truncated, _ = await _build_pdf(device_id, from_, to)
    filename = _base_filename(from_, to) + ".pdf"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-Row-Count": str(rows),
        "X-Truncated": "true" if truncated else "false",
    }
    logging.getLogger(__name__).info(
        "pdf export device=%d rows=%d truncated=%s", device_id, rows, truncated
    )
    return StreamingResponse(iter([data]), media_type=_PDF_MIME, headers=headers)
