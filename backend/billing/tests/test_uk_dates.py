import datetime
from unittest import mock

import pytest

from billing.services import parse_period

# 23:30 UTC on 31 May is already 00:30 on 1 June in the UK (BST).
JUST_AFTER_UK_MIDNIGHT = datetime.datetime(2026, 5, 31, 23, 30, tzinfo=datetime.timezone.utc)


@pytest.fixture
def uk_midnight():
    with mock.patch('django.utils.timezone.now', return_value=JUST_AFTER_UK_MIDNIGHT):
        yield


def test_month_that_just_ended_in_the_uk_can_be_billed(uk_midnight):
    # In the UK May has ended; in UTC it is still 31 May, so May would be
    # refused as a month that hasn't ended.
    assert parse_period('2026-05') == datetime.date(2026, 5, 1)


def test_new_uk_month_has_not_ended_yet(uk_midnight):
    with pytest.raises(ValueError):
        parse_period('2026-06')


@pytest.mark.django_db
def test_hours_for_the_new_uk_day_can_be_logged_just_after_midnight(freelancer_client, active_contract, uk_midnight):
    resp = freelancer_client.post('/api/timesheets/', {
        'contract': active_contract.id, 'date': '2026-06-01', 'hours': '8',
    })

    assert resp.status_code == 201


@pytest.mark.django_db
def test_tomorrow_in_the_uk_is_still_future(freelancer_client, active_contract, uk_midnight):
    resp = freelancer_client.post('/api/timesheets/', {
        'contract': active_contract.id, 'date': '2026-06-02', 'hours': '8',
    })

    assert resp.status_code == 400
    assert 'date' in resp.data

