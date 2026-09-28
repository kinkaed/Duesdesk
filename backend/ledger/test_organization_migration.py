from datetime import date
from decimal import Decimal
from uuid import uuid4
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class OrganizationMigrationTests(TransactionTestCase):
    migrate_from=[('ledger','0004_assign_missing_user_access')]
    migrate_to=[('ledger','0006_organization_backfill')]

    def setUp(self):
        super().setUp()
        executor=MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        self.old=executor.loader.project_state(self.migrate_from).apps
        self.old.get_model('ledger','Organisation').objects.all().delete()

    def tearDown(self):
        MigrationExecutor(connection).migrate(self.migrate_to)
        super().tearDown()

    def forward(self):
        executor=MigrationExecutor(connection);executor.migrate(self.migrate_to)
        return executor.loader.project_state(self.migrate_to).apps

    def test_existing_rows_roles_ids_and_balances_are_preserved(self):
        old=self.old
        org=old.get_model('ledger','Organisation').objects.create(pk=1,name='Existing Association',contact='Old contact',receipt_footer='Original thanks')
        user=old.get_model('auth','User').objects.create(username='old-secretary',is_staff=True,password='!')
        member=old.get_model('ledger','Member').objects.create(full_name='Existing Member',joined=date(2026,1,1))
        due=old.get_model('ledger','DuesMonth').objects.create(month=date(2026,9,1),amount_due=25)
        payment=old.get_model('ledger','Payment').objects.create(member_id=member.pk,amount_received=100,payment_date=date(2026,9,1),method='Cash',request_key=uuid4(),created_by_id=user.pk,member_name_snapshot='Original Name')
        allocation=old.get_model('ledger','Allocation').objects.create(payment_id=payment.pk,dues_month_id=due.pk,amount=25)
        access=old.get_model('ledger','UserAccess').objects.create(user_id=user.pk,role='secretary')
        event=old.get_model('ledger','AuditEvent').objects.create(actor_id=user.pk,action='payment.recorded',entity='Payment',entity_id=str(payment.pk))
        batch=old.get_model('ledger','ImportBatch').objects.create(digest='a'*64,kind='members',row_count=1,created_by_id=user.pk)
        new=self.forward()
        for name,obj in [('Member',member),('DuesMonth',due),('Payment',payment),('Allocation',allocation),('UserAccess',access),('AuditEvent',event),('ImportBatch',batch)]:
            row=new.get_model('ledger',name).objects.get(pk=obj.pk)
            self.assertEqual(row.organization_id,org.pk)
        migrated=new.get_model('ledger','Payment').objects.get(pk=payment.pk)
        self.assertEqual(migrated.amount_received,Decimal('100'));self.assertEqual(migrated.member_name_snapshot,'Original Name')
        self.assertEqual(new.get_model('ledger','UserAccess').objects.get(pk=access.pk).role,'secretary')
        kept=new.get_model('ledger','Organisation').objects.get(pk=1)
        self.assertEqual(kept.name,'Existing Association');self.assertEqual(kept.contact,'Old contact');self.assertTrue(kept.public_id)
        self.assertGreater(new.get_model('ledger','Organisation').objects.create(name='Next organization').pk,1)

    def test_empty_database_gets_default_and_safe_next_identity(self):
        new=self.forward();Org=new.get_model('ledger','Organisation')
        default=Org.objects.get(pk=1)
        self.assertEqual(default.name,'Membership Association')
        next_org=Org.objects.create(name='First signup')
        self.assertGreater(next_org.pk,default.pk)
        self.assertNotEqual(next_org.public_id,default.public_id)
