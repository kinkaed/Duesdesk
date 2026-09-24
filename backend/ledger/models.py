from django.conf import settings
from django.db import models
from django.db.models import Q
from django.core.validators import MinValueValidator
from decimal import Decimal

class Member(models.Model):
    full_name = models.CharField(max_length=100)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    joined = models.DateField()
    status = models.CharField(max_length=12, choices=[('Active', 'Active'), ('Inactive', 'Inactive'), ('Suspended', 'Suspended')], default='Active')
    created_at = models.DateTimeField(auto_now_add=True)
    billing_end = models.DateField(null=True, blank=True, help_text='Last month for which dues are charged.')

    @property
    def code(self):
        return f'MBR-{self.pk:04d}'

class DuesMonth(models.Model):
    month = models.DateField(unique=True)
    amount_due = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('25.00'))

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(amount_due__gt=0), name='dues_positive')]

class Payment(models.Model):
    member = models.ForeignKey(Member, on_delete=models.PROTECT, related_name='payments')
    amount_received = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    payment_date = models.DateField()
    method = models.CharField(max_length=20, choices=[(s, s) for s in ['Cash', 'Mobile Money', 'Bank Transfer', 'Check']])
    reference = models.CharField(max_length=80, blank=True)
    notes = models.CharField(max_length=500, blank=True)
    request_key = models.UUIDField(unique=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='voided_payments')
    void_reason = models.CharField(max_length=500, blank=True)
    member_name_snapshot = models.CharField(max_length=100, blank=True)
    request_fingerprint = models.CharField(max_length=64, blank=True)

    @property
    def receipt_number(self):
        return f'RCT-{self.pk:06d}'

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(amount_received__gt=0), name='payment_positive')]

class Allocation(models.Model):
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name='allocations')
    dues_month = models.ForeignKey(DuesMonth, on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['payment', 'dues_month'], name='unique_payment_month'), models.CheckConstraint(condition=Q(amount__gt=0), name='allocation_positive')]

class UserAccess(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='access')
    role = models.CharField(max_length=12, choices=[('secretary','Secretary'), ('auditor','Auditor'), ('member','Member')])
    member = models.OneToOneField(Member, on_delete=models.PROTECT, null=True, blank=True)

class Organisation(models.Model):
    name = models.CharField(max_length=120, default='Membership Association')
    contact = models.CharField(max_length=200, blank=True)
    receipt_footer = models.CharField(max_length=250, default='Thank you for your contribution.')

class AuditEvent(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    action = models.CharField(max_length=60)
    entity = models.CharField(max_length=30)
    entity_id = models.CharField(max_length=40)
    details = models.TextField(default='{}')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-id']

class RecoveryAttempt(models.Model):
    key = models.CharField(max_length=64, unique=True)
    requested_at = models.DateTimeField()

class ImportBatch(models.Model):
    digest = models.CharField(max_length=64, unique=True)
    kind = models.CharField(max_length=12)
    row_count = models.PositiveIntegerField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
