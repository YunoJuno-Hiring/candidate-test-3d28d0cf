import datetime
from decimal import Decimal

import pytest
from django.contrib.auth.models import User

from accounts.models import Freelancer
from billing.services import run_billing
from contracts.models import Contract, TimesheetEntry


def _entry(contract, status, date=datetime.date(2026, 10, 1), **extra):
    return TimesheetEntry.objects.create(
        contract=contract, date=date, hours=Decimal('8.0'), status=status, **extra
    )


@pytest.fixture
def someone_elses_contract(northstar):
    user = User.objects.create_user(username='other@test.test', email='other@test.test', password='x')
    other = Freelancer.objects.create(user=user, name='Other Freelancer')
    return Contract.objects.create(
        company=northstar, freelancer=other, daily_rate=Decimal('900.00'),
        start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 12, 31),
    )


# --- freelancers can't decide their own entries ---

@pytest.mark.django_db
@pytest.mark.parametrize('body', [
    {'status': 'approved'},
    {'status': 'rejected', 'rejection_reason': 'x'},
    {'rejection_reason': 'x'},
    {'status': 'approved', 'hours': '8.0'},
])
def test_freelancer_cannot_approve_or_reject(freelancer_client, active_contract, body):
    entry = _entry(active_contract, 'submitted')

    resp = freelancer_client.patch(f'/api/timesheets/{entry.id}/', body)

    assert resp.status_code == 403
    entry.refresh_from_db()
    assert entry.status == 'submitted'


@pytest.mark.django_db
def test_self_approval_attempt_is_never_billed(freelancer_client, northstar, admin_user, active_contract):
    entry = _entry(active_contract, 'submitted')
    freelancer_client.patch(f'/api/timesheets/{entry.id}/', {'status': 'approved'})

    run = run_billing(northstar, datetime.date(2026, 10, 1), admin_user)

    assert run.invoices_generated == 0


# --- freelancers can correct their own undecided hours ---

@pytest.mark.django_db
@pytest.mark.parametrize('entry_status', ['draft', 'submitted'])
def test_freelancer_can_correct_undecided_entry(freelancer_client, active_contract, entry_status):
    entry = _entry(active_contract, entry_status)

    resp = freelancer_client.patch(f'/api/timesheets/{entry.id}/', {'hours': '7.5', 'date': '2026-10-02'})

    assert resp.status_code == 200
    entry.refresh_from_db()
    assert (entry.hours, entry.date, entry.status) == (Decimal('7.50'), datetime.date(2026, 10, 2), entry_status)


@pytest.mark.django_db
@pytest.mark.parametrize('entry_status', ['approved', 'rejected'])
def test_freelancer_cannot_edit_decided_entry(freelancer_client, active_contract, entry_status):
    # Otherwise approved hours could be changed after the admin signed them off.
    entry = _entry(active_contract, entry_status, rejection_reason='x' if entry_status == 'rejected' else None)

    resp = freelancer_client.patch(f'/api/timesheets/{entry.id}/', {'hours': '12.0'})

    assert resp.status_code == 409
    entry.refresh_from_db()
    assert entry.hours == Decimal('8.00')


# --- nobody can move an entry to another contract ---

@pytest.mark.django_db
def test_freelancer_cannot_move_entry_to_someone_elses_contract(freelancer_client, active_contract, someone_elses_contract):
    entry = _entry(active_contract, 'submitted')

    resp = freelancer_client.patch(f'/api/timesheets/{entry.id}/', {'contract': someone_elses_contract.id})

    assert resp.status_code == 400
    assert 'contract' in resp.data
    entry.refresh_from_db()
    assert entry.contract == active_contract


@pytest.mark.django_db
def test_admin_cannot_move_entry_to_another_company(admin_client, active_contract, other_contract):
    entry = _entry(active_contract, 'submitted')

    resp = admin_client.patch(f'/api/timesheets/{entry.id}/', {'status': 'approved', 'contract': other_contract.id})

    assert resp.status_code == 400
    assert 'contract' in resp.data
    entry.refresh_from_db()
    assert (entry.contract, entry.status) == (active_contract, 'submitted')


@pytest.mark.django_db
@pytest.mark.parametrize('body', [{'status': 'approved', 'hours': '20.0'}, {'status': 'approved', 'date': '2026-10-05'}])
def test_admin_cannot_change_the_freelancers_hours_or_date(admin_client, active_contract, body):
    entry = _entry(active_contract, 'submitted')

    resp = admin_client.patch(f'/api/timesheets/{entry.id}/', body)

    assert resp.status_code == 400
    entry.refresh_from_db()
    assert (entry.hours, entry.status) == (Decimal('8.00'), 'submitted')


# --- admin decision rules ---

@pytest.mark.django_db
def test_admin_cannot_approve_a_draft(admin_client, active_contract):
    entry = _entry(active_contract, 'draft')

    resp = admin_client.patch(f'/api/timesheets/{entry.id}/', {'status': 'approved'})

    assert resp.status_code == 409
    entry.refresh_from_db()
    assert entry.status == 'draft'


@pytest.mark.django_db
@pytest.mark.parametrize('new_status', ['draft', 'submitted', 'paid', ''])
def test_admin_can_only_approve_or_reject(admin_client, active_contract, new_status):
    entry = _entry(active_contract, 'submitted')

    resp = admin_client.patch(f'/api/timesheets/{entry.id}/', {'status': new_status})

    assert resp.status_code == 400
    assert 'status' in resp.data


@pytest.mark.django_db
def test_admin_can_change_decision_before_invoicing(admin_client, active_contract):
    entry = _entry(active_contract, 'approved')

    resp = admin_client.patch(f'/api/timesheets/{entry.id}/', {'status': 'rejected', 'rejection_reason': 'Duplicate.'})

    assert resp.status_code == 200
    assert (resp.data['status'], resp.data['rejection_reason']) == ('rejected', 'Duplicate.')


@pytest.mark.django_db
def test_approving_clears_an_old_rejection_reason(admin_client, active_contract):
    entry = _entry(active_contract, 'rejected', rejection_reason='Missing details.')

    resp = admin_client.patch(f'/api/timesheets/{entry.id}/', {'status': 'approved', 'rejection_reason': 'stale'})

    assert resp.status_code == 200
    entry.refresh_from_db()
    assert (entry.status, entry.rejection_reason) == ('approved', None)
