from datetime import timedelta
from test_workflow import clients, create
from app.models import Ticket, now
from app.services import integrations

def test_impact_permissions_counts_and_missing_samples(clients):
    author,other,manager,outside,admin=clients
    assert author.get('/api/analytics/impact').status_code==403
    empty=admin.get('/api/analytics/impact').json()
    assert empty['current']['solution_hours'] is None
    t=create(author)
    with integrations.SessionLocal() as db:
        row=db.get(Ticket,t['id'])
        row.created_at=now()-timedelta(days=5)
        row.first_response_at=row.created_at+timedelta(hours=2)
        row.resolution_confirmed_at=row.created_at+timedelta(days=2)
        row.status='CLOSED';db.commit()
    data=manager.get('/api/analytics/impact').json()
    assert data['current']['response_hours']==2
    assert data['current']['solution_hours']==48
    assert data['current']['confirmed']==1
    assert data['backlog']==0
    assert len(data['weeks'])==12
    assert data['comparison_ready'] is False
    assert outside.get('/api/analytics/impact').json()['current']['created']==0
    for days in (30, 60, 90):
        response = admin.get('/api/analytics/impact', params={'days': days})
        assert response.status_code == 200, response.text
        assert response.json()['current']['confirmed'] == 1
    assert admin.get('/api/analytics/impact?days=7').status_code==422
