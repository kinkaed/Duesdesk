from django.core.management.base import BaseCommand, CommandError
from django.db.models import Sum
from ledger.models import Payment, Allocation, DuesMonth

class Command(BaseCommand):
    help='Read-only reconciliation of payment totals, allocations and monthly balances.'
    def handle(self,*args,**kwargs):
        errors=[]
        for p in Payment.objects.annotate(allocated=Sum('allocations__amount')):
            if p.allocated != p.amount_received: errors.append(f'{p.receipt_number}: allocation total differs from money received')
        months={d.pk:d for d in DuesMonth.objects.all()}
        for row in Allocation.objects.filter(payment__voided_at__isnull=True).values('payment__member_id','dues_month_id').annotate(total=Sum('amount')):
            if row['total']>months[row['dues_month_id']].amount_due:errors.append(f'Member {row["payment__member_id"]}: overallocated month {row["dues_month_id"]}')
        for m in months.values():
            if m.month.day!=1:errors.append(f'DuesMonth {m.pk}: month must start on day 1')
        if errors:raise CommandError('\n'.join(errors))
        self.stdout.write(self.style.SUCCESS('All payment totals and monthly allocations reconcile.'))
