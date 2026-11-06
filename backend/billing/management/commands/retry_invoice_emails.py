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
from django.core.management.base import BaseCommand

from billing.models import InvoiceEmail
from billing.services import MAX_EMAIL_ATTEMPTS, retryable_emails, send_invoice_email


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
        for email_id in email_ids:
            send_invoice_email(email_id)

        retried = InvoiceEmail.objects.filter(id__in=email_ids)
        sent = retried.filter(status=InvoiceEmail.STATUS_SENT).count()
        failed = retried.filter(status=InvoiceEmail.STATUS_FAILED)
        given_up = failed.filter(attempts__gte=MAX_EMAIL_ATTEMPTS).count()

        self.stdout.write(self.style.SUCCESS(f'Sent: {sent}'))
        if failed.exists():
            self.stdout.write(self.style.WARNING(
                f'Still failing: {failed.count()} ({given_up} reached the {MAX_EMAIL_ATTEMPTS}-attempt limit)'
            ))
        # Every attempt records sent or failed, so a row still pending was
        # skipped: another sender (a billing run, or an overlapping run of this
        # command) held it. Report it so every row in "Retrying N" is accounted for.
        skipped = retried.filter(status=InvoiceEmail.STATUS_PENDING).count()
        if skipped:
            self.stdout.write(f'Skipped: {skipped} (being sent by another process; picked up next run if needed)')
