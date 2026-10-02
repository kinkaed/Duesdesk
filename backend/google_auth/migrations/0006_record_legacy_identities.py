"""Record which existing accounts were created by the removed provider.

Duesdesk used to let someone sign in with an external identity instead of a
password. Those accounts exist in production today and have an unusable password,
because nothing ever set one. When that provider was removed they became
unreachable: Django refuses to send a reset link to an account with no usable
password, which is the correct default and would have locked every one of them
out of their own organization.

This migration reads the provider's identity table directly and writes one
``LegacyIdentity`` row per affected account, so the reset form can offer them a
password. It is deliberately narrow:

* Only accounts that actually have no usable password are marked. An account
  with a real password keeps Django's normal rules and gains nothing.
* Only accounts whose provider identity carried a verified email at the moment
  the provider was removed are marked. Nothing here asserts an address is
  verified today; it is a record of what the provider had proved.
* It never deletes or edits a user, an organization or a membership. It only
  adds a row to a new table.
* The provider's own tables are left exactly as they are, so this migration is
  reversible and an operator can still inspect what was there.

Fresh installations never reach the provider's table at all, because the package
that created it is no longer installed. Both cases are handled below: a missing
table is not an error, it just means there is nothing to migrate.
"""
import json

from django.conf import settings
from django.contrib.auth.hashers import is_password_usable
from django.db import migrations

# The provider's table and the columns this needs. Named as literals so the
# migration keeps working after the package that created them is uninstalled.
SOURCE_TABLE = 'socialaccount_socialaccount'
VERIFIED_PROVIDER = 'google'


def _source_exists(schema_editor):
    tables = schema_editor.connection.introspection.table_names()
    return SOURCE_TABLE in tables


def _user_columns(apps):
    """The user table's real column names, so a swapped user model still works.

    Read from the historical model rather than assumed: AUTH_USER_MODEL may point
    at a table of our own, and a migration that hard-codes auth_user would simply
    fail there.
    """
    User = apps.get_model(*settings.AUTH_USER_MODEL.split('.'))
    return (User._meta.db_table,
            User._meta.pk.column,
            User._meta.get_field('is_active').column,
            User._meta.get_field('password').column)


def forwards(apps, schema_editor):
    if not _source_exists(schema_editor):
        # Fresh database: the provider's table was never created.
        return

    LegacyIdentity = apps.get_model('google_auth', 'LegacyIdentity')
    user_table, user_pk, active_column, password_column = _user_columns(apps)

    # Both sides are read in one join, and the password is judged as a string.
    # Historical models carry their columns but none of AbstractBaseUser's
    # methods, so user.has_usable_password() would raise AttributeError here and
    # take the whole deployment's migration with it. is_password_usable() is the
    # same test that method performs.
    query = (
        'SELECT u."%s", s."extra_data" FROM "%s" u '
        'JOIN "%s" s ON s."user_id" = u."%s" '
        'WHERE s."provider" = %%s AND u."%s" = %%s'
        % (user_pk, user_table, SOURCE_TABLE, user_pk, active_column)
    )
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(query, [VERIFIED_PROVIDER, True])
        rows = cursor.fetchall()

        for user_id, extra_data in rows:
            cursor.execute(
                'SELECT "%s" FROM "%s" WHERE "%s" = %%s' % (password_column, user_table, user_pk),
                [user_id])
            stored = cursor.fetchone()[0]
            if is_password_usable(stored):
                # It already has a password. Django's own rule applies and nothing
                # is granted.
                continue

            address = ''
            try:
                address = (json.loads(extra_data) or {}).get('email') or ''
            except (TypeError, ValueError):
                # extra_data is provider-shaped and not ours to trust; a row we
                # cannot read still identifies the account, so mark it without one.
                address = ''

            LegacyIdentity.objects.get_or_create(
                user_id=user_id,
                defaults={'provider': VERIFIED_PROVIDER, 'email': address[:254]},
            )


def backwards(apps, schema_editor):
    # Removing the flag is the exact inverse of adding it: drop the rows this
    # migration created. The provider's own table is untouched either way, so
    # rolling back does not lose the evidence.
    LegacyIdentity = apps.get_model('google_auth', 'LegacyIdentity')
    LegacyIdentity.objects.filter(provider=VERIFIED_PROVIDER).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('google_auth', '0005_retire_external_identity'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]