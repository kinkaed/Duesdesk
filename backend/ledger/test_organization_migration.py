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

    def test_missing_accounts_get_least_privilege_roles(self):
        # Reproduce the real ordering: 0004 only ever over-privileged accounts
        # that already existed when it ran, so start one migration earlier and
        # create them there.
        executor=MigrationExecutor(connection)
        executor.migrate([('ledger','0003_receipt_snapshots')])
        old=executor.loader.project_state([('ledger','0003_receipt_snapshots')]).apps
        old.get_model('auth','User').objects.create(username='old-secretary',is_staff=True,password='!')
        old.get_model('auth','User').objects.create(username='root',is_superuser=True,password='!')
        old.get_model('auth','User').objects.create(username='ordinary',is_staff=False,password='!')
        new=self.forward()
        # 0004 must not pre-empt 0006: an ordinary account is read-only.
        roles=new.get_model('ledger','UserAccess').objects.values_list('user__username','role')
        self.assertEqual(sorted(roles),[('old-secretary','secretary'),('ordinary','auditor'),('root','secretary')])

    def test_empty_database_gets_default_and_safe_next_identity(self):
        new=self.forward();Org=new.get_model('ledger','Organisation')
        default=Org.objects.get(pk=1)
        self.assertEqual(default.name,'Membership Association')
        next_org=Org.objects.create(name='First signup')
        self.assertGreater(next_org.pk,default.pk)
        self.assertNotEqual(next_org.public_id,default.public_id)

    def test_legacy_organization_with_non_default_pk_keeps_its_records(self):
        org=self.old.get_model('ledger','Organisation').objects.create(pk=7,name='Real Association',contact='Real contact',receipt_footer='Real thanks')
        user=self.old.get_model('auth','User').objects.create(username='legacy',is_staff=True,password='!')
        member=self.old.get_model('ledger','Member').objects.create(full_name='Legacy Member',joined=date(2026,1,1))
        due=self.old.get_model('ledger','DuesMonth').objects.create(month=date(2026,9,1),amount_due=25)
        payment=self.old.get_model('ledger','Payment').objects.create(member_id=member.pk,amount_received=100,payment_date=date(2026,9,1),method='Cash',request_key=uuid4(),created_by_id=user.pk,member_name_snapshot='Legacy Member')
        allocation=self.old.get_model('ledger','Allocation').objects.create(payment_id=payment.pk,dues_month_id=due.pk,amount=25)
        new=self.forward()
        # Records must stay with the organization they already belonged to.
        for model,obj in [(new.get_model('ledger','Member'),member),(new.get_model('ledger','Payment'),payment),(new.get_model('ledger','Allocation'),allocation)]:
            self.assertEqual(model.objects.get(pk=obj.pk).organization_id,org.pk)
        # No second, generic organization may appear and no branding may be lost.
        self.assertEqual(new.get_model('ledger','Organisation').objects.count(),1)
        kept=new.get_model('ledger','Organisation').objects.get(pk=7)
        self.assertEqual(kept.name,'Real Association')
        self.assertEqual(kept.contact,'Real contact')
        self.assertEqual(kept.receipt_footer,'Real thanks')
        self.assertIsNotNone(kept.public_id)
        self.assertGreater(new.get_model('ledger','Organisation').objects.create(name='Next organization').pk,7)

    def test_multiple_existing_organizations_are_all_preserved(self):
        old_org=self.old.get_model('ledger','Organisation').objects.create(pk=3,name='Lowest Association')
        self.old.get_model('ledger','Organisation').objects.create(pk=9,name='Other Association')
        self.old.get_model('ledger','Member').objects.create(full_name='Legacy Member',joined=date(2026,1,1))
        new=self.forward()
        # Historical records cannot be split between organizations, so they are
        # adopted by the lowest existing pk; no organization may be dropped.
        self.assertEqual(new.get_model('ledger','Member').objects.get(full_name='Legacy Member').organization_id,old_org.pk)
        kept=new.get_model('ledger','Organisation').objects.order_by('pk')
        self.assertEqual([(o.pk,o.name) for o in kept],[(3,'Lowest Association'),(9,'Other Association')])
        self.assertTrue(all(o.public_id for o in kept))
