import datetime
import json
from decimal import Decimal

import pytest

from contracts.models import TimesheetEntry

NOT_OBJECTS = ['[]', '[{"status": "approved"}]', '"approved"', '5', 'null']
EXPECTED = {'detail': 'Request body must be a JSON object.'}


@pytest.mark.django_db
@pytest.mark.parametrize('body', NOT_OBJECTS)
def test_billing_run_rejects_non_object_body(admin_client, body):
    resp = admin_client.post('/api/billing/runs/', data=body, content_type='application/json')

    assert resp.status_code == 400
    assert resp.data == EXPECTED


@pytest.mark.django_db
@pytest.mark.parametrize('body', NOT_OBJECTS)
def test_timesheet_create_rejects_non_object_body(freelancer_client, body):
    resp = freelancer_client.post('/api/timesheets/', data=body, content_type='application/json')

    assert resp.status_code == 400
    assert resp.data == EXPECTED


@pytest.mark.django_db
@pytest.mark.parametrize('body', NOT_OBJECTS)
def test_timesheet_patch_rejects_non_object_body(admin_client, active_contract, body):
    entry = TimesheetEntry.objects.create(
        contract=active_contract, date=datetime.date(2026, 10, 1), hours=Decimal('8.0'), status='submitted'
    )

    resp = admin_client.patch(f'/api/timesheets/{entry.id}/', data=body, content_type='application/json')

    assert resp.status_code == 400
    assert resp.data == EXPECTED
    entry.refresh_from_db()
    assert entry.status == 'submitted'


@pytest.mark.django_db
def test_json_object_bodies_still_work(admin_client, active_contract):
    entry = TimesheetEntry.objects.create(
        contract=active_contract, date=datetime.date(2026, 10, 1), hours=Decimal('8.0'), status='submitted'
    )

    resp = admin_client.patch(
        f'/api/timesheets/{entry.id}/', data=json.dumps({'status': 'approved'}), content_type='application/json'
    )

    assert resp.status_code == 200
