from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone
from rest_framework import serializers

from accounts.serializers import CompanySerializer, FreelancerSerializer
from .models import Contract, TimesheetEntry

MAX_HOURS_PER_DAY = Decimal('24')


class ContractSerializer(serializers.ModelSerializer):
    company = CompanySerializer(read_only=True)
    freelancer = FreelancerSerializer(read_only=True)

    class Meta:
        model = Contract
        fields = ['id', 'company', 'freelancer', 'daily_rate', 'start_date', 'end_date', 'status']


class ContractCreateSerializer(serializers.ModelSerializer):
    """Write-only serialiser for manually creating a contract.

    The company is supplied by the view (the admin's own company), so it is
    not accepted from the client. Reads still go through ContractSerializer.
    """

    class Meta:
        model = Contract
        fields = ['freelancer', 'daily_rate', 'start_date', 'end_date', 'status']

    def validate(self, attrs):
        if attrs['end_date'] < attrs['start_date']:
            raise serializers.ValidationError(
                {'end_date': 'End date must be on or after the start date.'}
            )
        return attrs


class TimesheetEntrySerializer(serializers.ModelSerializer):
    # TODO: move validation logic (rejection_reason check) out of the view and into validate() here
    contract_id = serializers.IntegerField(read_only=True)  # redundant: 'contract' already exposes the FK id

    class Meta:
        model = TimesheetEntry
        fields = ['id', 'contract', 'contract_id', 'date', 'hours', 'status', 'rejection_reason']
        read_only_fields = ['id', 'contract_id']

    def validate_hours(self, value):
        # Same limits as the "Log hours" form, enforced here because billing
        # prices whatever hours are approved.
        if value <= 0 or value > MAX_HOURS_PER_DAY:
            raise serializers.ValidationError(f'Must be more than 0 and at most {MAX_HOURS_PER_DAY}.')
        if value % Decimal('0.5'):
            raise serializers.ValidationError('Must be in steps of 0.5 hours.')
        return value

    def validate(self, attrs):
        instance = self.instance
        # The work rules apply when the work itself is written (logging or
        # correcting hours). An admin approving or rejecting an entry isn't
        # re-validated, so submitted work on a since-closed contract can still
        # be decided.
        writes_work = bool(attrs.keys() & {'contract', 'date', 'hours'})
        # Resubmitting rejected work makes its hours count again, so the daily
        # total must be re-checked even when nothing else changes.
        resubmits = bool(
            instance
            and instance.status == TimesheetEntry.STATUS_REJECTED
            and attrs.get('status') == TimesheetEntry.STATUS_SUBMITTED
        )
        if not (writes_work or resubmits):
            return attrs

        contract = attrs.get('contract', instance.contract if instance else None)
        date = attrs.get('date', instance.date if instance else None)
        hours = attrs.get('hours', instance.hours if instance else None)
        if contract is None or date is None or hours is None:
            return attrs  # required-field errors are reported by the fields

        if writes_work:
            if contract.status != Contract.STATUS_ACTIVE:
                raise serializers.ValidationError({'contract': 'This contract is closed.'})
            if date > timezone.localdate():
                raise serializers.ValidationError({'date': 'Cannot log hours for a future date.'})
            if not contract.start_date <= date <= contract.end_date:
                raise serializers.ValidationError(
                    {'date': f'Must be within the contract ({contract.start_date} to {contract.end_date}).'}
                )

        # A day has 24 hours across all of the freelancer's contracts. Rejected
        # hours don't count: they won't be billed, and would otherwise block
        # genuine work logged on another contract that day.
        other_hours = (
            TimesheetEntry.objects.filter(contract__freelancer=contract.freelancer, date=date)
            .exclude(status=TimesheetEntry.STATUS_REJECTED)
            .exclude(pk=instance.pk if instance else None)
            .aggregate(total=Sum('hours'))['total']
        ) or Decimal('0')
        if other_hours + hours > MAX_HOURS_PER_DAY:
            raise serializers.ValidationError(
                {'hours': f'Total hours on {date} would exceed {MAX_HOURS_PER_DAY} ({other_hours} already logged).'}
            )
        return attrs
