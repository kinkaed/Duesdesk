"""Fail the build if a template references an uncollected static asset."""
import json
import re
from pathlib import Path
from django.conf import settings
from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Verify template static references against the collected manifest and hashed files.'

    def handle(self, *args, **options):
        root = Path(settings.STATIC_ROOT)
        manifest = root / 'staticfiles.json'
        self.stdout.write(f'STATICFILES_DIRS: {settings.STATICFILES_DIRS}')
        self.stdout.write(f'STATIC_ROOT: {root}')
        self.stdout.write(f'Storage: {settings.STORAGES["staticfiles"]["BACKEND"]}')
        self.stdout.write(f'Manifest: {manifest}')
        if not root.is_absolute():
            raise CommandError('STATIC_ROOT must be absolute.')
        try:
            paths = json.loads(manifest.read_text(encoding='utf-8'))['paths']
        except (OSError, ValueError, KeyError) as error:
            raise CommandError(f'Missing or invalid manifest: {manifest}') from error
        required = {'app.css', 'app/app.js', 'app/style.css', 'favicon.svg'}
        for template in (settings.BASE_DIR / 'templates').rglob('*.html'):
            required.update(re.findall(r"{%\s*static\s+['\"]([^'\"]+)['\"]", template.read_text(encoding='utf-8')))
        for name in sorted(required):
            mapped = paths.get(name)
            if not mapped or not (root / mapped).is_file():
                raise CommandError(f'Missing collected asset or manifest entry: {name}')
            # Force manifest URL resolution even if DJANGO_DEBUG was enabled.
            url = staticfiles_storage.url(name, force=True)
            self.stdout.write(f'OK {name} -> {mapped} ({url})')
        self.stdout.write(self.style.SUCCESS(f'Verified {len(required)} template/static assets.'))
