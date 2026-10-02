"""Retire the external-identity structures left behind by the provider removal.

Three changes, in one migration because they describe one removal:

* ``GoogleAuthRejection`` becomes ``AuthRejection``. A rename, not a drop, so
  every historical refusal row survives. Its ``provider`` column is dropped
  because there is no longer a provider to name; the ``action`` strings already
  recorded which one was involved.
* ``LegacyIdentity`` is added. This is what keeps accounts that were created by
  the removed provider reachable: they have no usable password, and Django
  refuses to send a reset link to exactly those accounts.
* ``django.contrib.sites`` is left installed. It is not the provider's, and
  removing it here would mean touching migrations this one does not own.
"""
from django.db import migrations, models
import django.db.models.deletion
from django.conf import settings


class Migration(migrations.Migration):

    dependencies = [
        ('google_auth', '0004_pendingsignup_verified_at'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RenameModel(
            old_name='GoogleAuthRejection',
            new_name='AuthRejection',
        ),
        migrations.AlterModelOptions(
            name='authrejection',
            options={'ordering': ['-id'], 'verbose_name': 'Authentication rejection'},
        ),
        migrations.RemoveField(
            model_name='authrejection',
            name='provider',
        ),
        migrations.CreateModel(
            name='LegacyIdentity',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('provider', models.CharField(max_length=32)),
                ('email', models.CharField(blank=True, default='', max_length=254)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='legacy_identity', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Legacy identity',
                'ordering': ['-id'],
            },
        ),
    ]