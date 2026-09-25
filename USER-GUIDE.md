# Using Duesdesk

## Start

Open the app, sign in, and choose the reporting month. Local test data is clearly labelled. Before using real records, deploy to a fresh live database, create your own secretary account, and complete the deployment checklist.

## Members

Choose **Members → Add member**. Enter the full name and joining date; phone and email are optional. The member ID is automatic. Dues begin in the joining month.

Select a member's name to see total paid, cumulative arrears through the current month, and every receipt. Secretaries can use **Edit member** to update contact details and status. The joining date locks once payments exist, to protect historical balances.

When membership ends, set the **last billable month**. The person still owes unpaid dues for prior months, but no new monthly dues accrue after that month. Existing allocations beyond the chosen date must be corrected first. Inactive/suspended members cannot receive new payments; reactivate them before collecting arrears. Changing status alone does not erase debt.

## Receive GH₵100

1. Select **Record payment** and choose a member.
2. Enter **100**, choose the first month, the payment date and method.
3. Review the four GH₵25 allocations. Already-paid months are skipped.
4. Save, then open the receipt. Use **Print / save PDF** to print or save through your browser.

If a member already paid GH₵10 for the starting month, the app allocates GH₵15 to finish it and sends the remaining payment to later months. Amounts smaller than GH₵25 are allowed. Earlier arrears are not automatically paid if you choose a later starting month. Always inspect the preview. The server recomputes it when saving to avoid over-allocation if another secretary has just recorded a payment.

## Correct a mistake

Open **Payments**, find the receipt, and select **Void**. Enter a clear reason. The receipt remains marked VOID; its allocations no longer count as paid. Record a new payment with the correct details. Voiding is an administrative correction, not a bank transfer or an automatic refund.

## Reports

The selected month controls the overview, member balances and reports. Collections are money received that month. Covered dues are money allocated to that month, including advance payments. Arrears sum unpaid dues from joining through that month, respecting the last billable month.

Under **Reports**, choose balances or the received-payment ledger and download Excel or CSV. The payment ledger includes voided rows clearly marked; do not total voided payments as valid collections. Reports include only the records your role may view.

## Import

In **Manage → Import records**, download the template, fill it, and upload a UTF-8 CSV. Dates use `YYYY-MM-DD`; covered starting months use `YYYY-MM`. Use numeric member IDs in payment imports. You may import up to 500 rows and 1 MB per file. Review the preview before confirming. A failed commit rolls back the whole batch. Reimporting the exact same file is blocked. A modified file is a different batch, so review carefully to avoid re-entering the same real-world transaction.

## Roles and accounts

- **Secretary:** manages members, payments, corrections, imports, reports, organisation details and accounts. Give this role only to trusted administrators.
- **Auditor:** reads all records, reports and audit history; cannot change them.
- **Member:** sees only their linked member record, payments, receipts and reports.

Create accounts in **Manage**, using a unique email and a strong initial password. Secretaries can also register at `/signup/`; that flow always creates a secretary role. Share the password privately; ask the recipient to change it. Disabling an account removes access, including existing sessions, while retaining its historical records. To re-enable an account, an operator must use the management shell (`User.objects.filter(username='...').update(is_active=True)`) and record the change operationally. If an older account is missing its access role, assign it explicitly with `python backend/manage.py assign_access <username> --role secretary`; use `--role member --member-id <id>` for a linked member account.

Use **Change password** while signed in, or **Forgot your password?** on the sign-in page. Five failed login attempts lock that account for 15 minutes. In local mode recovery emails are written to the server log; in production they require the configured SMTP provider.

## Audit and privacy

Audit history records creation, updates, imports, exports, account changes and payment voids. It cannot be modified through the app. Database administrators retain direct database powers; see deployment instructions for restricting the runtime database user. Keep exports and backups private. Only collect member details your organisation needs.


## Download one member's payment report

The secretary opens Members, selects the member's name, then clicks **Download payment report (Excel)**. No member account or input is needed. The file contains only that member: a summary, one row per payment, a separate breakdown of months covered, and monthly balances. Amounts are in GHS. Outstanding dues run through the current month; advance payments are labelled separately. Voided receipts remain visible but do not count toward totals.
