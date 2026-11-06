import calendar
import datetime
import re
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from django.core.mail import send_mail
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

from accounts.models import Company
from contracts.models import TimesheetEntry
from .models import BillingRun, Invoice, InvoiceEmail, InvoiceLine

# Contracts have a daily rate but timesheet entries log hours. A working day is
# 8 hours, the same assumption the frontend's cost estimate already makes
# (ContractDetail.tsx), so billed amounts match what users have been shown.
HOURS_PER_DAY = Decimal('8')
PENNY = Decimal('0.01')


def line_amount(daily_rate: Decimal, hours: Decimal) -> Decimal:
    # Each line is rounded to the penny, half-up, and the invoice total is the
    # sum of the rounded lines, so the printed lines always add up to the total.
    # The rounding mode is explicit because Decimal defaults to half-even.
    return (daily_rate * hours / HOURS_PER_DAY).quantize(PENNY, rounding=ROUND_HALF_UP)


def parse_period(value: str, only_ended: bool = True) -> datetime.date:
    """Parse a 'YYYY-MM' billing month into its first day.

    Raises ValueError for a malformed value or, when only_ended, a month that
    hasn't ended yet. There is one invoice per freelancer per month, so billing
    a month that is still running would issue a partial invoice that can never
    be completed; the rest of its work would land on the next month's invoice.
    """
    # strptime alone also accepts e.g. '2026-1', so check the exact shape first.
    if not isinstance(value, str) or not re.fullmatch(r'[12]\d{3}-\d{2}', value):
        raise ValueError('Month must be in YYYY-MM format.')
    try:
        period = datetime.datetime.strptime(value, '%Y-%m').date()
    except ValueError:
        raise ValueError('Month must be in YYYY-MM format.')
    if only_ended and period >= timezone.localdate().replace(day=1):
        raise ValueError('Can only bill a month that has ended.')
    return period


def period_end(period: datetime.date) -> datetime.date:
    return period.replace(day=calendar.monthrange(period.year, period.month)[1])


def run_billing(company: Company, period: datetime.date, run_by) -> BillingRun:
    """Create invoices for every approved, not-yet-billed entry of the company
    dated on or before the end of the billing month: one invoice per freelancer.

    The whole run is one transaction, so it either happens completely or not
    at all. Re-running a month is safe: a freelancer who already has an invoice
    for the period is skipped, and an entry can only ever be on one invoice
    line (one-to-one), whatever runs overlap.
    """
    with transaction.atomic():
        # Serialise runs per company. A concurrent run waits here and, once the
        # first commits, sees its invoices and lines, so it creates nothing twice.
        # Use the row as read under the lock from here on: the instance passed
        # in may be stale (e.g. its billing_email changed while this run waited).
        company = Company.objects.select_for_update().get(pk=company.pk)

        run = BillingRun.objects.create(company=company, period=period, run_by=run_by)

        # Freelancers who already have an invoice for this period are skipped:
        # their entries approved since then roll into the next period's invoice.
        # Excluded in the query itself, so a re-run doesn't fetch, or lock,
        # entries it won't bill (that would block approvals on them for nothing).
        already_invoiced = set(
            Invoice.objects.filter(company=company, period=period).values_list('freelancer_id', flat=True)
        )

        # Lock the entries being billed so they can't be rejected or edited
        # half-way through the run.
        entries = (
            TimesheetEntry.objects.select_for_update(of=('self',))
            .filter(
                contract__company=company,
                status=TimesheetEntry.STATUS_APPROVED,
                date__lte=period_end(period),
                invoice_line__isnull=True,
            )
            .exclude(contract__freelancer_id__in=already_invoiced)
            .select_related('contract__freelancer__user')
            .order_by('date', 'id')
        )

        by_freelancer = defaultdict(list)
        for entry in entries:
            by_freelancer[entry.contract.freelancer].append(entry)

        email_ids = []
        for freelancer, freelancer_entries in by_freelancer.items():
            email_ids.extend(_create_invoice(run, company, freelancer, period, freelancer_entries))
            run.invoices_generated += 1

        run.save(update_fields=['invoices_generated'])

        # Send only once the invoices are committed: an email must never go
        # out for an invoice that was rolled back, and a failed send must never
        # roll back an invoice. Failures stay on their outbox rows for retry.
        # One robust callback per email: if anything escapes one send (e.g. the
        # database drops while recording it), Django logs it and still runs the
        # rest, and the committed run isn't turned into a 500. That row stays
        # pending for the retry command.
        for email_id in email_ids:
            transaction.on_commit(_after_commit_sender(email_id), robust=True)
    return run


def _after_commit_sender(email_id):
    # A real function rather than functools.partial: when a robust on_commit
    # callback fails, Django logs it by __qualname__, which partial lacks, so
    # the logging itself would raise and defeat robust=True.
    def send_after_commit():
        send_invoice_email(email_id)
    return send_after_commit


def _create_invoice(run, company, freelancer, period, entries) -> list[int]:
    """Create one invoice with its lines and outbox rows; return the outbox row ids."""
    invoice = Invoice.objects.create(run=run, company=company, freelancer=freelancer, period=period)
    # The number embeds the primary key, which only exists after the insert.
    invoice.number = f'INV-{period:%Y-%m}-{invoice.pk:06d}'

    lines = [
        InvoiceLine(
            invoice=invoice,
            timesheet_entry=entry,
            date=entry.date,
            hours=entry.hours,
            daily_rate=entry.contract.daily_rate,
            amount=line_amount(entry.contract.daily_rate, entry.hours),
        )
        for entry in entries
    ]
    InvoiceLine.objects.bulk_create(lines)
    invoice.total = sum((line.amount for line in lines), Decimal('0'))
    invoice.save(update_fields=['number', 'total'])

    # Outbox rows, committed with the invoice; sending happens after commit.
    # If the company and freelancer share an address, it gets one email.
    recipients = []
    for address in (company.billing_email, freelancer.user.email):
        if address and address.lower() not in {r.lower() for r in recipients}:
            recipients.append(address)
    emails = InvoiceEmail.objects.bulk_create(
        InvoiceEmail(invoice=invoice, recipient=address) for address in recipients
    )
    # Postgres returns the new ids from bulk_create, so no query is needed to
    # find which rows to send after commit.
    return [email.pk for email in emails]


# After this many failed attempts an email is left as failed for a person to
# look at, instead of being retried forever.
MAX_EMAIL_ATTEMPTS = 5


def retryable_emails():
    """Outbox rows the retry command should try: failed rows, and pending rows
    whose first send never happened (e.g. the process stopped before the
    on-commit send ran), below the attempt limit."""
    return InvoiceEmail.objects.exclude(status=InvoiceEmail.STATUS_SENT).filter(
        attempts__lt=MAX_EMAIL_ATTEMPTS
    )


def send_invoice_email(email_id: int) -> bool:
    """Try to deliver one outbox row. Returns True if the row is sent.

    The row is locked while sending, and a row another sender holds is
    skipped, so the same email is never sent twice at once. A row already
    sent is left alone. Delivery is at-least-once: if the process dies after
    the mail server accepts the email but before the row is saved as sent,
    a retry sends it again.
    """
    with transaction.atomic():
        email = (
            InvoiceEmail.objects.select_for_update(skip_locked=True, of=('self',))
            .select_related('invoice__company', 'invoice__freelancer')
            .filter(pk=email_id)
            .first()
        )
        if email is None:
            return False
        if email.status == InvoiceEmail.STATUS_SENT:
            return True

        invoice = email.invoice
        email.attempts += 1
        try:
            subject = f'Invoice {invoice.number}: {invoice.company.name}, {invoice.period:%B %Y}'
            body = render_to_string(
                'billing/invoice_email.txt', {'invoice': invoice, 'lines': invoice.lines.all()}
            )
            send_mail(subject, body, None, [email.recipient])
        except Exception as exc:
            # Deliberately broad. This runs after the invoice has committed, so
            # any failure (mail server down, or e.g. BadHeaderError for a company
            # name containing a newline) must be recorded on this row, not raised:
            # raising would skip the run's remaining emails, turn a committed run
            # into a 500, and roll back this attempt so the row never reaches
            # MAX_EMAIL_ATTEMPTS and blocks the retry command forever.
            email.status = InvoiceEmail.STATUS_FAILED
            email.last_error = f'{type(exc).__name__}: {exc}'
        else:
            email.status = InvoiceEmail.STATUS_SENT
            email.sent_at = timezone.now()
            email.last_error = ''
        email.save(update_fields=['status', 'attempts', 'last_error', 'sent_at'])
    return email.status == InvoiceEmail.STATUS_SENT
