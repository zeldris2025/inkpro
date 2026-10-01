"""Bulk-import the "INKPRO Recharge Register" spreadsheet from the command line.

The same import is on the register page ("Import CSV"); this is for scripted
or very large imports. Accepts the workbook (.xlsx) or a CSV export of it.
Rows already in the register are updated rather than duplicated — see
``main.register_import`` for how rows are matched and parsed.

Usage::

    python manage.py import_register "INKPRO Recharge Register 2026.xlsx"
    python manage.py import_register register.csv --dry-run
    python manage.py import_register register.xlsx --sheet 2026
"""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from main import register_import


class Command(BaseCommand):
    help = 'Import the Recharge Register (.xlsx or .csv) into the register.'

    def add_arguments(self, parser):
        parser.add_argument('path', help='Path to the .xlsx workbook or .csv export.')
        parser.add_argument('--sheet', help='Worksheet name (defaults to every sheet).')
        parser.add_argument(
            '--dry-run', action='store_true', help='Parse and report without writing.'
        )

    def handle(self, *args, **options):
        path = Path(options['path'])
        if not path.exists():
            raise CommandError(f'File not found: {path}')

        totals = {'created': 0, 'updated': 0, 'unchanged': 0}
        with transaction.atomic():
            for title, rows in self._sheets(path, options['sheet']):
                try:
                    result = register_import.parse(rows)
                except register_import.RegisterImportError as exc:
                    self.stdout.write(self.style.WARNING(f'  {title}: {exc} Skipped.'))
                    continue
                self.stdout.write(
                    f'  {title}: header on row {result.header_line}, {len(result.rows)} entries, '
                    f'{result.skipped} blank or summary rows skipped.'
                )
                for row in result.warnings:
                    for warning in row.warnings:
                        self.stdout.write(self.style.WARNING(f'    row {row.line}: {warning}'))
                for key, value in register_import.apply(result).items():
                    totals[key] += value
            if options['dry_run']:
                transaction.set_rollback(True)

        verb = 'Would import' if options['dry_run'] else 'Imported'
        self.stdout.write(
            self.style.SUCCESS(
                f'{verb} {totals["created"]} new and {totals["updated"]} updated entries '
                f'({totals["unchanged"]} already up to date).'
            )
        )

    def _sheets(self, path, sheet_name):
        if path.suffix.lower() == '.csv':
            try:
                yield path.name, register_import.read_csv(path.read_bytes())
            except register_import.RegisterImportError as exc:
                raise CommandError(str(exc)) from exc
            return
        try:
            from openpyxl import load_workbook
        except ImportError as exc:  # pragma: no cover
            raise CommandError('openpyxl is required: pip install openpyxl') from exc
        workbook = load_workbook(path, data_only=True, read_only=True)
        sheets = [workbook[sheet_name]] if sheet_name else list(workbook.worksheets)
        for sheet in sheets:
            yield sheet.title, list(sheet.iter_rows(values_only=True))
