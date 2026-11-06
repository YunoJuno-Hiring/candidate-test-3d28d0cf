import datetime
from decimal import Decimal

import pytest

from billing.models import InvoiceLine
from billing.services import run_billing
from contracts.models import TimesheetEntry

OCTOBER = datetime.date(2026, 10, 1)


@pytest.fixture
def invoiced_entry(northstar, admin_user, active_contract):
    entry = TimesheetEntry.objects.create(
        contract=active_contract, date=datetime.date(2026, 10, 1), hours=Decimal('7.5'), status='approved'
    )
    run_billing(northstar, OCTOBER, admin_user)
    return entry


def _assert_unchanged(entry):
    entry.refresh_from_db()
    assert (entry.status, entry.hours, entry.rejection_reason) == ('approved', Decimal('7.50'), None)
    line = InvoiceLine.objects.get(timesheet_entry=entry)
    assert (line.hours, line.amount) == (Decimal('7.50'), Decimal('562.50'))


@pytest.mark.django_db
def test_admin_cannot_reject_an_invoiced_entry(admin_client, invoiced_entry):
    resp = admin_client.patch(f'/api/timesheets/{invoiced_entry.id}/', {
        'status': 'rejected',
        'rejection_reason': 'Not agreed.',
    })

    assert resp.status_code == 409
    assert resp.data == {'detail': 'This entry has been invoiced and can no longer be changed.'}
    _assert_unchanged(invoiced_entry)


@pytest.mark.django_db
def test_freelancer_cannot_change_hours_of_an_invoiced_entry(freelancer_client, invoiced_entry):
    resp = freelancer_client.patch(f'/api/timesheets/{invoiced_entry.id}/', {'hours': '10.0'})

    assert resp.status_code == 409
    _assert_unchanged(invoiced_entry)


@pytest.mark.django_db
def test_any_change_to_an_invoiced_entry_is_refused(admin_client, invoiced_entry):
    # Even a request that would leave the status as it is: the entry is closed.
    resp = admin_client.patch(f'/api/timesheets/{invoiced_entry.id}/', {'status': 'approved'})

    assert resp.status_code == 409


@pytest.mark.django_db
def test_entries_not_yet_invoiced_can_still_be_changed(admin_client, active_contract):
    entry = TimesheetEntry.objects.create(
        contract=active_contract, date=datetime.date(2026, 10, 1), hours=Decimal('8.0'), status='approved'
    )

    resp = admin_client.patch(f'/api/timesheets/{entry.id}/', {
        'status': 'rejected',
        'rejection_reason': 'Logged twice.',
    })

    assert resp.status_code == 200
    assert resp.data['status'] == 'rejected'


@pytest.mark.django_db
def test_other_company_invoiced_entry_is_still_404(admin_client, meridian, admin_user, other_contract):
    # Scoping is checked before the invoiced check, so nothing leaks about
    # other companies' entries.
    entry = TimesheetEntry.objects.create(
        contract=other_contract, date=datetime.date(2026, 10, 1), hours=Decimal('8.0'), status='approved'
    )
    run_billing(meridian, OCTOBER, admin_user)

    resp = admin_client.patch(f'/api/timesheets/{entry.id}/', {'status': 'rejected', 'rejection_reason': 'x'})

    assert resp.status_code == 404
