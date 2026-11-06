import calendar
import datetime
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
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


def parse_period(value: str) -> datetime.date:
    """Parse a 'YYYY-MM' billing month into its first day.

    Raises ValueError for a malformed value or a month in the future. The
    current month is allowed: entries approved later in it roll into the next
    month's invoice.
    """
    try:
        period = datetime.datetime.strptime(value, '%Y-%m').date()
    except (TypeError, ValueError):
        raise ValueError('Month must be in YYYY-MM format.')
    today = timezone.localdate()
    if period > today.replace(day=1):
        raise ValueError('Cannot bill a future month.')
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
        Company.objects.select_for_update().get(pk=company.pk)

        run = BillingRun.objects.create(company=company, period=period, run_by=run_by)

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
            .select_related('contract__freelancer__user')
            .order_by('date', 'id')
        )

        by_freelancer = defaultdict(list)
        for entry in entries:
            by_freelancer[entry.contract.freelancer].append(entry)

        already_invoiced = set(
            Invoice.objects.filter(company=company, period=period).values_list('freelancer_id', flat=True)
        )

        for freelancer, freelancer_entries in by_freelancer.items():
            # Entries approved after this period was invoiced roll into the
            # next period's invoice.
            if freelancer.id in already_invoiced:
                continue
            _create_invoice(run, company, freelancer, period, freelancer_entries)
            run.invoices_generated += 1

        run.save(update_fields=['invoices_generated'])
    return run


def _create_invoice(run, company, freelancer, period, entries) -> Invoice:
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
    InvoiceEmail.objects.bulk_create(
        InvoiceEmail(invoice=invoice, recipient=address) for address in recipients
    )
    return invoice
