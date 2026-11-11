from test_workflow import clients, create  # noqa: F401


def test_notifications_recipients_read_and_privacy(clients):
    author, other, manager, outside, admin = clients
    ticket = create(author)
    assert author.get('/api/notifications').json()['unread_count'] == 0
    notice = manager.get('/api/notifications').json()['items'][0]
    assert notice['kind'] == 'assigned'
    assert notice['href'] == f"/tickets/{ticket['id']}"
    assert other.post(f"/api/notifications/{notice['id']}/read").status_code == 404
    assert manager.post(f"/api/notifications/{notice['id']}/read").status_code == 200
    assert manager.get('/api/notifications').json()['unread_count'] == 0
    assert manager.post(f"/api/tickets/{ticket['id']}/comments", json={'body':'Уточните детали', 'is_clarification_request':True}).status_code == 201
    items = author.get('/api/notifications').json()['items']
    assert items[0]['kind'] == 'clarification'
    assert 'body' not in items[0]
    assert other.get('/api/notifications').json()['total'] == 0
    author.post('/api/notifications/read-all')
    assert author.get('/api/notifications').json()['unread_count'] == 0
    assert manager.patch(f"/api/tickets/{ticket['id']}", json={'status':'IN_PROGRESS'}).status_code == 200
    assert manager.patch(f"/api/tickets/{ticket['id']}", json={'status':'IMPLEMENTED','resolution':'Готово'}).status_code == 200
    items = author.get('/api/notifications').json()['items']
    assert items[0]['kind'] == 'result' and items[1]['kind'] == 'status'
    total = author.get('/api/notifications').json()['total']
    manager.patch(f"/api/tickets/{ticket['id']}", json={'status':'IMPLEMENTED','resolution':'Готово'})
    assert author.get('/api/notifications').json()['total'] == total
