from django.db import transaction
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Contract, TimesheetEntry
from .serializers import (
    ContractCreateSerializer,
    ContractSerializer,
    TimesheetEntrySerializer,
)


class ContractListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = self._get_queryset(request.user)
        return Response(ContractSerializer(qs, many=True).data)

    def post(self, request):
        # Manual contract creation — primarily a convenience for walking through
        # the app by hand. A company admin creates contracts for their own company;
        # the company is taken from the admin, never trusted from the request body.
        if not hasattr(request.user, 'company_admin'):
            return Response(
                {'detail': 'Only company admins can create contracts.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = ContractCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        contract = serializer.save(company=request.user.company_admin.company)
        return Response(ContractSerializer(contract).data, status=status.HTTP_201_CREATED)

    def _get_queryset(self, user):
        if hasattr(user, 'company_admin'):
            return Contract.objects.filter(
                company=user.company_admin.company
            ).select_related('company', 'freelancer')
        if hasattr(user, 'freelancer'):
            return Contract.objects.filter(
                freelancer=user.freelancer
            ).select_related('company', 'freelancer')
        return Contract.objects.none()


class ContractDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        # Fetch the contract and verify the current user should be able to see it.
        # Company admins see contracts belonging to their company; freelancers see their own.
        # Note: this permission check and the queryset construction are both inline here.
        # TODO: extract permission checking into a reusable mixin or policy object
        user = request.user

        if hasattr(user, 'company_admin'):
            try:
                contract = Contract.objects.select_related('company', 'freelancer').get(
                    pk=pk, company=user.company_admin.company
                )
            except Contract.DoesNotExist:
                return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        elif hasattr(user, 'freelancer'):
            try:
                contract = Contract.objects.select_related('company', 'freelancer').get(
                    pk=pk, freelancer=user.freelancer
                )
            except Contract.DoesNotExist:
                return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        else:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        return Response(ContractSerializer(contract).data)


class TimesheetListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user

        if hasattr(user, 'company_admin'):
            qs = TimesheetEntry.objects.filter(
                contract__company=user.company_admin.company
            )
        elif hasattr(user, 'freelancer'):
            qs = TimesheetEntry.objects.filter(
                contract__freelancer=user.freelancer
            )
        else:
            qs = TimesheetEntry.objects.none()

        # Filter by status query param
        status_param = request.query_params.get('status')
        if status_param:
            qs = qs.filter(status=status_param)

        # Filter by contract id query param
        # TODO: move filter logic to a proper FilterSet class when we add django-filter
        contract_param = request.query_params.get('contract')
        if contract_param:
            qs = qs.filter(contract_id=contract_param)

        return Response(TimesheetEntrySerializer(qs, many=True).data)

    def post(self, request):
        if not hasattr(request.user, 'freelancer'):
            return Response(
                {'detail': 'Only freelancers can submit timesheet entries.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = TimesheetEntrySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        contract_id = request.data.get('contract')
        try:
            contract = Contract.objects.get(pk=contract_id, freelancer=request.user.freelancer)
        except Contract.DoesNotExist:
            return Response({'contract': 'Invalid contract.'}, status=status.HTTP_400_BAD_REQUEST)

        entry = serializer.save(contract=contract, status=TimesheetEntry.STATUS_SUBMITTED)
        return Response(TimesheetEntrySerializer(entry).data, status=status.HTTP_201_CREATED)


ADMIN_EDITABLE_FIELDS = {'status', 'rejection_reason'}
FREELANCER_EDITABLE_FIELDS = {'hours', 'date'}


class TimesheetDetailView(APIView):
    permission_classes = [IsAuthenticated]

    # Atomic so the entry stays locked from the invoiced check until the save:
    # a billing run locks entries the same way, so the two can't interleave.
    @transaction.atomic
    def patch(self, request, pk):
        user = request.user

        if hasattr(user, 'company_admin'):
            try:
                entry = TimesheetEntry.objects.select_for_update(of=('self',)).get(
                    pk=pk, contract__company=user.company_admin.company
                )
            except TimesheetEntry.DoesNotExist:
                return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        elif hasattr(user, 'freelancer'):
            try:
                entry = TimesheetEntry.objects.select_for_update(of=('self',)).get(
                    pk=pk, contract__freelancer=user.freelancer
                )
            except TimesheetEntry.DoesNotExist:
                return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        else:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        # Once an entry is on an invoice it can't change, for anyone: otherwise
        # an issued invoice and its timesheet would disagree (e.g. the company
        # invoiced for work it later rejected). Corrections would need a credit note.
        if hasattr(entry, 'invoice_line'):
            return Response(
                {'detail': 'This entry has been invoiced and can no longer be changed.'},
                status=status.HTTP_409_CONFLICT,
            )

        # Billing invoices whatever is approved, so each role may only change
        # what it owns: admins decide entries, freelancers correct their own
        # undecided hours. Anything else, such as moving an entry to another
        # contract, is refused.
        is_admin = hasattr(user, 'company_admin')
        fields = set(request.data.keys())

        if not is_admin and fields & ADMIN_EDITABLE_FIELDS:
            return Response(
                {'detail': 'Only company admins can approve or reject entries.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        disallowed = fields - (ADMIN_EDITABLE_FIELDS if is_admin else FREELANCER_EDITABLE_FIELDS)
        if disallowed:
            return Response(
                {field: 'This field cannot be changed.' for field in sorted(disallowed)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if is_admin:
            new_status = request.data.get('status')
            rejection_reason = request.data.get('rejection_reason')

            if new_status not in (TimesheetEntry.STATUS_APPROVED, TimesheetEntry.STATUS_REJECTED):
                return Response(
                    {'status': 'Must be "approved" or "rejected".'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if entry.status == TimesheetEntry.STATUS_DRAFT:
                return Response(
                    {'detail': 'This entry has not been submitted yet.'},
                    status=status.HTTP_409_CONFLICT,
                )
            if new_status == TimesheetEntry.STATUS_REJECTED and not rejection_reason:
                return Response(
                    {'rejection_reason': 'This field is required when rejecting an entry.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            # An approved entry never keeps a reason from an earlier rejection.
            data = {
                'status': new_status,
                'rejection_reason': rejection_reason if new_status == TimesheetEntry.STATUS_REJECTED else None,
            }
        else:
            if entry.status not in (TimesheetEntry.STATUS_DRAFT, TimesheetEntry.STATUS_SUBMITTED):
                return Response(
                    {'detail': 'Only draft or submitted entries can be edited.'},
                    status=status.HTTP_409_CONFLICT,
                )
            data = request.data

        serializer = TimesheetEntrySerializer(entry, data=data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)
