import datetime
import smtplib
from decimal import Decimal
from unittest import mock

import pytest
from django.core import mail

from billing.models import Invoice, InvoiceEmail
from billing.services import run_billing, send_invoice_email
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

    # Inside the transaction nothing has been sent; sending is deferred to commit.
    assert len(callbacks) == 1
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

    assert send_invoice_email(email.id) is True

    email.refresh_from_db()
    assert email.attempts == 1
    assert len(mail.outbox) == 2


@pytest.mark.django_db
def test_failed_email_can_be_sent_later(northstar, admin_user, billable, django_capture_on_commit_callbacks):
    with mock.patch('billing.services.send_mail', side_effect=ConnectionRefusedError('down')):
        with django_capture_on_commit_callbacks(execute=True):
            run_billing(northstar, OCTOBER, admin_user)
    email = InvoiceEmail.objects.first()

    assert send_invoice_email(email.id) is True

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
    assert (resp.data['emails_failed'], resp.data['status']) == (0, 'completed')
    assert len(mail.outbox) == 2


@pytest.mark.django_db(transaction=True)
def test_billing_run_reports_failed_emails(admin_client, billable):
    with mock.patch('billing.services.send_mail', side_effect=ConnectionRefusedError('down')):
        resp = admin_client.post('/api/billing/runs/', {'month': '2026-10'})

    assert resp.status_code == 201
    assert resp.data['invoices_generated'] == 1
    assert (resp.data['emails_failed'], resp.data['status']) == (2, 'completed_with_errors')
