import datetime
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.utils import timezone

from accounts.models import Freelancer
from billing.models import BillingRun, Invoice, InvoiceEmail, InvoiceLine
from billing.services import line_amount, parse_period, run_billing
from contracts.models import Contract, TimesheetEntry

OCTOBER = datetime.date(2026, 10, 1)
NOVEMBER = datetime.date(2026, 11, 1)


def _entry(contract, date, status='approved', hours='8.0'):
    return TimesheetEntry.objects.create(
        contract=contract, date=date, hours=Decimal(hours), status=status
    )


# --- pricing ---

def test_line_amount_is_pro_rata_on_an_8_hour_day():
    assert line_amount(Decimal('600.00'), Decimal('8.0')) == Decimal('600.00')
    assert line_amount(Decimal('600.00'), Decimal('7.5')) == Decimal('562.50')


def test_line_amount_rounds_to_the_penny():
    # 555 × 7.5 / 8 = 520.3125
    assert line_amount(Decimal('555.00'), Decimal('7.5')) == Decimal('520.31')


def test_line_amount_rounds_half_up():
    # 600.08 × 0.5 / 8 = 37.505: half-up gives .51, Decimal's default half-even .50
    assert line_amount(Decimal('600.08'), Decimal('0.5')) == Decimal('37.51')


# --- period parsing ---

def test_parse_period_returns_first_of_month():
    assert parse_period('2026-10') == OCTOBER


@pytest.mark.parametrize('value', ['', '2026-13', '2026/10', 'October', '2026-10-01', '2026-1', '0999-10', None])
def test_parse_period_rejects_malformed_values(value):
    with pytest.raises(ValueError):
        parse_period(value)


def test_parse_period_allows_current_month():
    today = timezone.localdate()
    assert parse_period(f'{today:%Y-%m}') == today.replace(day=1)


def test_parse_period_rejects_future_month():
    next_month = (timezone.localdate().replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
    with pytest.raises(ValueError):
        parse_period(f'{next_month:%Y-%m}')


# --- what gets billed ---

@pytest.mark.django_db
def test_run_creates_one_invoice_per_freelancer_with_a_line_per_entry(northstar, admin_user, active_contract):
    _entry(active_contract, datetime.date(2026, 10, 1))
    _entry(active_contract, datetime.date(2026, 10, 2), hours='7.5')

    run = run_billing(northstar, OCTOBER, admin_user)

    assert run.invoices_generated == 1
    invoice = Invoice.objects.get()
    assert invoice.run == run
    assert invoice.company == northstar
    assert invoice.freelancer == active_contract.freelancer
    assert invoice.period == OCTOBER
    assert [line.amount for line in invoice.lines.all()] == [Decimal('600.00'), Decimal('562.50')]
    assert invoice.total == Decimal('1162.50')


@pytest.mark.django_db
def test_invoice_number_embeds_period_and_id(northstar, admin_user, active_contract):
    _entry(active_contract, datetime.date(2026, 10, 1))
    run_billing(northstar, OCTOBER, admin_user)

    invoice = Invoice.objects.get()
    assert invoice.number == f'INV-2026-10-{invoice.pk:06d}'


@pytest.mark.django_db
def test_lines_store_what_was_billed(northstar, admin_user, active_contract):
    _entry(active_contract, datetime.date(2026, 10, 1), hours='7.5')
    run_billing(northstar, OCTOBER, admin_user)

    # Changing the contract rate afterwards must not change the issued invoice.
    active_contract.daily_rate = Decimal('900.00')
    active_contract.save()

    line = InvoiceLine.objects.get()
    assert (line.date, line.hours, line.daily_rate, line.amount) == (
        datetime.date(2026, 10, 1), Decimal('7.50'), Decimal('600.00'), Decimal('562.50')
    )
    assert line.invoice.total == Decimal('562.50')


@pytest.mark.django_db
@pytest.mark.parametrize('status', ['draft', 'submitted', 'rejected'])
def test_only_approved_entries_are_billed(northstar, admin_user, active_contract, status):
    _entry(active_contract, datetime.date(2026, 10, 1), status=status)

    run = run_billing(northstar, OCTOBER, admin_user)

    assert run.invoices_generated == 0
    assert not Invoice.objects.exists()


@pytest.mark.django_db
def test_entries_after_the_period_are_not_billed(northstar, admin_user, active_contract):
    _entry(active_contract, datetime.date(2026, 10, 31))
    _entry(active_contract, datetime.date(2026, 11, 1))

    run_billing(northstar, OCTOBER, admin_user)

    assert [line.date for line in InvoiceLine.objects.all()] == [datetime.date(2026, 10, 31)]


@pytest.mark.django_db
def test_unbilled_entries_from_earlier_months_are_included(northstar, admin_user, active_contract):
    # The period is the billing month, not the work month.
    _entry(active_contract, datetime.date(2026, 9, 15))
    _entry(active_contract, datetime.date(2026, 10, 15))

    run_billing(northstar, OCTOBER, admin_user)

    assert InvoiceLine.objects.filter(invoice__period=OCTOBER).count() == 2


@pytest.mark.django_db
def test_other_companies_entries_are_not_billed(northstar, admin_user, active_contract, other_contract):
    _entry(active_contract, datetime.date(2026, 10, 1))
    _entry(other_contract, datetime.date(2026, 10, 2))

    run_billing(northstar, OCTOBER, admin_user)

    assert list(Invoice.objects.values_list('company', flat=True)) == [northstar.id]
    assert InvoiceLine.objects.get().timesheet_entry.contract == active_contract


@pytest.mark.django_db
def test_closed_contracts_are_still_billed(northstar, admin_user, active_contract):
    active_contract.status = Contract.STATUS_CLOSED
    active_contract.save()
    _entry(active_contract, datetime.date(2026, 10, 1))

    run = run_billing(northstar, OCTOBER, admin_user)

    assert run.invoices_generated == 1


@pytest.mark.django_db
def test_each_freelancer_gets_their_own_invoice(northstar, admin_user, active_contract):
    other_user = User.objects.create_user(username='sam@test.test', email='sam@test.test', password='x')
    sam = Freelancer.objects.create(user=other_user, name='Sam')
    sam_contract = Contract.objects.create(
        company=northstar, freelancer=sam, daily_rate=Decimal('500.00'),
        start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 12, 31),
    )
    _entry(active_contract, datetime.date(2026, 10, 1))
    _entry(sam_contract, datetime.date(2026, 10, 1))

    run = run_billing(northstar, OCTOBER, admin_user)

    assert run.invoices_generated == 2
    assert {i.freelancer for i in Invoice.objects.all()} == {active_contract.freelancer, sam}


# --- idempotency ---

@pytest.mark.django_db
def test_rerunning_a_billed_month_creates_nothing(northstar, admin_user, active_contract):
    _entry(active_contract, datetime.date(2026, 10, 1))
    run_billing(northstar, OCTOBER, admin_user)

    rerun = run_billing(northstar, OCTOBER, admin_user)

    assert rerun.invoices_generated == 0
    assert Invoice.objects.count() == 1
    assert InvoiceLine.objects.count() == 1
    # The re-run is still recorded in the audit trail.
    assert BillingRun.objects.count() == 2


@pytest.mark.django_db
def test_late_approval_rolls_into_next_months_invoice(northstar, admin_user, active_contract):
    _entry(active_contract, datetime.date(2026, 10, 1))
    run_billing(northstar, OCTOBER, admin_user)
    late = _entry(active_contract, datetime.date(2026, 10, 20))  # approved after October was billed

    assert run_billing(northstar, OCTOBER, admin_user).invoices_generated == 0
    assert run_billing(northstar, NOVEMBER, admin_user).invoices_generated == 1

    line = InvoiceLine.objects.get(timesheet_entry=late)
    assert line.invoice.period == NOVEMBER
    assert line.date == datetime.date(2026, 10, 20)  # keeps its work date


@pytest.mark.django_db
def test_an_entry_cannot_be_on_two_invoice_lines(northstar, admin_user, active_contract):
    entry = _entry(active_contract, datetime.date(2026, 10, 1))
    run_billing(northstar, OCTOBER, admin_user)
    second = Invoice.objects.create(
        run=BillingRun.objects.get(), company=northstar, freelancer=active_contract.freelancer, period=NOVEMBER
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        InvoiceLine.objects.create(
            invoice=second, timesheet_entry=entry, date=entry.date,
            hours=entry.hours, daily_rate=Decimal('600.00'), amount=Decimal('600.00'),
        )


@pytest.mark.django_db
def test_only_one_invoice_per_company_freelancer_and_period(northstar, admin_user, active_contract):
    _entry(active_contract, datetime.date(2026, 10, 1))
    run_billing(northstar, OCTOBER, admin_user)

    with pytest.raises(IntegrityError), transaction.atomic():
        Invoice.objects.create(
            run=BillingRun.objects.get(), company=northstar,
            freelancer=active_contract.freelancer, period=OCTOBER,
        )


# --- audit trail and record protection ---

@pytest.mark.django_db
def test_run_records_who_billed_which_month(northstar, admin_user, active_contract):
    _entry(active_contract, datetime.date(2026, 10, 1))

    run = run_billing(northstar, OCTOBER, admin_user)

    assert (run.company, run.period, run.run_by, run.invoices_generated) == (northstar, OCTOBER, admin_user, 1)


@pytest.mark.django_db
def test_billed_entries_and_their_company_cannot_be_deleted(northstar, admin_user, active_contract):
    entry = _entry(active_contract, datetime.date(2026, 10, 1))
    run_billing(northstar, OCTOBER, admin_user)

    with pytest.raises(ProtectedError):
        entry.delete()
    with pytest.raises(ProtectedError):
        northstar.delete()


# --- outbox rows ---

@pytest.mark.django_db
def test_invoice_queues_an_email_for_company_and_freelancer(northstar, admin_user, active_contract):
    _entry(active_contract, datetime.date(2026, 10, 1))
    run_billing(northstar, OCTOBER, admin_user)

    emails = InvoiceEmail.objects.all()
    assert [e.recipient for e in emails] == ['billing@northstar.test', 'freelancer@test.test']
    assert all(e.status == InvoiceEmail.STATUS_PENDING and e.attempts == 0 for e in emails)


@pytest.mark.django_db
def test_shared_address_gets_a_single_email(northstar, admin_user, active_contract):
    northstar.billing_email = 'Freelancer@test.test'
    northstar.save()
    _entry(active_contract, datetime.date(2026, 10, 1))

    run_billing(northstar, OCTOBER, admin_user)

    assert InvoiceEmail.objects.count() == 1


# --- concurrency ---

@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('periods', [(OCTOBER, OCTOBER), (OCTOBER, NOVEMBER)], ids=['same-month', 'different-months'])
def test_concurrent_runs_bill_each_entry_once(northstar, admin_user, active_contract, periods):
    # Two real transactions racing on Postgres. The company lock makes the
    # second run wait, then see the first run's invoice and lines, so it
    # creates nothing. Without the lock, two runs for different months both
    # select the same entry and the second fails on the one-to-one constraint:
    # still no double billing, but an error for the user.
    import threading

    from django.db import connection

    _entry(active_contract, datetime.date(2026, 10, 1))
    barrier = threading.Barrier(2)
    results, errors = [], []

    def bill(period):
        try:
            barrier.wait()
            results.append(run_billing(northstar, period, admin_user).invoices_generated)
        except Exception as exc:  # surfaced by the assertion below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=bill, args=(period,)) for period in periods]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert sorted(results) == [0, 1]
    assert Invoice.objects.count() == 1
    assert InvoiceLine.objects.count() == 1
