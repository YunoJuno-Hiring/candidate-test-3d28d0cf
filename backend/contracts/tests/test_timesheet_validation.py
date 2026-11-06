import datetime
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from accounts.models import Freelancer
from contracts.models import Contract, TimesheetEntry

TODAY = timezone.localdate()
YESTERDAY = TODAY - datetime.timedelta(days=1)


def _contract(company, freelancer, status='active', start_days_ago=90, end_days_ahead=90):
    return Contract.objects.create(
        company=company, freelancer=freelancer, daily_rate=Decimal('600.00'), status=status,
        start_date=TODAY - datetime.timedelta(days=start_days_ago),
        end_date=TODAY + datetime.timedelta(days=end_days_ahead),
    )


@pytest.fixture
def contract(northstar, freelancer_user):
    return _contract(northstar, freelancer_user.freelancer)


@pytest.fixture
def second_contract(meridian, freelancer_user):
    # Same freelancer, another company: hours on the same day add up.
    return _contract(meridian, freelancer_user.freelancer)


def _log(client, contract, date=YESTERDAY, hours='8.0', **extra):
    return client.post('/api/timesheets/', {'contract': contract.id, 'date': date.isoformat(), 'hours': hours, **extra})


# --- hours ---

@pytest.mark.django_db
@pytest.mark.parametrize('hours', ['0', '-8', '24.5', '999.99', '7.3'])
def test_rejects_invalid_hours(freelancer_client, contract, hours):
    resp = _log(freelancer_client, contract, hours=hours)

    assert resp.status_code == 400
    assert 'hours' in resp.data
    assert not TimesheetEntry.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize('hours', ['0.5', '7.5', '24'])
def test_accepts_valid_hours(freelancer_client, contract, hours):
    assert _log(freelancer_client, contract, hours=hours).status_code == 201


@pytest.mark.django_db
def test_editing_hours_is_validated_too(freelancer_client, contract):
    entry_id = _log(freelancer_client, contract).data['id']

    resp = freelancer_client.patch(f'/api/timesheets/{entry_id}/', {'hours': '-1'})

    assert resp.status_code == 400
    assert TimesheetEntry.objects.get(pk=entry_id).hours == Decimal('8.00')


# --- daily total across contracts ---

@pytest.mark.django_db
def test_daily_total_across_contracts_cannot_exceed_24(freelancer_client, contract, second_contract):
    assert _log(freelancer_client, contract, hours='16').status_code == 201

    resp = _log(freelancer_client, second_contract, hours='9')

    assert resp.status_code == 400
    assert 'hours' in resp.data
    assert _log(freelancer_client, second_contract, hours='8').status_code == 201


@pytest.mark.django_db
def test_editing_an_entry_does_not_count_its_old_hours(freelancer_client, contract):
    entry_id = _log(freelancer_client, contract, hours='16').data['id']

    resp = freelancer_client.patch(f'/api/timesheets/{entry_id}/', {'hours': '20'})

    assert resp.status_code == 200


# --- dates ---

@pytest.mark.django_db
def test_rejects_future_dates(freelancer_client, contract):
    resp = _log(freelancer_client, contract, date=TODAY + datetime.timedelta(days=1))

    assert resp.status_code == 400
    assert 'date' in resp.data


@pytest.mark.django_db
def test_accepts_today(freelancer_client, contract):
    assert _log(freelancer_client, contract, date=TODAY).status_code == 201


@pytest.mark.django_db
def test_rejects_dates_before_the_contract_starts(freelancer_client, northstar, freelancer_user):
    contract = _contract(northstar, freelancer_user.freelancer, start_days_ago=10)

    resp = _log(freelancer_client, contract, date=TODAY - datetime.timedelta(days=11))

    assert resp.status_code == 400
    assert 'date' in resp.data


@pytest.mark.django_db
def test_rejects_dates_after_the_contract_ends(freelancer_client, northstar, freelancer_user):
    contract = _contract(northstar, freelancer_user.freelancer, start_days_ago=30, end_days_ahead=-5)

    resp = _log(freelancer_client, contract, date=YESTERDAY)

    assert resp.status_code == 400
    assert 'date' in resp.data


@pytest.mark.django_db
def test_editing_the_date_is_validated_too(freelancer_client, contract):
    entry_id = _log(freelancer_client, contract).data['id']

    resp = freelancer_client.patch(f'/api/timesheets/{entry_id}/', {'date': (TODAY + datetime.timedelta(days=3)).isoformat()})

    assert resp.status_code == 400
    assert TimesheetEntry.objects.get(pk=entry_id).date == YESTERDAY


# --- closed contracts ---

@pytest.mark.django_db
def test_cannot_log_hours_on_a_closed_contract(freelancer_client, northstar, freelancer_user):
    contract = _contract(northstar, freelancer_user.freelancer, status='closed')

    resp = _log(freelancer_client, contract)

    assert resp.status_code == 400
    assert 'contract' in resp.data


@pytest.mark.django_db
def test_cannot_edit_hours_once_the_contract_is_closed(freelancer_client, contract):
    entry_id = _log(freelancer_client, contract).data['id']
    contract.status = Contract.STATUS_CLOSED
    contract.save()

    resp = freelancer_client.patch(f'/api/timesheets/{entry_id}/', {'hours': '7.5'})

    assert resp.status_code == 400


@pytest.mark.django_db
def test_admin_can_still_decide_entries_on_a_closed_contract(admin_client, contract):
    # The work was submitted while the contract was active; closing it later
    # mustn't leave the entry stuck. (Created directly: admin_client and
    # freelancer_client share one APIClient, so they can't both be used here.)
    entry = TimesheetEntry.objects.create(contract=contract, date=YESTERDAY, hours=Decimal('8.0'), status='submitted')
    contract.status = Contract.STATUS_CLOSED
    contract.save()

    resp = admin_client.patch(f'/api/timesheets/{entry.id}/', {'status': 'approved'})

    assert resp.status_code == 200


# --- only the expected fields, and nothing about other people's contracts ---

@pytest.mark.django_db
@pytest.mark.parametrize('extra', [{'status': 'approved'}, {'rejection_reason': 'x'}])
def test_create_refuses_fields_the_freelancer_does_not_own(freelancer_client, contract, extra):
    resp = _log(freelancer_client, contract, **extra)

    assert resp.status_code == 400
    assert not TimesheetEntry.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize('contract_value', ['abc', ''])
def test_create_rejects_a_malformed_contract(freelancer_client, contract_value):
    resp = freelancer_client.post('/api/timesheets/', {'contract': contract_value, 'date': YESTERDAY.isoformat(), 'hours': '8'})

    assert resp.status_code == 400
    assert resp.data == {'contract': 'Invalid contract.'}


@pytest.mark.django_db
def test_errors_reveal_nothing_about_someone_elses_contract(freelancer_client, northstar):
    user = User.objects.create_user(username='other@test.test', email='other@test.test', password='x')
    other = Freelancer.objects.create(user=user, name='Other')
    closed = _contract(northstar, other, status='closed')

    resp = _log(freelancer_client, closed)

    assert resp.status_code == 400
    assert resp.data == {'contract': 'Invalid contract.'}
