from django.contrib import admin
from .models import Contract, TimesheetEntry

admin.site.register(Contract)


@admin.register(TimesheetEntry)
class TimesheetEntryAdmin(admin.ModelAdmin):
    # Invoiced entries are read-only here too; the model refuses changes anyway.
    def get_readonly_fields(self, request, obj=None):
        if obj is not None and obj.is_invoiced:
            return [field.name for field in self.model._meta.fields]
        return super().get_readonly_fields(request, obj)
