"""Migration 0008: request context on audit events, and the action rename.

Run against a real database through the migration executor rather than against
the current model, because the whole point of these tests is what happens to rows
that already exist when the migration runs. A model-level test cannot see that.
"""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

# The names the application used before they were normalised. If one of these is
# ever written again, the taxonomy has drifted and this test should fail rather
# than quietly leave two spellings in the history.
LEGACY_ACTIONS = ['organisation.updated', 'report.exported', 'member.report_exported']


class AuditEventMigrationTests(TransactionTestCase):
    migrate_from = [('ledger', '0007_secretary_only_access')]
    migrate_to = [('ledger', '0008_auditevent_http_method_auditevent_ip_address_and_more')]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        self.old = executor.loader.project_state(self.migrate_from).apps
        self.old.get_model('ledger', 'AuditEvent').objects.all().delete()

    def tearDown(self):
        MigrationExecutor(connection).migrate(self.migrate_to)
        super().tearDown()

    def forward(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        return executor.loader.project_state(self.migrate_to).apps

    def seed(self, action, suffix='1'):
        old = self.old
        org = old.get_model('ledger', 'Organisation').objects.create(name=f'Existing Association {suffix}')
        user = old.get_model('auth', 'User').objects.create(username=f'old-secretary-{suffix}', is_staff=True, password='!')
        old.get_model('ledger', 'UserAccess').objects.create(user_id=user.pk, role='secretary', organization_id=org.pk)
        return org, user, old.get_model('ledger', 'AuditEvent').objects.create(
            organization_id=org.pk, actor_id=user.pk, action=action,
            entity='Payment', entity_id='7', details='{"amount": "25.00"}')

    def test_legacy_action_names_are_normalised_without_losing_anything_else(self):
        seeded = {action: self.seed(action, str(index)) for index, action in enumerate(LEGACY_ACTIONS)}
        new = self.forward()
        rows = new.get_model('ledger', 'AuditEvent').objects.all()
        self.assertEqual(rows.count(), 3)
        self.assertEqual({row.action for row in rows},
                         {'organization.updated', 'export.generated', 'member.export.generated'})
        # A pure rename: the row keeps its identity, actor, organization, entity
        # and timestamp. An audit history that loses rows on a schema change is
        # worse than one with an ugly name in it.
        for action, (org, user, row) in seeded.items():
            migrated = rows.get(pk=row.pk)
            self.assertEqual(migrated.organization_id, org.pk)
            self.assertEqual(migrated.actor_id, user.pk)
            self.assertEqual(migrated.entity, 'Payment')
            self.assertEqual(migrated.entity_id, '7')
            self.assertEqual(migrated.created_at, row.created_at)
            self.assertEqual(migrated.details, '{"amount": "25.00"}')

    def test_existing_rows_gain_the_new_fields_with_safe_defaults(self):
        org, user, row = self.seed('payment.recorded')
        new = self.forward()
        migrated = new.get_model('ledger', 'AuditEvent').objects.get(pk=row.pk)
        # A pre-existing row did not have a request id, an IP or a method. The
        # migration must not invent any of them.
        self.assertEqual(migrated.outcome, 'success')
        self.assertEqual(migrated.request_id, '')
        self.assertIsNone(migrated.ip_address)
        self.assertEqual(migrated.user_agent, '')
        self.assertEqual(migrated.http_method, '')
        self.assertEqual(migrated.path, '')
        self.assertEqual(migrated.reason, '')

    def test_organization_becomes_optional_so_pre_auth_events_can_exist(self):
        old = self.old
        # organization was NOT NULL before 0008, so this row is only writable
        # through the new column default: the reason for making it nullable.
        self.old.get_model('ledger', 'AuditEvent').objects.all().delete()
        new = self.forward()
        Event = new.get_model('ledger', 'AuditEvent')
        row = Event.objects.create(organization=None, actor=None, action='auth.login.failure',
                                   entity='', entity_id='', details='{}')
        self.assertIsNone(Event.objects.get(pk=row.pk).organization)

    def test_the_rename_is_idempotent_for_rows_written_afterwards(self):
        self.seed('export.generated')
        new = self.forward()
        Event = new.get_model('ledger', 'AuditEvent')
        # A row already using the new name must not be touched, or the data
        # migration would rewrite a live name back to a legacy one.
        Event.objects.create(organization=None, actor=None, action='export.generated',
                             entity='', entity_id='', details='{}')
        migrated = Event.objects.filter(action='export.generated').count()
        self.assertEqual(migrated, 2)
        self.assertFalse(Event.objects.filter(action='report.exported').exists())
