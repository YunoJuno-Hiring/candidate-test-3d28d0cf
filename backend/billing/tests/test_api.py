import datetime
from decimal import Decimal

import pytest
from django.utils import timezone

from billing.models import BillingRun, Invoice
from billing.services import run_billing
from contracts.models import TimesheetEntry

OCTOBER = datetime.date(2026, 10, 1)


def _entry(contract, date, hours='8.0'):
    return TimesheetEntry.objects.create(
        contract=contract, date=date, hours=Decimal(hours), status='approved'
    )


def _next_month():
    return (timezone.localdate().replace(day=28) + datetime.timedelta(days=4)).strftime('%Y-%m')


# --- POST /api/billing/runs/ ---

@pytest.mark.django_db
def test_billing_run_requires_auth(api_client):
    resp = api_client.post('/api/billing/runs/', {'month': '2026-10'})
    assert resp.status_code == 401


@pytest.mark.django_db
def test_freelancer_cannot_run_billing(freelancer_client, active_contract):
    _entry(active_contract, datetime.date(2026, 10, 1))

    resp = freelancer_client.post('/api/billing/runs/', {'month': '2026-10'})

    assert resp.status_code == 403
    assert not BillingRun.objects.exists()


@pytest.mark.django_db
def test_admin_runs_billing(admin_client, admin_user, active_contract):
    _entry(active_contract, datetime.date(2026, 10, 1))

    resp = admin_client.post('/api/billing/runs/', {'month': '2026-10'})

    assert resp.status_code == 201
    run = BillingRun.objects.get()
    assert resp.data == {'run_id': run.id, 'invoices_generated': 1, 'emails_failed': 0, 'status': 'completed'}
    assert run.run_by == admin_user


@pytest.mark.django_db
def test_rerun_returns_zero_invoices(admin_client, active_contract):
    _entry(active_contract, datetime.date(2026, 10, 1))
    admin_client.post('/api/billing/runs/', {'month': '2026-10'})

    resp = admin_client.post('/api/billing/runs/', {'month': '2026-10'})

    assert resp.status_code == 201
    assert resp.data['invoices_generated'] == 0
    assert Invoice.objects.count() == 1


@pytest.mark.django_db
def test_admin_only_bills_own_company(admin_client, northstar, active_contract, other_contract):
    _entry(active_contract, datetime.date(2026, 10, 1))
    _entry(other_contract, datetime.date(2026, 10, 1))

    admin_client.post('/api/billing/runs/', {'month': '2026-10'})

    assert list(Invoice.objects.values_list('company', flat=True)) == [northstar.id]


@pytest.mark.django_db
@pytest.mark.parametrize('body', [{}, {'month': ''}, {'month': '2026-13'}, {'month': 'October'}])
def test_billing_run_rejects_invalid_month(admin_client, body):
    resp = admin_client.post('/api/billing/runs/', body)

    assert resp.status_code == 400
    assert 'month' in resp.data
    assert not BillingRun.objects.exists()


@pytest.mark.django_db
def test_billing_run_rejects_future_month(admin_client):
    resp = admin_client.post('/api/billing/runs/', {'month': _next_month()})

    assert resp.status_code == 400
    assert 'month' in resp.data


# --- GET /api/invoices/ ---

@pytest.fixture
def october_invoice(northstar, admin_user, active_contract):
    _entry(active_contract, datetime.date(2026, 10, 1))
    _entry(active_contract, datetime.date(2026, 10, 2), hours='7.5')
    run_billing(northstar, OCTOBER, admin_user)
    return Invoice.objects.get()


@pytest.fixture
def meridian_invoice(meridian, other_contract):
    from django.contrib.auth.models import User
    from accounts.models import CompanyAdmin

    meridian_admin = User.objects.create_user(username='m@test.test', email='m@test.test', password='x')
    CompanyAdmin.objects.create(user=meridian_admin, company=meridian)
    _entry(other_contract, datetime.date(2026, 10, 1))
    run_billing(meridian, OCTOBER, meridian_admin)
    return Invoice.objects.get(company=meridian)


@pytest.mark.django_db
def test_invoice_list_requires_auth(api_client):
    resp = api_client.get('/api/invoices/?month=2026-10')
    assert resp.status_code == 401


@pytest.mark.django_db
def test_admin_lists_invoices_for_month(admin_client, october_invoice):
    resp = admin_client.get('/api/invoices/?month=2026-10')

    assert resp.status_code == 200
    assert len(resp.data) == 1
    invoice = resp.data[0]
    assert invoice['id'] == october_invoice.id
    assert invoice['number'] == october_invoice.number
    assert invoice['period'] == '2026-10'
    assert invoice['company']['name'] == 'NorthStar Consulting'
    assert invoice['freelancer']['name'] == 'Test Freelancer'
    assert invoice['total'] == '1162.50'
    assert invoice['currency'] == 'GBP'
    assert invoice['delivery_status'] == 'pending'


@pytest.mark.django_db
def test_invoice_list_filters_by_month(admin_client, october_invoice):
    resp = admin_client.get('/api/invoices/?month=2026-09')

    assert resp.status_code == 200
    assert resp.data == []


@pytest.mark.django_db
def test_invoice_list_future_month_is_empty_not_an_error(admin_client, october_invoice):
    resp = admin_client.get(f'/api/invoices/?month={_next_month()}')

    assert resp.status_code == 200
    assert resp.data == []


@pytest.mark.django_db
def test_invoice_list_rejects_invalid_month(admin_client):
    resp = admin_client.get('/api/invoices/?month=2026-13')

    assert resp.status_code == 400
    assert 'month' in resp.data


@pytest.mark.django_db
def test_admin_does_not_see_other_company_invoices(admin_client, october_invoice, meridian_invoice):
    resp = admin_client.get('/api/invoices/?month=2026-10')

    assert [i['id'] for i in resp.data] == [october_invoice.id]


@pytest.mark.django_db
def test_freelancer_sees_own_invoices(freelancer_client, october_invoice, meridian_invoice):
    # The fixture freelancer works for both companies, so sees both invoices.
    resp = freelancer_client.get('/api/invoices/?month=2026-10')

    assert resp.status_code == 200
    assert {i['id'] for i in resp.data} == {october_invoice.id, meridian_invoice.id}


# --- GET /api/invoices/<id>/ ---

@pytest.mark.django_db
def test_invoice_detail_requires_auth(api_client, october_invoice):
    resp = api_client.get(f'/api/invoices/{october_invoice.id}/')
    assert resp.status_code == 401


@pytest.mark.django_db
def test_admin_invoice_detail(admin_client, october_invoice):
    resp = admin_client.get(f'/api/invoices/{october_invoice.id}/')

    assert resp.status_code == 200
    data = resp.data
    assert data['number'] == october_invoice.number
    assert data['total'] == '1162.50'
    assert [(line['date'], line['hours'], line['daily_rate'], line['amount']) for line in data['lines']] == [
        ('2026-10-01', '8.00', '600.00', '600.00'),
        ('2026-10-02', '7.50', '600.00', '562.50'),
    ]
    assert [(e['recipient'], e['status']) for e in data['emails']] == [
        ('billing@northstar.test', 'pending'),
        ('freelancer@test.test', 'pending'),
    ]


@pytest.mark.django_db
def test_admin_cannot_see_other_company_invoice(admin_client, meridian_invoice):
    resp = admin_client.get(f'/api/invoices/{meridian_invoice.id}/')
    assert resp.status_code == 404


@pytest.mark.django_db
def test_freelancer_invoice_detail(freelancer_client, october_invoice):
    resp = freelancer_client.get(f'/api/invoices/{october_invoice.id}/')
    assert resp.status_code == 200


@pytest.mark.django_db
def test_freelancer_cannot_see_another_freelancers_invoice(api_client, october_invoice):
    from django.contrib.auth.models import User
    from rest_framework.authtoken.models import Token
    from accounts.models import Freelancer

    other = User.objects.create_user(username='other@test.test', email='other@test.test', password='x')
    Freelancer.objects.create(user=other, name='Other')
    token, _ = Token.objects.get_or_create(user=other)
    api_client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    resp = api_client.get(f'/api/invoices/{october_invoice.id}/')
    assert resp.status_code == 404


@pytest.mark.django_db
def test_invoice_detail_not_found(admin_client):
    resp = admin_client.get('/api/invoices/99999/')
    assert resp.status_code == 404


@pytest.mark.django_db
def test_invoice_with_no_recipients_says_so(admin_client, northstar, admin_user, active_contract, freelancer_user):
    # Blank addresses are allowed by the database; no email can be sent, and
    # the invoice must not look like it is just waiting to go out.
    northstar.billing_email = ''
    northstar.save()
    freelancer_user.email = ''
    freelancer_user.save()
    _entry(active_contract, datetime.date(2026, 10, 1))
    run_billing(northstar, OCTOBER, admin_user)
    invoice = Invoice.objects.get()

    assert not invoice.emails.exists()
    assert admin_client.get('/api/invoices/?month=2026-10').data[0]['delivery_status'] == 'no_recipients'
    assert admin_client.get(f'/api/invoices/{invoice.id}/').data['delivery_status'] == 'no_recipients'


@pytest.fixture
def failed_delivery(october_invoice):
    october_invoice.emails.update(status='failed', attempts=1, last_error='SMTPRecipientsRefused: 550 mail.internal.example rejected')
    return october_invoice


@pytest.mark.django_db
def test_admin_sees_email_delivery_errors(admin_client, failed_delivery):
    emails = admin_client.get(f'/api/invoices/{failed_delivery.id}/').data['emails']

    assert all(e['last_error'].startswith('SMTPRecipientsRefused') for e in emails)


@pytest.mark.django_db
def test_freelancer_does_not_see_email_delivery_errors(freelancer_client, failed_delivery):
    emails = freelancer_client.get(f'/api/invoices/{failed_delivery.id}/').data['emails']

    assert emails and all('last_error' not in e for e in emails)
    assert all(e['status'] == 'failed' for e in emails)  # the status itself is still visible
