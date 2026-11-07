"""
Retry invoice emails that were not delivered when billing ran.

This is a recovery tool, not the way invoice emails are normally sent: a
billing run sends its emails itself, straight after its invoices commit. This
command picks up what that missed:

  failed   the first attempt hit an error (mail server down, recipient refused)
  pending  the invoice committed but the process stopped before its send ran

Rows already sent are never sent again, and rows that have failed
MAX_EMAIL_ATTEMPTS times are left as failed for a person to look at. Safe to
run at any time, including while a billing run is sending: each row is locked
while it is sent. In production it would run on a schedule (e.g. cron every
few minutes).
"""
from collections import Counter

from django.core.management.base import BaseCommand

from billing.models import InvoiceEmail
from billing.services import (
    MAX_EMAIL_ATTEMPTS,
    SEND_ALREADY_SENT,
    SEND_FAILED,
    SEND_SENT,
    SEND_SKIPPED,
    retryable_emails,
    send_invoice_email,
)


class Command(BaseCommand):
    help = 'Retry invoice emails that failed or were never sent (recovery tool, not the main sending path).'

    def handle(self, *args, **options):
        at_limit = InvoiceEmail.objects.filter(
            status=InvoiceEmail.STATUS_FAILED, attempts__gte=MAX_EMAIL_ATTEMPTS
        ).count()
        if at_limit:
            self.stdout.write(self.style.WARNING(
                f'Not retrying {at_limit} email(s) that reached the {MAX_EMAIL_ATTEMPTS}-attempt limit; they need attention.'
            ))

        email_ids = list(retryable_emails().order_by('id').values_list('id', flat=True))
        if not email_ids:
            self.stdout.write('No invoice emails to retry.')
            return

        self.stdout.write(f'Retrying {len(email_ids)} invoice email(s)…')
        # Count what each attempt actually did. Reading row statuses back
        # afterwards would misreport rows another process held or sent meanwhile
        # (e.g. a failed row it held would look like "still failing" here).
        outcomes = {email_id: send_invoice_email(email_id) for email_id in email_ids}
        counts = Counter(outcomes.values())
        failed_ids = [email_id for email_id, outcome in outcomes.items() if outcome == SEND_FAILED]
        given_up = InvoiceEmail.objects.filter(id__in=failed_ids, attempts__gte=MAX_EMAIL_ATTEMPTS).count()

        # Every row in "Retrying N" is accounted for by exactly one line below.
        self.stdout.write(self.style.SUCCESS(f'Sent: {counts[SEND_SENT]}'))
        if counts[SEND_FAILED]:
            self.stdout.write(self.style.WARNING(
                f'Still failing: {counts[SEND_FAILED]} ({given_up} reached the {MAX_EMAIL_ATTEMPTS}-attempt limit)'
            ))
        if counts[SEND_SKIPPED]:
            self.stdout.write(f'Skipped: {counts[SEND_SKIPPED]} (being sent by another process; picked up next run if needed)')
        if counts[SEND_ALREADY_SENT]:
            self.stdout.write(f'Already sent by another process: {counts[SEND_ALREADY_SENT]}')
