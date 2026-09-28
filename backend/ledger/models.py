from django.conf import settings
from django.db import models
from django.db.models import Q
from django.core.validators import MinValueValidator
from decimal import Decimal
import uuid
from django.utils import timezone
from django.core.validators import RegexValidator
from django.core.exceptions import ValidationError

class ScopedModel(models.Model):
    tenant_relations = ()

    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        if self.pk:
            existing=type(self).objects.filter(pk=self.pk).values_list('organization_id',flat=True).first()
            if existing and existing!=self.organization_id:
                raise ValidationError('Records cannot be moved between organizations.')
        for field in self.tenant_relations:
            if getattr(self,field+'_id',None):
                related=getattr(self,field)
                if related.organization_id!=self.organization_id:
                    raise ValidationError('Related records must belong to the same organization.')

    def save(self,*args,**kwargs):
        self.clean()
        return super().save(*args,**kwargs)

class Member(ScopedModel):
    tenant_relations = ()
    organization = models.ForeignKey("Organisation", on_delete=models.PROTECT)
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

class DuesMonth(ScopedModel):
    tenant_relations = ()
    organization = models.ForeignKey("Organisation", on_delete=models.PROTECT)
    month = models.DateField()
    amount_due = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('25.00'))

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization','month'], name='org_dues_month'), models.CheckConstraint(condition=Q(amount_due__gt=0), name='dues_positive')]

class Payment(ScopedModel):
    tenant_relations = ('member',)
    organization = models.ForeignKey("Organisation", on_delete=models.PROTECT)
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

class Allocation(ScopedModel):
    tenant_relations = ('payment', 'dues_month')
    organization = models.ForeignKey("Organisation", on_delete=models.PROTECT)
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name='allocations')
    dues_month = models.ForeignKey(DuesMonth, on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['payment', 'dues_month'], name='unique_payment_month'), models.CheckConstraint(condition=Q(amount__gt=0), name='allocation_positive')]

class UserAccess(ScopedModel):
    tenant_relations = ('member',)
    organization = models.ForeignKey("Organisation", on_delete=models.PROTECT)
    active = models.BooleanField(default=True)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='access')
    role = models.CharField(max_length=12, choices=[('secretary','Secretary'), ('auditor','Auditor'), ('member','Member')])
    member = models.OneToOneField(Member, on_delete=models.PROTECT, null=True, blank=True)

class Organisation(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    created_at = models.DateTimeField(default=timezone.now)
    # Small normalized PNGs live in PostgreSQL, surviving Render restarts.
    logo = models.BinaryField(blank=True, default=bytes)
    primary = models.CharField(max_length=7, default='#214f43', validators=[RegexValidator(r'^#[0-9a-fA-F]{6}$')])
    secondary = models.CharField(max_length=7, default='#edf4e6', validators=[RegexValidator(r'^#[0-9a-fA-F]{6}$')])
    accent = models.CharField(max_length=7, default='#527735', validators=[RegexValidator(r'^#[0-9a-fA-F]{6}$')])

    name = models.CharField(max_length=120, default='Membership Association')
    contact = models.CharField(max_length=200, blank=True)
    receipt_footer = models.CharField(max_length=250, default='Thank you for your contribution.')

class AuditEvent(ScopedModel):
    tenant_relations = ()
    organization = models.ForeignKey("Organisation", on_delete=models.PROTECT)
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

class ImportBatch(ScopedModel):
    tenant_relations = ()
    organization = models.ForeignKey("Organisation", on_delete=models.PROTECT)
    digest = models.CharField(max_length=64)
    kind = models.CharField(max_length=12)
    row_count = models.PositiveIntegerField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization','digest'], name='org_import_digest')]


class SecretaryInvite(ScopedModel):
    tenant_relations = ()
    organization = models.ForeignKey(Organisation, on_delete=models.PROTECT)
    token_hash = models.CharField(max_length=64, unique=True)
    email = models.EmailField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='sent_invites')
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    used_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name='accepted_invites')
    revoked_at = models.DateTimeField(null=True, blank=True)
