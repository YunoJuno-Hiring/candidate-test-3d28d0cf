"""
Seed command — idempotent. Safe to run multiple times.

Produces:
  2 companies, 4 freelancers, 6 contracts, ~42 timesheet entries.
  Dates anchor to today so March and April are always the two complete
  prior calendar months relative to the seed run.

Status assignment rules (based on entry date):
  more than ~2 months old  → approved (with a few rejected)
  prior calendar month     → approved or submitted
  current/recent           → submitted or draft
"""
import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from accounts.models import Company, CompanyAdmin, Freelancer
from contracts.models import Contract, TimesheetEntry

SEED_PASSWORD = 'testpass123'


def _working_days(start: datetime.date, end: datetime.date) -> list[datetime.date]:
    day, result = start, []
    while day <= end:
        if day.weekday() < 5:
            result.append(day)
        day += datetime.timedelta(days=1)
    return result


class Command(BaseCommand):
    help = 'Load deterministic seed data (idempotent).'

    def handle(self, *args, **options):
        today = datetime.date.today()

        # Status threshold dates
        month_start = datetime.date(today.year, today.month, 1)
        prev_month_start = (month_start - datetime.timedelta(days=1)).replace(day=1)
        two_months_ago_start = (prev_month_start - datetime.timedelta(days=1)).replace(day=1)

        self.stdout.write('Seeding companies…')
        northstar, _ = Company.objects.get_or_create(
            name='NorthStar Consulting',
            defaults={'billing_email': 'billing@northstar.example.com'},
        )
        meridian, _ = Company.objects.get_or_create(
            name='Meridian Digital',
            defaults={'billing_email': 'accounts@meridiandigital.example.com'},
        )

        self.stdout.write('Seeding users and freelancers…')
        admin_user, created = User.objects.get_or_create(
            username='admin@northstar.test',
            defaults={'email': 'admin@northstar.test', 'is_staff': True},
        )
        if created:
            admin_user.set_password(SEED_PASSWORD)
            admin_user.save()
        CompanyAdmin.objects.get_or_create(user=admin_user, defaults={'company': northstar})

        def _make_freelancer(username, name):
            user, created = User.objects.get_or_create(
                username=username,
                defaults={'email': username},
            )
            if created:
                user.set_password(SEED_PASSWORD)
                user.save()
            freelancer, _ = Freelancer.objects.get_or_create(user=user, defaults={'name': name})
            return freelancer

        alex = _make_freelancer('alex@freelance.test', 'Alex Rivera')
        sam = _make_freelancer('sam@freelance.test', 'Sam Chen')
        jordan = _make_freelancer('jordan@freelance.test', 'Jordan Mitchell')
        taylor = _make_freelancer('taylor@freelance.test', 'Taylor Brooks')

        self.stdout.write('Seeding contracts…')
        y = today.year

        contract_specs = [
            (northstar, alex,   Decimal('600.00'), datetime.date(y, 1, 1),        datetime.date(y, 6, 30),        'active'),
            (northstar, sam,    Decimal('500.00'), datetime.date(y, 2, 1),        datetime.date(y, 7, 31),        'active'),
            (northstar, jordan, Decimal('450.00'), datetime.date(y - 1, 10, 1),   datetime.date(y, 3, 31),        'closed'),
            (meridian,  alex,   Decimal('700.00'), datetime.date(y, 1, 15),       datetime.date(y, 12, 31),       'active'),
            (meridian,  taylor, Decimal('400.00'), datetime.date(y, 3, 1),        datetime.date(y, 9, 30),        'active'),
            (meridian,  jordan, Decimal('800.00'), datetime.date(y - 1, 12, 1),   datetime.date(y, 4, 30),        'closed'),
        ]

        contracts = []
        for company, freelancer, rate, start, end, bstatus in contract_specs:
            contract, _ = Contract.objects.get_or_create(
                company=company,
                freelancer=freelancer,
                start_date=start,
                defaults={'daily_rate': rate, 'end_date': end, 'status': bstatus},
            )
            contracts.append(contract)

        self.stdout.write('Seeding timesheet entries…')

        # Window: Jan 1 of this year (or previous year if we're in Jan/Feb) up to yesterday
        if today.month > 2:
            window_start = datetime.date(today.year, 1, 1)
        else:
            window_start = datetime.date(today.year - 1, 11, 1)
        window_end = today - datetime.timedelta(days=1)

        entry_count = 0
        for contract in contracts:
            gen_start = max(contract.start_date, window_start)
            gen_end = min(contract.end_date, window_end)
            if gen_start > gen_end:
                continue

            all_days = _working_days(gen_start, gen_end)
            if not all_days:
                continue

            # Target ~7 entries per contract; sample evenly across the date range
            target = 7
            step = max(1, len(all_days) // target)
            selected = all_days[::step][:target]

            for i, day in enumerate(selected):
                if day < two_months_ago_start:
                    # January / February: mostly approved, a few rejected
                    entry_status = 'rejected' if i % 7 == 0 else 'approved'
                    reason = 'Hours logged on a non-working day.' if entry_status == 'rejected' else None
                elif day < prev_month_start:
                    # Two months ago (e.g. March): approved with a few still submitted
                    entry_status = 'submitted' if i % 5 == 0 else 'approved'
                    reason = None
                elif day < month_start:
                    # Last month (e.g. April): mix — half approved, half submitted, a few draft
                    if i % 4 == 0:
                        entry_status = 'draft'
                    elif i % 2 == 0:
                        entry_status = 'submitted'
                    else:
                        entry_status = 'approved'
                    reason = None
                else:
                    # Current month (e.g. May): draft or submitted, not yet reviewed
                    entry_status = 'draft' if i % 3 == 0 else 'submitted'
                    reason = None

                hours = Decimal('7.5') if i % 5 == 0 else Decimal('8.0')

                # Entries already on an invoice are left as they are: they can't
                # change (re-seeding after a billing run must not rewrite them).
                if TimesheetEntry.objects.filter(contract=contract, date=day, invoice_line__isnull=False).exists():
                    entry_count += 1
                    continue
                TimesheetEntry.objects.update_or_create(
                    contract=contract,
                    date=day,
                    defaults={'hours': hours, 'status': entry_status, 'rejection_reason': reason},
                )
                entry_count += 1

        self.stdout.write(self.style.SUCCESS(
            f'Done — {entry_count} timesheet entries across {len(contracts)} contracts.'
        ))
        self.stdout.write(self.style.SUCCESS(
            'Credentials:  admin@northstar.test / testpass123  |  alex@freelance.test / testpass123'
        ))
