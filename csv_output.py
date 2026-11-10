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


def save_results(leads, query, output_dir):
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
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows({field: lead.get(field, '') for field in CSV_FIELDS} for lead in leads)
    return path


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
