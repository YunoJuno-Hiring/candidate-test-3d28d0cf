from django.contrib.auth.models import User
from django.db import models
from django.db.models import Q

from accounts.models import Company, Freelancer
from contracts.models import TimesheetEntry

# Invoices are financial records, so references from billing to the rest of the
# system use PROTECT rather than the CASCADE used elsewhere in the codebase:
# deleting a company, freelancer, user or timesheet entry must fail instead of
# silently deleting invoices that have already been issued. Invoices protect
# their own lines and emails too: deleting an invoice would free its timesheet
# entries to be billed again, so an issued invoice can't be deleted at all.


class BillingRun(models.Model):
    """Audit record of every billing run: who billed which month, and when.

    Runs that generate no invoices (e.g. re-runs of a month already billed)
    are recorded too.
    """

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name='billing_runs')
    period = models.DateField()  # first day of the billing month
    run_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='billing_runs')
    invoices_generated = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(condition=Q(period__day=1), name='billing_run_period_first_of_month'),
        ]

    def __str__(self):
        return f'{self.company.name} — {self.period:%Y-%m} (run {self.pk})'


class Invoice(models.Model):
    """One invoice per company × freelancer × billing month.

    The period is the billing month, not the month the work was done: an
    invoice covers every approved, not-yet-billed entry dated on or before the
    end of the period, so late approvals roll into the next month's invoice.
    """

    run = models.ForeignKey(BillingRun, on_delete=models.PROTECT, related_name='invoices')
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name='invoices')
    freelancer = models.ForeignKey(Freelancer, on_delete=models.PROTECT, related_name='invoices')
    period = models.DateField()  # first day of the billing month
    # INV-YYYY-MM-<id>. It embeds the primary key, which only exists after the
    # first save, so it is null until then and set in the same transaction.
    number = models.CharField(max_length=32, unique=True, null=True, blank=True)
    # GBP, excluding VAT. Stored rather than computed so an issued invoice's
    # total never changes; it is the sum of the already-rounded line amounts.
    total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-period', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['company', 'freelancer', 'period'],
                name='unique_invoice_per_company_freelancer_period',
            ),
            models.CheckConstraint(condition=Q(period__day=1), name='invoice_period_first_of_month'),
        ]

    def __str__(self):
        return self.number or f'Invoice {self.pk}'


class InvoiceLine(models.Model):
    """One billed timesheet entry.

    hours, daily_rate and amount are what was billed, so they are stored on the
    line: recomputing from the contract's current rate would change invoices
    that have already been sent. The one-to-one link to the entry guarantees an
    entry is billed at most once.
    """

    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name='lines')
    timesheet_entry = models.OneToOneField(
        TimesheetEntry, on_delete=models.PROTECT, related_name='invoice_line'
    )
    date = models.DateField()  # the day the work was done
    hours = models.DecimalField(max_digits=5, decimal_places=2)
    daily_rate = models.DecimalField(max_digits=8, decimal_places=2)
    amount = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        ordering = ['date']

    def __str__(self):
        return f'{self.invoice} — {self.date} ({self.hours}h, £{self.amount})'


class InvoiceEmail(models.Model):
    """Delivery record for one recipient of an invoice (transactional outbox).

    Rows are created as pending in the same transaction as the invoice and sent
    after it commits, so a failed send never rolls back the invoice. recipient
    is the address the email was actually sent to, read from the company or the
    freelancer's user when the row is created.
    """

    STATUS_PENDING = 'pending'
    STATUS_SENT = 'sent'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_SENT, 'Sent'),
        (STATUS_FAILED, 'Failed'),
    ]

    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name='emails')
    recipient = models.EmailField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True, default='')
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']
        constraints = [
            models.UniqueConstraint(fields=['invoice', 'recipient'], name='unique_invoice_email_recipient'),
        ]

    def __str__(self):
        return f'{self.invoice} → {self.recipient} ({self.status})'
