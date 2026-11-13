from test_workflow import clients, create

def test_catalog_and_ticket_snapshot(clients):
    author, _, _, _, admin = clients
    result = author.get('/api/integrations/onec/items?q=КСПВ').json()
    assert result['mode'] == 'mock' and len(result['value']) == 1
    item = result['value'][0]
    category = admin.get('/api/categories').json()[0]['id']
    body = {'type':'PROBLEM','category_id':category,'title':'Ошибка карточки товара','description':'Неверное описание кабеля в каталоге','onec_item_ref':item['Ref_Key']}
    response = author.post('/api/tickets',json=body)
    assert response.status_code == 201, response.text
    ticket = response.json()
    assert ticket['onec_item']['Code'] == item['Code']
    assert author.get('/api/tickets/'+str(ticket['id'])).json()['onec_item']['mode'] == 'mock'
    body['onec_item_ref'] = '00000000-0000-0000-0000-000000000000'
    assert author.post('/api/tickets',json=body).status_code == 422
