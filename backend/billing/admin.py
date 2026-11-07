from django.contrib import admin
from .models import BillingRun, Invoice, InvoiceEmail, InvoiceLine


class ReadOnlyAdmin(admin.ModelAdmin):
    # Billing records are issued financial records: viewable, never edited or
    # deleted by hand. Changing a line or total would make a sent invoice
    # disagree with its email, and deleting an invoice would CASCADE its lines
    # and free its timesheet entries to be billed again.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(BillingRun, ReadOnlyAdmin)
admin.site.register(Invoice, ReadOnlyAdmin)
admin.site.register(InvoiceLine, ReadOnlyAdmin)
admin.site.register(InvoiceEmail, ReadOnlyAdmin)
