import datetime
import smtplib
from decimal import Decimal
from unittest import mock

import pytest
from django.core import mail

from billing.models import Invoice, InvoiceEmail
from billing.services import SEND_ALREADY_SENT, SEND_SENT, SEND_SKIPPED, run_billing, send_invoice_email
from contracts.models import TimesheetEntry

OCTOBER = datetime.date(2026, 10, 1)


def _entry(contract, date, hours='8.0'):
    return TimesheetEntry.objects.create(
        contract=contract, date=date, hours=Decimal(hours), status='approved'
    )


@pytest.fixture
def billable(active_contract):
    _entry(active_contract, datetime.date(2026, 10, 1))
    _entry(active_contract, datetime.date(2026, 10, 2), hours='7.5')
    return active_contract


# --- sending after commit ---

@pytest.mark.django_db
def test_emails_are_sent_only_after_commit(northstar, admin_user, billable, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        run_billing(northstar, OCTOBER, admin_user)

    # Inside the transaction nothing has been sent; sending is deferred to
    # commit, one callback per email (company and freelancer).
    assert len(callbacks) == 2
    assert mail.outbox == []
    assert all(e.status == InvoiceEmail.STATUS_PENDING for e in InvoiceEmail.objects.all())


@pytest.mark.django_db
def test_invoice_is_emailed_to_company_and_freelancer(northstar, admin_user, billable, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        run_billing(northstar, OCTOBER, admin_user)

    invoice = Invoice.objects.get()
    assert sorted(m.to[0] for m in mail.outbox) == ['billing@northstar.test', 'freelancer@test.test']
    for message in mail.outbox:
        assert message.subject == f'Invoice {invoice.number}: NorthStar Consulting, October 2026'
        assert message.from_email == 'billing@yunojuno.test'
        assert invoice.number in message.body
        assert '2026-10-01' in message.body and '£600.00' in message.body
        assert '2026-10-02' in message.body and '£562.50' in message.body
        assert 'Total: £1162.50 (GBP, excluding VAT)' in message.body

    for email in InvoiceEmail.objects.all():
        assert (email.status, email.attempts, email.last_error) == (InvoiceEmail.STATUS_SENT, 1, '')
        assert email.sent_at is not None


@pytest.mark.django_db
def test_email_body_is_plain_text_not_html_escaped(northstar, admin_user, billable, django_capture_on_commit_callbacks):
    northstar.name = 'Smith & Jones'
    northstar.save()

    with django_capture_on_commit_callbacks(execute=True):
        run_billing(northstar, OCTOBER, admin_user)

    assert 'Smith & Jones' in mail.outbox[0].body
    assert '&amp;' not in mail.outbox[0].body


# --- failures never roll back the invoice ---

@pytest.mark.django_db
def test_failed_send_keeps_the_invoice_and_records_the_error(northstar, admin_user, billable, django_capture_on_commit_callbacks):
    with mock.patch('billing.services.send_mail', side_effect=ConnectionRefusedError('Connection refused')):
        with django_capture_on_commit_callbacks(execute=True):
            run = run_billing(northstar, OCTOBER, admin_user)

    assert run.invoices_generated == 1
    assert Invoice.objects.get().lines.count() == 2
    for email in InvoiceEmail.objects.all():
        assert (email.status, email.attempts, email.sent_at) == (InvoiceEmail.STATUS_FAILED, 1, None)
        assert email.last_error == 'ConnectionRefusedError: Connection refused'


@pytest.mark.django_db
def test_one_recipient_failing_does_not_affect_the_other(northstar, admin_user, billable, django_capture_on_commit_callbacks):
    with mock.patch('billing.services.send_mail', side_effect=[1, smtplib.SMTPRecipientsRefused({})]):
        with django_capture_on_commit_callbacks(execute=True):
            run_billing(northstar, OCTOBER, admin_user)

    company_email, freelancer_email = InvoiceEmail.objects.order_by('id')
    assert company_email.status == InvoiceEmail.STATUS_SENT
    assert freelancer_email.status == InvoiceEmail.STATUS_FAILED
    assert freelancer_email.last_error.startswith('SMTPRecipientsRefused')


# --- send_invoice_email on its own ---

@pytest.mark.django_db
def test_sent_email_is_never_sent_again(northstar, admin_user, billable, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        run_billing(northstar, OCTOBER, admin_user)
    email = InvoiceEmail.objects.first()

    assert send_invoice_email(email.id) == SEND_ALREADY_SENT

    email.refresh_from_db()
    assert email.attempts == 1
    assert len(mail.outbox) == 2


@pytest.mark.django_db
def test_failed_email_can_be_sent_later(northstar, admin_user, billable, django_capture_on_commit_callbacks):
    with mock.patch('billing.services.send_mail', side_effect=ConnectionRefusedError('down')):
        with django_capture_on_commit_callbacks(execute=True):
            run_billing(northstar, OCTOBER, admin_user)
    email = InvoiceEmail.objects.first()

    assert send_invoice_email(email.id) == SEND_SENT

    email.refresh_from_db()
    assert (email.status, email.attempts, email.last_error) == (InvoiceEmail.STATUS_SENT, 2, '')
    assert [m.to for m in mail.outbox] == [[email.recipient]]


@pytest.mark.django_db
def test_rerunning_billing_does_not_retry_failed_emails(northstar, admin_user, billable, django_capture_on_commit_callbacks):
    with mock.patch('billing.services.send_mail', side_effect=ConnectionRefusedError('down')):
        with django_capture_on_commit_callbacks(execute=True):
            run_billing(northstar, OCTOBER, admin_user)

    with django_capture_on_commit_callbacks(execute=True):
        run_billing(northstar, OCTOBER, admin_user)

    # Retries belong to the retry command, not to the web app.
    assert mail.outbox == []
    assert all(e.status == InvoiceEmail.STATUS_FAILED for e in InvoiceEmail.objects.all())


# --- API reports delivery failures ---
# transaction=True so the request's transaction really commits and the
# on-commit sends run before the view builds its response.

@pytest.mark.django_db(transaction=True)
def test_billing_run_reports_sent_emails(admin_client, billable):
    resp = admin_client.post('/api/billing/runs/', {'month': '2026-10'})

    assert resp.status_code == 201
    assert (resp.data['emails_failed'], resp.data['emails_pending'], resp.data['status']) == (0, 0, 'completed')
    assert len(mail.outbox) == 2


@pytest.mark.django_db(transaction=True)
def test_billing_run_reports_failed_emails(admin_client, billable):
    with mock.patch('billing.services.send_mail', side_effect=ConnectionRefusedError('down')):
        resp = admin_client.post('/api/billing/runs/', {'month': '2026-10'})

    assert resp.status_code == 201
    assert resp.data['invoices_generated'] == 1
    assert (resp.data['emails_failed'], resp.data['status']) == (2, 'completed_with_errors')


# --- any send error is recorded, never raised ---

@pytest.mark.django_db
def test_unexpected_send_error_is_recorded_not_raised(northstar, admin_user, billable, django_capture_on_commit_callbacks):
    # A newline in the company name makes Django raise BadHeaderError (a
    # ValueError, not an SMTP error) when building the subject.
    northstar.name = 'NorthStar\nConsulting'
    northstar.save()

    with django_capture_on_commit_callbacks(execute=True):
        run = run_billing(northstar, OCTOBER, admin_user)

    assert run.invoices_generated == 1
    for email in InvoiceEmail.objects.all():
        assert (email.status, email.attempts) == (InvoiceEmail.STATUS_FAILED, 1)
        assert email.last_error.startswith('BadHeaderError')


@pytest.mark.django_db
def test_one_bad_email_does_not_stop_the_others(northstar, admin_user, billable, django_capture_on_commit_callbacks):
    with mock.patch('billing.services.send_mail', side_effect=[ValueError('boom'), 1]):
        with django_capture_on_commit_callbacks(execute=True):
            run_billing(northstar, OCTOBER, admin_user)

    first, second = InvoiceEmail.objects.order_by('id')
    assert (first.status, first.last_error) == (InvoiceEmail.STATUS_FAILED, 'ValueError: boom')
    assert second.status == InvoiceEmail.STATUS_SENT


@pytest.mark.django_db(transaction=True)
def test_billing_run_with_unexpected_send_error_still_returns_201(admin_client, northstar, billable):
    northstar.name = 'NorthStar\nConsulting'
    northstar.save()

    resp = admin_client.post('/api/billing/runs/', {'month': '2026-10'})

    assert resp.status_code == 201
    assert (resp.data['invoices_generated'], resp.data['emails_failed']) == (1, 2)


@pytest.mark.django_db(transaction=True)
def test_an_error_escaping_one_send_does_not_stop_the_others_or_fail_the_run(admin_client, billable):
    # E.g. the database drops while one send is being recorded: the other
    # email is still sent, and the committed run still returns 201.
    from django.db import DatabaseError

    with mock.patch('billing.services.send_invoice_email', side_effect=[DatabaseError('connection lost'), SEND_SENT]) as send:
        resp = admin_client.post('/api/billing/runs/', {'month': '2026-10'})

    assert resp.status_code == 201
    assert resp.data['invoices_generated'] == 1
    assert send.call_count == 2


@pytest.mark.django_db(transaction=True)
def test_billing_run_reports_emails_left_pending(admin_client, billable):
    # e.g. skipped because the retry command held the row at that moment: not
    # failed, but not sent either, so the run isn't reported as completed.
    with mock.patch('billing.services.send_invoice_email', return_value=SEND_SKIPPED):
        resp = admin_client.post('/api/billing/runs/', {'month': '2026-10'})

    assert resp.status_code == 201
    assert (resp.data['emails_failed'], resp.data['emails_pending'], resp.data['status']) == (
        0, 2, 'completed_with_errors'
    )
