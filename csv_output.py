"""CSV schema and collision-safe exports; no browser or collector dependencies."""
import csv
import re
import tempfile
from pathlib import Path

CSV_FIELDS = [
    'name', 'phone', 'email', 'website', 'address', 'instagram', 'facebook',
    'twitter', 'linkedin', 'tiktok', 'yelp', 'youtube', 'place_id', 'cid',
    'category', 'reviews', 'rating', 'latitude', 'longitude', 'monday',
    'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday',
    'additional_emails', 'maps_url',
]


def query_to_filename(query):
    return re.sub(r'\s+', '_', re.sub(r'[^a-z0-9_\s]', '', query.lower().strip())) or 'maps_results'


def save_results(leads, query, output_dir, fields=None):
    fields = CSV_FIELDS if fields is None else list(fields)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stem = query_to_filename(query)
    index = 1
    while True:
        path = directory / (stem + (f'_{index}' if index > 1 else '') + '.csv')
        try:
            stream = path.open('x', encoding='utf-8', newline='')
            break
        except FileExistsError:
            index += 1
    with stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows({field: lead.get(field, '') for field in fields} for lead in leads)
    return path


def excel_bytes(leads, fields, criteria):
    """Produce a real XLSX with full-row status fills and a criteria sheet."""
    import io
    from openpyxl import Workbook
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    workbook = Workbook()
    workbook.properties.creator = "Metering Lead Finder"
    workbook.properties.title = "Maps-based property-management screening"
    sheet = workbook.active
    sheet.title = "Qualified leads"
    fills = {
        "Accepted": PatternFill("solid", fgColor="C6EFCE"),
        "Rejected": PatternFill("solid", fgColor="FFC7CE"),
    }
    header_fill = PatternFill("solid", fgColor="17365D")
    widths = {
        "name": 38, "qualification_status": 20, "phone": 22, "email": 38,
        "qualification_reason": 78, "address": 38, "website": 45,
        "property_management_match": 30, "berlin_relevance": 38,
        "portfolio_size": 38, "metering_service_fit": 44, "kpi_basis": 48,
        "kpi_source": 45,
    }
    wrapped = {"name", "qualification_reason", "property_management_match",
               "berlin_relevance", "portfolio_size", "metering_service_fit", "kpi_basis"}

    def literal_cell(worksheet, row, column, value):
        # Never interpret website/name/contact strings as Excel formulas.
        value = ILLEGAL_CHARACTERS_RE.sub("", str(value if value is not None else ""))
        cell = worksheet.cell(row, column, value)
        cell.data_type = "s"
        cell.number_format = "@"
        cell.font = Font(name="Arial", size=10, color="182B3A")
        return cell

    for column, field in enumerate(fields, 1):
        cell = literal_cell(sheet, 1, column, field)
        cell.font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        sheet.column_dimensions[get_column_letter(column)].width = widths.get(field, 22)
    sheet.row_dimensions[1].height = 32
    for row_number, lead in enumerate(leads, 2):
        fill = fills.get(lead.get("qualification_status"))
        for column, field in enumerate(fields, 1):
            cell = literal_cell(sheet, row_number, column, lead.get(field, ""))
            cell.alignment = Alignment(wrap_text=field in wrapped, vertical="top")
            if fill is not None:
                cell.fill = fill
        sheet.row_dimensions[row_number].height = 54
    sheet.freeze_panes = "E2"
    sheet.auto_filter.ref = sheet.dimensions
    sheet.sheet_view.showGridLines = False

    notes = workbook.create_sheet("KPI criteria")
    for row_number, (key, value) in enumerate([("Criterion", "Definition"), *criteria.items()], 1):
        for column, text in enumerate((key, value), 1):
            cell = literal_cell(notes, row_number, column, text)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            if row_number == 1:
                cell.font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
                cell.fill = header_fill
        notes.row_dimensions[row_number].height = 65 if row_number > 1 else 25
    notes.column_dimensions["A"].width = 24
    notes.column_dimensions["B"].width = 110
    notes.freeze_panes = "A2"
    notes.sheet_view.showGridLines = False
    stream = io.BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def replace_results(leads, path):
    """Atomically refresh an already-allocated enrichment export, never raw data."""
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="",
                                         dir=path.parent, prefix=".email-checkpoint-",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows({field: lead.get(field, "") for field in CSV_FIELDS} for lead in leads)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path
