from django.contrib import admin
from .models import BillingRun, Invoice, InvoiceEmail, InvoiceLine

admin.site.register(BillingRun)
admin.site.register(Invoice)
admin.site.register(InvoiceLine)
admin.site.register(InvoiceEmail)
