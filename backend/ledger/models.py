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
    # Members are records, not accounts. No member role exists, so a member can
    # never be granted a login. Secretaries arrive by invitation; auditors are
    # read-only accounts a secretary creates.
    role = models.CharField(max_length=12, choices=[('secretary','Secretary'), ('auditor','Auditor')])
    # Reserved for a future read-only member portal. It links an account to a
    # member record but grants no access by itself: access comes from role, and
    # no role lets a member sign in.
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

    # Onboarding is the guided first-run (organization name, effectively chosen at
    # signup, and the skippable branding step). It is deliberately separate from
    # "when the organization was created": backfilling this for organizations that
    # predate the flow would claim they completed something they never saw.
    # onboarding_required is set only by the immediate-creation signup, so a legacy
    # organization is False/NULL and is never treated as an abandoned signup.
    onboarding_required = models.BooleanField(default=False)
    onboarded_at = models.DateTimeField(null=True, blank=True)

    # A soft lifecycle stop. Never a hard delete: AuditEvent.organization and
    # UserAccess.organization are PROTECT, so an organization with history cannot
    # be removed. A disabled organization resolves to no membership, so every
    # tenant query and endpoint refuses it, while the audit rows remain.
    disabled_at = models.DateTimeField(null=True, blank=True)

class AuditEventQuerySet(models.QuerySet):
    """Audit rows are evidence, so they can be appended but never rewritten.

    Ordinary application flows must not be able to quietly adjust a timestamp,
    an actor or an outcome after the fact. Bulk update and delete are refused
    here as well as on the instance, because a queryset is the easy way to
    bypass a model-level guard.
    """

    def update(self, **kwargs):
        raise ValidationError('Audit events cannot be modified.')

    def delete(self):
        raise ValidationError('Audit events cannot be deleted.')


class AuditEvent(ScopedModel):
    tenant_relations = ()
    # Nullable only so that events which happen before authentication (a failed
    # login, a lockout) can be recorded without inventing a user or an
    # organization. Tenant queries filter on organization, so a null
    # organization is simply never visible to any tenant. The audit() helper
    # only allows it for the allowlist in observability.ANONYMOUS_ACTIONS.
    organization = models.ForeignKey("Organisation", null=True, blank=True, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    action = models.CharField(max_length=60)
    # success | failure | denied | rejected | replayed. Defaults to success so
    # every pre-existing call site keeps its meaning without being rewritten.
    outcome = models.CharField(max_length=10, choices=[(o, o) for o in ('success', 'failure', 'denied', 'rejected', 'replayed')], default='success')
    # Why a non-success outcome happened, e.g. duplicate_idempotency_key.
    reason = models.CharField(max_length=60, blank=True)
    entity = models.CharField(max_length=30, blank=True)
    entity_id = models.CharField(max_length=40, blank=True)
    # Correlates the event with the technical log lines and any 500 for the
    # same request. Null for events raised outside a request.
    request_id = models.CharField(max_length=64, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    http_method = models.CharField(max_length=10, blank=True)
    path = models.CharField(max_length=200, blank=True)
    details = models.TextField(default='{}')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = AuditEventQuerySet.as_manager()

    class Meta:
        ordering = ['-id']
        indexes = [
            # Tenant timeline: everything for an organization, newest first.
            models.Index(fields=['organization', '-created_at'], name='audit_org_created'),
            # "Show me every payment event" over a period.
            models.Index(fields=['action', '-created_at'], name='audit_action_created'),
            # "What happened during request ABC123?"
            models.Index(fields=['request_id'], name='audit_request_id'),
            # "Everything that happened to member 42 last month."
            models.Index(fields=['entity', 'entity_id', '-created_at'], name='audit_resource_created'),
            # "Everything this user did."
            models.Index(fields=['actor', '-created_at'], name='audit_actor_created'),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Audit events cannot be modified.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Audit events cannot be deleted.')

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
