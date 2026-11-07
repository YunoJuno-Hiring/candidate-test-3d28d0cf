from rest_framework import serializers

from accounts.serializers import CompanySerializer, FreelancerSerializer
from .models import Invoice, InvoiceEmail, InvoiceLine


class InvoiceLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = InvoiceLine
        fields = ['id', 'timesheet_entry', 'date', 'hours', 'daily_rate', 'amount']


class InvoiceEmailSerializer(serializers.ModelSerializer):
    class Meta:
        model = InvoiceEmail
        fields = ['recipient', 'status', 'attempts', 'last_error', 'sent_at']

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # last_error holds raw mail-server errors (operational detail for the
        # company admin), so it isn't shown to anyone else, such as the freelancer.
        request = self.context.get('request')
        if not (request and hasattr(request.user, 'company_admin')):
            data.pop('last_error')
        return data


class InvoiceSerializer(serializers.ModelSerializer):
    # Company and freelancer details are read through the relations, not
    # copied onto the invoice; lines and total are what was billed.
    company = CompanySerializer(read_only=True)
    freelancer = FreelancerSerializer(read_only=True)
    period = serializers.DateField(format='%Y-%m', read_only=True)
    currency = serializers.SerializerMethodField()
    delivery_status = serializers.SerializerMethodField()

    class Meta:
        model = Invoice
        fields = [
            'id', 'number', 'period', 'company', 'freelancer',
            'total', 'currency', 'delivery_status', 'run', 'created_at',
        ]

    def get_currency(self, obj):
        return 'GBP'  # amounts exclude VAT

    def get_delivery_status(self, obj):
        # 'sent' once every recipient has it, 'failed' if any recipient needs a
        # retry, otherwise 'pending'. 'no_recipients' when neither the company
        # nor the freelancer had an email address: nothing will ever be sent, so
        # 'pending' would hide that the invoice was never delivered.
        statuses = {email.status for email in obj.emails.all()}
        if not statuses:
            return 'no_recipients'
        if statuses == {InvoiceEmail.STATUS_SENT}:
            return InvoiceEmail.STATUS_SENT
        if InvoiceEmail.STATUS_FAILED in statuses:
            return InvoiceEmail.STATUS_FAILED
        return InvoiceEmail.STATUS_PENDING


class InvoiceDetailSerializer(InvoiceSerializer):
    lines = InvoiceLineSerializer(many=True, read_only=True)
    emails = InvoiceEmailSerializer(many=True, read_only=True)

    class Meta(InvoiceSerializer.Meta):
        fields = InvoiceSerializer.Meta.fields + ['lines', 'emails']
