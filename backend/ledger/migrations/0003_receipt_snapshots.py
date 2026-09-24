from django.db import migrations

def populate(apps, schema_editor):
    Payment = apps.get_model('ledger', 'Payment')
    Organisation = apps.get_model('ledger', 'Organisation')
    for payment in Payment.objects.select_related('member').all().iterator():
        payment.member_name_snapshot = payment.member.full_name
        payment.save(update_fields=['member_name_snapshot'])
    Organisation.objects.get_or_create(pk=1)

class Migration(migrations.Migration):
    dependencies = [('ledger','0002_organisation_recoveryattempt_member_billing_end_and_more')]
    operations = [migrations.RunPython(populate, migrations.RunPython.noop)]
