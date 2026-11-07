import datetime
import smtplib
from decimal import Decimal
from io import StringIO
from unittest import mock

import pytest
from django.core import mail
from django.core.management import call_command

from billing.models import InvoiceEmail
from billing.services import MAX_EMAIL_ATTEMPTS, SEND_ALREADY_SENT, SEND_SKIPPED, run_billing
from contracts.models import TimesheetEntry

OCTOBER = datetime.date(2026, 10, 1)


def _retry():
    out = StringIO()
    call_command('retry_invoice_emails', stdout=out)
    return out.getvalue()


@pytest.fixture
def invoice_emails(northstar, admin_user, active_contract):
    # A normal db test never commits, so the on-commit send doesn't run and
    # both rows stay pending: exactly the "never attempted" case.
    TimesheetEntry.objects.create(
        contract=active_contract, date=datetime.date(2026, 10, 1), hours=Decimal('8.0'), status='approved'
    )
    run_billing(northstar, OCTOBER, admin_user)
    return list(InvoiceEmail.objects.order_by('id'))


@pytest.mark.django_db
def test_nothing_to_retry():
    assert 'No invoice emails to retry.' in _retry()
    assert mail.outbox == []


@pytest.mark.django_db
def test_sends_pending_emails_that_were_never_attempted(invoice_emails):
    output = _retry()

    assert sorted(m.to[0] for m in mail.outbox) == ['billing@northstar.test', 'freelancer@test.test']
    assert all(e.status == InvoiceEmail.STATUS_SENT for e in InvoiceEmail.objects.all())
    assert 'Sent: 2' in output


@pytest.mark.django_db
def test_retries_failed_emails(invoice_emails):
    InvoiceEmail.objects.update(status=InvoiceEmail.STATUS_FAILED, attempts=1, last_error='ConnectionRefusedError: down')

    _retry()

    for email in InvoiceEmail.objects.all():
        assert (email.status, email.attempts, email.last_error) == (InvoiceEmail.STATUS_SENT, 2, '')


@pytest.mark.django_db
def test_never_resends_sent_emails(invoice_emails):
    company_email, freelancer_email = invoice_emails
    InvoiceEmail.objects.filter(pk=company_email.pk).update(status=InvoiceEmail.STATUS_SENT, attempts=1)
    InvoiceEmail.objects.filter(pk=freelancer_email.pk).update(status=InvoiceEmail.STATUS_FAILED, attempts=1)

    _retry()

    # Only the recipient who missed out gets the retry.
    assert [m.to for m in mail.outbox] == [['freelancer@test.test']]


@pytest.mark.django_db
def test_still_failing_emails_count_another_attempt(invoice_emails):
    with mock.patch('billing.services.send_mail', side_effect=smtplib.SMTPServerDisconnected('gone')):
        output = _retry()

    for email in InvoiceEmail.objects.all():
        assert (email.status, email.attempts) == (InvoiceEmail.STATUS_FAILED, 1)
        assert email.last_error == 'SMTPServerDisconnected: gone'
    assert 'Still failing: 2' in output


@pytest.mark.django_db
def test_skips_emails_at_the_attempt_limit(invoice_emails):
    InvoiceEmail.objects.update(status=InvoiceEmail.STATUS_FAILED, attempts=MAX_EMAIL_ATTEMPTS)

    output = _retry()

    assert mail.outbox == []
    assert all(e.attempts == MAX_EMAIL_ATTEMPTS for e in InvoiceEmail.objects.all())
    assert f'Not retrying 2 email(s) that reached the {MAX_EMAIL_ATTEMPTS}-attempt limit' in output


@pytest.mark.django_db
def test_reports_emails_reaching_the_limit(invoice_emails):
    InvoiceEmail.objects.update(status=InvoiceEmail.STATUS_FAILED, attempts=MAX_EMAIL_ATTEMPTS - 1)

    with mock.patch('billing.services.send_mail', side_effect=ConnectionRefusedError('down')):
        output = _retry()

    assert all(e.attempts == MAX_EMAIL_ATTEMPTS for e in InvoiceEmail.objects.all())
    assert f'Still failing: 2 (2 reached the {MAX_EMAIL_ATTEMPTS}-attempt limit)' in output


@pytest.mark.django_db
def test_a_bad_row_does_not_abort_the_command(invoice_emails):
    # Previously an unexpected error escaped, aborted the command and rolled
    # back the attempt, so the row never reached the limit.
    with mock.patch('billing.services.send_mail', side_effect=[ValueError('bad header'), 1]):
        output = _retry()

    bad, good = InvoiceEmail.objects.order_by('id')
    assert (bad.status, bad.attempts, bad.last_error) == (InvoiceEmail.STATUS_FAILED, 1, 'ValueError: bad header')
    assert good.status == InvoiceEmail.STATUS_SENT
    assert 'Sent: 1' in output and 'Still failing: 1' in output


@pytest.mark.django_db
def test_reports_rows_another_sender_held(invoice_emails):
    # send_invoice_email skips a row another process holds, leaving it pending.
    with mock.patch('billing.management.commands.retry_invoice_emails.send_invoice_email', return_value=SEND_SKIPPED):
        output = _retry()

    assert 'Retrying 2 invoice email(s)' in output
    assert 'Sent: 0' in output
    assert 'Skipped: 2 (being sent by another process' in output


@pytest.mark.django_db
def test_failed_row_held_elsewhere_is_reported_as_skipped_not_failing(invoice_emails):
    # The review's case: a failed row another process holds is never attempted
    # here, so it must not be reported as "Still failing".
    InvoiceEmail.objects.update(status=InvoiceEmail.STATUS_FAILED, attempts=1)

    with mock.patch('billing.management.commands.retry_invoice_emails.send_invoice_email', return_value=SEND_SKIPPED):
        output = _retry()

    assert 'Skipped: 2' in output
    assert 'Still failing' not in output


@pytest.mark.django_db
def test_rows_sent_by_another_process_are_not_counted_as_sent_here(invoice_emails):
    with mock.patch('billing.management.commands.retry_invoice_emails.send_invoice_email', return_value=SEND_ALREADY_SENT):
        output = _retry()

    assert 'Sent: 0' in output
    assert 'Already sent by another process: 2' in output


@pytest.mark.django_db(transaction=True)
def test_row_locked_by_another_process_is_really_skipped(invoice_emails):
    # End to end on Postgres, no mocks: another transaction holds a failed row
    # (as an overlapping retry run would). The command must skip it without
    # an attempt and report it as skipped; the other row is sent.
    import threading

    from django.db import connection, transaction

    InvoiceEmail.objects.update(status=InvoiceEmail.STATUS_FAILED, attempts=1)
    held, other = invoice_emails
    holding, release = threading.Event(), threading.Event()

    def hold_lock():
        try:
            with transaction.atomic():
                InvoiceEmail.objects.select_for_update().get(pk=held.pk)
                holding.set()
                release.wait(10)
        finally:
            connection.close()

    holder = threading.Thread(target=hold_lock)
    holder.start()
    try:
        assert holding.wait(10)
        output = _retry()
    finally:
        release.set()
        holder.join()

    held.refresh_from_db()
    other.refresh_from_db()
    assert (held.status, held.attempts) == (InvoiceEmail.STATUS_FAILED, 1)  # not attempted
    assert other.status == InvoiceEmail.STATUS_SENT
    assert 'Sent: 1' in output and 'Skipped: 1' in output
    assert 'Still failing' not in output
