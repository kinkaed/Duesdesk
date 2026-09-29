"""The vocabulary of the audit history, in one place.

An audit row's `action` is a stable identifier written once, at the moment the
event happens, and it must never be rewritten to make a screen look nicer. What
*is* presentation is the label a reader sees, the group it sorts under, and how
prominent it should be. All three are derived here, from the identifier, so they
cannot drift from the events that actually exist and no React component has to
know the string "payment.recorded" exists.

Three rules shape this module:

* Nothing here is stored on the row. A new column would have to be backfilled for
  every historical event, and any backfill is a guess about the past. Deriving is
  honest and self-correcting.
* Every event type the application can write has an entry, and a source-level test
  asserts that. An entry that is missing is a gap, not a runtime error.
* An unknown action still renders. Historical rows survive deploys that add or
  rename event types, and a blank timeline would be worse than a plain sentence,
  so `describe()` falls back to the identifier itself.
"""
from dataclasses import dataclass


# Grouping shown in the filter bar. `other` exists so that an unrecognised
# historical action still lands in a real, selectable category instead of a
# filter that silently matches nothing.
CATEGORIES = (
    ('members', 'Members'),
    ('payments', 'Payments'),
    ('accounts', 'Accounts'),
    ('organization', 'Organization'),
    ('data', 'Imports and exports'),
    ('security', 'Security'),
    ('other', 'Other'),
)

# Ordered from least to most demanding of attention. A rail and a text label are
# drawn from this, never colour alone.
SEVERITIES = ('routine', 'notice', 'warning', 'critical')

# A non-success outcome promotes the event. How far it is promoted is fixed here
# rather than spread across call sites, so "denied" always reads the same way no
# matter which endpoint produced it.
PROMOTION = {
    'success': 0,
    'replayed': 1,
    'rejected': 1,
    'denied': 2,
    'failure': 2,
}

SUBJECT = '{subject}'


@dataclass(frozen=True)
class Action:
    label: str
    category: str
    severity: str
    # The resource type this event normally concerns. Used only to pick a label
    # resolver; the row's own `entity` still wins, so an event recorded with an
    # explicit resource is described correctly even if the default is stale.
    entity: str = ''
    # What to say when the affected record cannot be named. Always written out
    # rather than assembled by deleting the placeholder, because "…'s details
    # were updated" with the name missing reads as broken English and a stripped
    # sentence is easy to get subtly wrong.
    bare: str = ''

    @property
    def wants_subject(self) -> bool:
        return SUBJECT in self.label


ACTIONS = {
    # --- Members -------------------------------------------------------------
    'member.created': Action('{subject} was added', 'members', 'routine', 'Member',
                            'A member was added'),
    'member.updated': Action('Details updated for {subject}', 'members', 'routine', 'Member',
                             'Member details were updated'),
    'member.imported': Action('{subject} was added by an import', 'members', 'routine', 'Member',
                              'A member was added by an import'),
    'member.export.generated': Action('A payment history report was generated for {subject}',
                                      'members', 'notice', 'Member',
                                      'A payment history report was generated'),
    # --- Payments ------------------------------------------------------------
    'payment.recorded': Action('{subject} was recorded', 'payments', 'routine', 'Payment',
                               'A payment was recorded'),
    'payment.voided': Action('{subject} was voided', 'payments', 'notice', 'Payment',
                             'A payment was voided'),
    'payment.replayed': Action('{subject} had already been recorded', 'payments', 'notice', 'Payment',
                               'A payment had already been recorded'),
    # --- Accounts ------------------------------------------------------------
    'account.created': Action('Account {subject} was created', 'accounts', 'notice', 'User',
                              'An account was created'),
    'account.disabled': Action('Access for {subject} was disabled', 'accounts', 'warning', 'User',
                               'Access was disabled for an account'),
    'account.enabled': Action('Access for {subject} was restored', 'accounts', 'routine', 'User',
                              'Access was restored for an account'),
    'invite.created': Action('An invitation was created for {subject}', 'accounts', 'notice',
                             'SecretaryInvite', 'An invitation was created'),
    'invite.revoked': Action('The invitation for {subject} was revoked', 'accounts', 'warning',
                             'SecretaryInvite', 'An invitation was revoked'),
    # --- Organization --------------------------------------------------------
    'organization.created': Action('The organization was created', 'organization', 'notice', 'Organisation'),
    'organization.joined': Action('{subject} joined the organization', 'organization', 'notice', 'User',
                                  'An account joined the organization'),
    'organization.updated': Action('Organization settings were updated', 'organization', 'routine', 'Organisation'),
    'organization.left': Action('{subject} left the organization', 'organization', 'warning', 'User',
                                'An account left the organization'),
    # --- Imports and exports -------------------------------------------------
    'import.completed': Action('An import added {subject}', 'data', 'routine', 'ImportBatch',
                               'An import finished'),
    'import.rejected': Action('An import was rejected', 'data', 'warning'),
    'import.failed': Action('An import failed', 'data', 'warning'),
    'export.generated': Action('A report was exported', 'data', 'notice', 'User'),
    # --- Security ------------------------------------------------------------
    # Declared in observability.ANONYMOUS_ACTIONS so that a pre-auth event can have
    # no organization, which is also why none of these can name a subject.
    'auth.login.success': Action('Signed in', 'security', 'routine', 'User'),
    'auth.login.failure': Action('A sign-in attempt failed', 'security', 'warning'),
    'auth.logout': Action('Signed out', 'security', 'routine'),
    'auth.password.changed': Action('Changed their password', 'security', 'notice', 'User'),
    'auth.password.change_failed': Action('A password change was rejected', 'security', 'warning'),
    'auth.password_reset.completed': Action('A password was reset', 'security', 'warning'),
    'auth.recovery.requested': Action('A password reset was requested', 'security', 'warning'),
    'security.account_locked': Action('An account was locked after repeated failed sign-ins', 'security', 'critical'),
    'security.csrf.failure': Action('A request was blocked for a missing or invalid security token', 'security', 'critical'),
    'security.suspicious_request': Action('A suspicious request was blocked', 'security', 'critical'),
    'access.denied': Action('Access was denied', 'security', 'critical'),
    'system.startup': Action('The service started', 'security', 'routine'),
}

# Every category that an action can claim, for validating the `category` filter.
CATEGORY_IDS = frozenset(key for key, _ in CATEGORIES)
CATEGORY_LABELS = dict(CATEGORIES)

# Reverse index, so "show me every security event" is one filter on a known set
# of names rather than a pattern match on the action string. Built from ACTIONS,
# so it cannot fall out of step with it. An action in a category with no entries
# yields an empty list, which matches nothing rather than everything.
CATEGORY_ACTIONS = {
    key: tuple(name for name, entry in sorted(ACTIONS.items()) if entry.category == key)
    for key in CATEGORY_IDS
}


def category_actions(category: str) -> tuple:
    """The action names belonging to a category. Empty for an unknown category."""
    return CATEGORY_ACTIONS.get(category, ())

# Outcomes the UI offers as a filter, in the order a reader is likely to use them.
OUTCOME_LABELS = {
    'success': 'Successful',
    'failure': 'Failed',
    'denied': 'Denied',
    'rejected': 'Rejected',
    'replayed': 'Already recorded',
}

# Why a non-success outcome happened, in the words an administrator would use.
#
# `import.failed` records the class name of whatever exception was raised, which
# is deliberately absent here: an exception class name is a developer-facing
# detail, and it would also vary with the Python version. A row carrying one gets
# the generic label below and keeps the raw value in its technical section, where
# somebody investigating actually wants it.
REASON_LABELS = {
    'invalid_credentials': 'The username and password did not match.',
    'account_locked': 'The account was locked after too many failed attempts.',
    'validation_failed': 'The new password did not meet the rules.',
    'invalid_or_expired_link': 'The reset link was invalid or had expired.',
    'request_key_conflict': 'This save was already used with different details.',
    'no_active_membership': 'The account no longer has access to the organization.',
    'read_only_role': 'The account is read-only and cannot make changes.',
    'preview_expired': 'The file review had expired. Upload the file again.',
    'duplicate_file': 'This file has already been imported.',
}

# Used when the stored reason is one this release does not recognize. Never the
# raw value: see the note above.
UNKNOWN_REASON_LABEL = 'It did not complete.'


def reason_label(reason: str, outcome: str = 'success') -> str:
    """The plain-language reason, or an empty string when there is nothing to say."""
    if not reason:
        return ''
    return REASON_LABELS.get(reason, UNKNOWN_REASON_LABEL)


def fallback_label(action: str) -> str:
    """Turn an unrecognised identifier into a sentence.

    Kept deliberately dull. Guessing at the meaning of a historical event would
    be worse than showing its name plainly, so only the punctuation the
    application itself uses is expanded.
    """
    words = action.replace('.', ' ').replace('_', ' ').split()
    if not words:
        return 'Event'
    # Only the first word is capitalized. "Legacy thing happened" reads as a
    # sentence; capitalizing every word reads as a heading and fights with the
    # labels that are deliberately written as sentences.
    return ' '.join([words[0].capitalize()] + [word.lower() for word in words[1:]])


def describe(action: str, outcome: str = 'success', subject: str = '') -> dict:
    """Describe one event for the reader.

    Returns the human sentence, the category, and the severity. `subject` is the
    already-resolved name of the affected record; when it is missing the label
    is returned without the slot rather than with a gap where a name should be.
    """
    entry = ACTIONS.get(action)
    if entry is None:
        category = action.split('.', 1)[0]
        if category not in CATEGORY_IDS:
            category = 'other'
        return {
            'label': fallback_label(action),
            'category': category,
            'severity': SEVERITIES[min(2 + PROMOTION.get(outcome, 0), len(SEVERITIES) - 1)],
        }

    level = SEVERITIES.index(entry.severity) if entry.severity in SEVERITIES else 0
    severity = SEVERITIES[min(level + PROMOTION.get(outcome, 0), len(SEVERITIES) - 1)]
    label = entry.label
    if entry.wants_subject:
        if subject:
            label = label.replace(SUBJECT, subject)
        elif entry.bare:
            label = entry.bare
        else:
            # No bare form was written. Fall back to the sentence without the
            # slot rather than leaking a placeholder into the interface.
            label = label.replace(SUBJECT, 'a record').strip()
    return {'label': label, 'category': entry.category, 'severity': severity}


def changes(details) -> list:
    """Normalise the before/after diff a member edit records.

    The shape lives in services/views, not in a model, so it is read defensively:
    a historical row written before the diff was added has no `changed` key, and
    one written by a different version may hold a different shape. Neither may
    raise while somebody is reading the history.
    """
    if not isinstance(details, dict):
        return []
    changed = details.get('changed')
    if not isinstance(changed, dict) or not changed:
        return []
    rows = []
    for field in sorted(changed):
        before_after = changed[field]
        if isinstance(before_after, dict) and ('from' in before_after or 'to' in before_after):
            before, after = before_after.get('from'), before_after.get('to')
        else:
            before, after = None, before_after
        rows.append({'field': str(field), 'from': before, 'to': after})
    return rows


def public_taxonomy() -> dict:
    """The label, category and outcome vocabulary, for the filter controls.

    Served by the API rather than hardcoded in React so that the words on screen
    and the strings written to the database are maintained in the same place, and
    cannot disagree.
    """
    return {
        'actions': {name: {'label': entry.label.replace(SUBJECT, 'the record'),
                           'category': entry.category,
                           'severity': entry.severity}
                    for name, entry in sorted(ACTIONS.items())},
        'categories': [{'id': key, 'label': label} for key, label in CATEGORIES],
        'outcomes': [{'id': key, 'label': label} for key, label in OUTCOME_LABELS.items()],
        'severities': list(SEVERITIES),
    }