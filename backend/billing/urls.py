from django.urls import path
from . import views

urlpatterns = [
    path('billing/runs/', views.BillingRunView.as_view(), name='billing-run'),
    path('invoices/', views.InvoiceListView.as_view(), name='invoice-list'),
    path('invoices/<int:pk>/', views.InvoiceDetailView.as_view(), name='invoice-detail'),
]
