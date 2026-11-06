from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.api import object_body_error
from .models import Invoice, InvoiceEmail
from .serializers import InvoiceDetailSerializer, InvoiceSerializer
from .services import parse_period, run_billing


def _invoices_for(user):
    # Same scoping as contracts and timesheets: company admins see their
    # company's invoices, freelancers see their own.
    qs = Invoice.objects.select_related('company', 'freelancer').prefetch_related('emails')
    if hasattr(user, 'company_admin'):
        return qs.filter(company=user.company_admin.company)
    if hasattr(user, 'freelancer'):
        return qs.filter(freelancer=user.freelancer)
    return qs.none()


class BillingRunView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not hasattr(request.user, 'company_admin'):
            return Response(
                {'detail': 'Only company admins can run billing.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        error = object_body_error(request)
        if error:
            return error
        try:
            period = parse_period(request.data.get('month'))
        except ValueError as exc:
            return Response({'month': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        run = run_billing(request.user.company_admin.company, period, request.user)
        # Invoices are committed even if some emails failed; those are retried
        # outside the request.
        emails_failed = InvoiceEmail.objects.filter(
            invoice__run=run, status=InvoiceEmail.STATUS_FAILED
        ).count()
        return Response(
            {
                'run_id': run.id,
                'invoices_generated': run.invoices_generated,
                'emails_failed': emails_failed,
                'status': 'completed_with_errors' if emails_failed else 'completed',
            },
            status=status.HTTP_201_CREATED,
        )


class InvoiceListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = _invoices_for(request.user)

        month_param = request.query_params.get('month')
        if month_param:
            try:
                period = parse_period(month_param, allow_future=True)
            except ValueError as exc:
                return Response({'month': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
            qs = qs.filter(period=period)

        return Response(InvoiceSerializer(qs, many=True).data)


class InvoiceDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        try:
            invoice = _invoices_for(request.user).prefetch_related('lines').get(pk=pk)
        except Invoice.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(InvoiceDetailSerializer(invoice, context={'request': request}).data)
