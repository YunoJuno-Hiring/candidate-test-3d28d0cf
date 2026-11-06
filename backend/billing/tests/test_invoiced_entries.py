import datetime
import io
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


# --- the rule holds for every write path, not only the API ---

@pytest.mark.django_db
def test_model_refuses_changes_to_an_invoiced_entry(invoiced_entry):
    from contracts.models import InvoicedEntryError

    invoiced_entry.refresh_from_db()
    invoiced_entry.status = 'rejected'
    invoiced_entry.rejection_reason = 'Changed in the admin.'

    with pytest.raises(InvoicedEntryError):
        invoiced_entry.save()
    _assert_unchanged(invoiced_entry)


@pytest.mark.django_db
def test_saving_an_invoiced_entry_unchanged_is_allowed(invoiced_entry):
    invoiced_entry.refresh_from_db()
    invoiced_entry.save()  # no change, no error


@pytest.mark.django_db
def test_admin_shows_invoiced_entries_read_only(invoiced_entry, active_contract, rf, admin_user):
    from django.contrib.admin.sites import site

    model_admin = site._registry[TimesheetEntry]
    request = rf.get('/admin/')
    request.user = admin_user
    unbilled = TimesheetEntry.objects.create(
        contract=active_contract, date=datetime.date(2026, 10, 2), hours=Decimal('8.0'), status='submitted'
    )

    assert set(model_admin.get_readonly_fields(request, invoiced_entry)) >= {'status', 'hours', 'date', 'contract'}
    assert not model_admin.get_readonly_fields(request, unbilled)


@pytest.mark.django_db
def test_reseeding_after_billing_leaves_invoiced_entries_alone():
    # seed.py is "safe to run multiple times", and resets existing entries to
    # its own values. Run on another day those values differ (statuses depend
    # on the date): simulated here by changing an invoiced entry behind the
    # model's back. Re-seeding must neither crash nor rewrite it.
    from django.contrib.auth.models import User
    from django.core.management import call_command
    from django.utils import timezone

    from accounts.models import Company

    call_command('seed', stdout=io.StringIO())
    northstar = Company.objects.get(name='NorthStar Consulting')
    admin = User.objects.get(username='admin@northstar.test')
    run_billing(northstar, timezone.localdate().replace(day=1), admin)
    line = InvoiceLine.objects.first()
    TimesheetEntry.objects.filter(pk=line.timesheet_entry_id).update(hours=Decimal('1.0'))

    call_command('seed', stdout=io.StringIO())

    assert TimesheetEntry.objects.get(pk=line.timesheet_entry_id).hours == Decimal('1.00')
