"""What the removal migration does to a database that already has accounts.

These tests drive the real migration executor rather than the model layer, because
the whole question is what happens to rows that were written before the provider
existed. They run against the 0004 state, put a copy of the provider's table back
by hand, and then migrate forward.

The cases that matter:

* an account created by the provider, which has no usable password, is marked and
  can therefore be given one;
* an account that already had a password is not marked, because Django's own rule
  already applies to it;
* a disabled account is not marked, so no reset link can revive it;
* rolling back removes only the marks, leaving the provider's table intact.
"""
from django.contrib.auth import get_user_model
from django.db import connection, models
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.test import TransactionTestCase

from .models import LegacyIdentity

SOURCE_TABLE = 'socialaccount_socialaccount'
BEFORE = [('google_auth', '0004_pendingsignup_verified_at')]
AFTER = [('google_auth', '0006_record_legacy_identities')]


class ProviderIdentity(models.Model):
    """A stand-in for the provider's identity table, with the columns the
    migration reads and nothing else.

    The package that owned the real table is no longer installed, so the test has
    to put it back. It is declared as a model rather than written as SQL so the
    table is created the way the database in front of us expects one: hand-written
    DDL is where a SQLite test quietly stops being a PostgreSQL test.
    """

    # The provider's own table had a foreign key here. This one does not: the
    # migration only ever reads the number, and a stand-in that carries no
    # constraint cannot pass for the wrong reason.
    user = models.IntegerField(null=True, db_column='user_id')
    provider = models.CharField(max_length=32)
    uid = models.CharField(max_length=255)
    extra_data = models.TextField(null=True)

    class Meta:
        db_table = SOURCE_TABLE
        app_label = 'google_auth'


class ProviderTableMixin:
    def migrate_to(self, target):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        return executor.migrate(target)

    def migrate_to_latest(self):
        """Restore every google_auth migration, not just the one under test."""
        loader = MigrationLoader(connection)
        return self.migrate_to(loader.graph.leaf_nodes('google_auth'))

    def drop_provider_table(self):
        # The table is created by hand here, so Django's flush has no reason to
        # know about it and it would otherwise survive into the next test. It is
        # often not there at all: the first test to run finds no trace of it.
        if SOURCE_TABLE not in connection.introspection.table_names():
            return
        with connection.schema_editor() as editor:
            editor.delete_model(ProviderIdentity)

    def put_provider_rows(self, *rows):
        """Recreate the provider's table and fill it with (user, provider, extra_data)."""
        self.drop_provider_table()
        with connection.schema_editor() as editor:
            editor.create_model(ProviderIdentity)
        with connection.cursor() as cursor:
            for user, provider, extra_data in rows:
                cursor.execute(
                    'INSERT INTO ' + SOURCE_TABLE + ' '
                    '(user_id, provider, uid, extra_data) VALUES (%s, %s, %s, %s)',
                    [user.pk, provider, 'uid-%s' % user.pk, extra_data])

    def provider_account(self, username, email, password=None, active=True):
        user = get_user_model().objects.create_user(username, email, password=password)
        if not active:
            user.is_active = False
            user.save(update_fields=['is_active'])
        return user


class FreshDatabaseTests(TransactionTestCase):
    """A database built after the removal never had the provider's table."""

    def setUp(self):
        # Belt and braces: whatever a sibling test left behind, the database this
        # class is asserting about must not have it.
        ProviderTableMixin.drop_provider_table(self)

    def test_the_migration_runs_without_the_provider_table(self):
        tables = connection.introspection.table_names()
        self.assertNotIn(SOURCE_TABLE, tables)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM django_migrations WHERE app = 'google_auth' "
                "AND name = '0006_record_legacy_identities'")
            self.assertEqual(cursor.fetchone()[0], 1,
                             'the removal migration must have run on a fresh database')

    def test_and_it_marks_nothing(self):
        self.assertFalse(LegacyIdentity.objects.exists())


class ExistingDatabaseTests(ProviderTableMixin, TransactionTestCase):
    """A database that was in production before the provider was removed."""

    reset_sequences = True

    def setUp(self):
        # Stay on the pre-removal state for the whole test, so the forward
        # migration really runs once the rows are in place. Returning to it in
        # tearDown leaves the database as the rest of the suite expects to find it.
        self.migrate_to(BEFORE)

    def tearDown(self):
        # The schema goes back first and whatever the test did. Restoring it is
        # what the rest of the suite depends on, and a half-migrated database
        # breaks every test after this one rather than this one alone.
        self.migrate_to_latest()
        # The evidence goes last, so the next test starts from a database that
        # never had the provider.
        self.drop_provider_table()

    def test_only_the_provider_accounts_are_marked(self):
        User = get_user_model()
        founder = self.provider_account('founder', 'founder@example.com')
        with_password = self.provider_account('haspass', 'haspass@example.com',
                                              password='An-Existing-Password-42!')
        disabled = self.provider_account('former', 'former@example.com', active=False)
        absent = self.provider_account('no-identity', 'no-identity@example.com')

        # And an account from another provider, which this removal does not own.
        other = self.provider_account('other-provider', 'other@example.com')
        self.put_provider_rows(
            (founder, 'google', '{"email": "founder@example.com"}'),
            (with_password, 'google', '{"email": "haspass@example.com"}'),
            (disabled, 'google', '{"email": "former@example.com"}'),
            (absent, 'other', '{"email": "no-identity@example.com"}'),
            (other, 'github', '{"email": "other@example.com"}'),
        )

        self.migrate_to(AFTER)

        marked = {row.user.username: row for row in LegacyIdentity.objects.all()}
        self.assertEqual(list(marked), ['founder'])
        self.assertEqual(marked['founder'].provider, 'google')
        self.assertEqual(marked['founder'].email, 'founder@example.com')
        self.assertEqual(User.objects.count(), 5,
                         'no account may be created or removed by the migration')

    def test_the_marked_account_can_be_given_a_password_afterwards(self):
        founder = self.provider_account('founder', 'founder@example.com')
        self.put_provider_rows((founder, 'google', '{"email": "founder@example.com"}'))
        self.migrate_to(AFTER)

        self.assertFalse(get_user_model().objects.get(pk=founder.pk).has_usable_password())
        self.assertEqual(LegacyIdentity.objects.filter(user=founder).count(), 1)

        # The rest of the recovery machinery is proved in test_legacy_identity.py;
        # this test only has to establish that the row the migration left behind is
        # the one that recovery looks for.
        founder.set_password('A-New-Password-42!')
        founder.save(update_fields=['password'])
        LegacyIdentity.objects.filter(user=founder).delete()
        self.assertTrue(get_user_model().objects.get(pk=founder.pk).has_usable_password())
        self.assertFalse(LegacyIdentity.objects.exists())

    def test_a_row_the_provider_wrote_that_cannot_be_read_is_still_marked(self):
        """extra_data is provider-shaped and not ours to trust.

        A row we cannot parse still identifies the account, so it is marked without
        an address rather than skipped: skipping it would lock that person out.
        """
        founder = self.provider_account('founder', 'founder@example.com')
        self.put_provider_rows((founder, 'google', 'not-json'))

        self.migrate_to(AFTER)

        row = LegacyIdentity.objects.get()
        self.assertEqual(row.user, founder)
        self.assertEqual(row.email, '')

    def test_running_the_migration_twice_marks_nothing_new(self):
        founder = self.provider_account('founder', 'founder@example.com')
        self.put_provider_rows((founder, 'google', '{"email": "founder@example.com"}'))
        self.migrate_to(AFTER)
        self.migrate_to(BEFORE)
        self.migrate_to(AFTER)

        self.assertEqual(LegacyIdentity.objects.count(), 1)

    def test_rolling_back_removes_the_marks_and_leaves_the_evidence(self):
        founder = self.provider_account('founder', 'founder@example.com')
        self.put_provider_rows((founder, 'google', '{"email": "founder@example.com"}'))
        self.migrate_to(AFTER)
        self.assertEqual(LegacyIdentity.objects.count(), 1)

        self.migrate_to(BEFORE)

        # Rolling back 0005 removes the table the marks lived in.
        self.assertNotIn('google_auth_legacyidentity',
                         connection.introspection.table_names())
        # The provider's table was never ours to drop, forwards or backwards.
        with connection.cursor() as cursor:
            cursor.execute('SELECT COUNT(*) FROM ' + SOURCE_TABLE)
            self.assertEqual(cursor.fetchone()[0], 1)
        # And coming forward again produces the same mark, from the same evidence.
        self.migrate_to(AFTER)
        self.assertEqual(LegacyIdentity.objects.get().user, founder)