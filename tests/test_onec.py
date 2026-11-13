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


def payload(item=None, doc=None):
    return {'type':'PROBLEM','category_id':1,'title':'Ошибка в поставке кабеля',
            'description':'В поставке указаны неверные характеристики кабеля',
            'onec_item_ref':item,'onec_document_ref':doc,'onec_document_kind':'purchase','onec_scope':'mock'}


def test_documents_snapshot_privacy_and_supplier_analytics(clients):
    author,other,manager,outside,admin=clients
    item=author.get('/api/integrations/onec/items').json()['value'][0]['Ref_Key']
    doc=author.get('/api/integrations/onec/documents').json()['value'][0]['Ref_Key']
    body=payload(item,doc)
    body['is_shared']=True
    assert author.post('/api/tickets',json=body).status_code==422
    body['is_shared']=False
    tickets=[author.post('/api/tickets',json=body).json() for _ in range(2)]
    assert tickets[0]['onec_document']['partner']['Description']
    assert other.get('/api/tickets/'+str(tickets[0]['id'])).status_code==404
    assert admin.patch('/api/tickets/'+str(tickets[0]['id']),json={'is_shared':True}).status_code==422
    assert author.get('/api/integrations/onec/analytics').status_code==403
    assert outside.get('/api/integrations/onec/analytics').json()['groups']==[]
    data=manager.get('/api/integrations/onec/analytics?group=suppliers').json()
    assert len(data['groups'])==1 and data['groups'][0]['count']==2
    details=manager.get('/api/integrations/onec/analytics',params={'group':'suppliers','key':data['groups'][0]['key']}).json()
    assert details['total']==2 and len(details['tickets'])==2
    assert manager.get('/api/integrations/onec/analytics?mode=real').json()['groups']==[]


def test_modes_logs_and_stale_selection(clients,monkeypatch):
    author,_,_,_,admin=clients
    assert author.put('/api/integrations/onec/mode',json={'mode':'off'}).status_code==403
    assert admin.post('/api/integrations/onec/sync').status_code==200
    assert admin.get('/api/integrations/onec').json()['jobs'][0]['state']=='success'
    ref=author.get('/api/integrations/onec/items').json()['value'][0]['Ref_Key']
    assert admin.put('/api/integrations/onec/mode',json={'mode':'off'}).status_code==200
    assert author.get('/api/integrations/onec/items').json()['value']==[]
    assert author.post('/api/tickets',json=payload(ref)).status_code==422
    monkeypatch.delenv('ONEC_ODATA_URL',raising=False)
    assert admin.put('/api/integrations/onec/mode',json={'mode':'real'}).status_code==422
    assert admin.get('/api/integrations/onec').json()['mode']=='off'


def test_real_import_atomicity_and_read_only_adapter(clients,monkeypatch):
    import httpx
    from app.services import onec_client
    from app.services.onec import ITEMS, PARTNERS, DOCUMENTS
    from app.services.onec_client import DEFAULTS
    author,_,manager,_,admin=clients
    monkeypatch.setenv('ONEC_ODATA_URL','https://onec.test/base/odata/standard.odata')
    monkeypatch.setenv('ONEC_USERNAME','reader')
    monkeypatch.setenv('ONEC_PASSWORD','secret-must-not-leak')
    monkeypatch.delenv('ONEC_ALLOW_EMPLOYEE_DOCUMENTS',raising=False)
    failed=[False]; requests=[]
    def handle(request):
        requests.append(request)
        assert request.method=='GET'
        kind=next(k for k,v in DEFAULTS.items() if request.url.path.endswith(v))
        if failed[0] and kind=='sale': return httpx.Response(500,text='secret-must-not-leak')
        data=ITEMS if kind=='items' else PARTNERS if kind=='partners' else [
            {'Ref_Key':d['Ref_Key'],'Number':d['Number'],'Date':d['Date'],'Контрагент_Key':d['partner']['Ref_Key']}
            for d in DOCUMENTS if d['kind']==kind]
        return httpx.Response(200,json={'value':data})
    real_client=httpx.Client
    monkeypatch.setattr(onec_client.httpx,'Client',lambda **kw:real_client(transport=httpx.MockTransport(handle),**kw))
    assert admin.put('/api/integrations/onec/mode',json={'mode':'real'}).status_code==200
    assert admin.post('/api/integrations/onec/sync').status_code==200
    catalog=author.get('/api/integrations/onec/items').json()
    assert catalog['mode']=='real' and len(catalog['value'])==6
    assert author.get('/api/integrations/onec/documents').status_code==403
    assert manager.get('/api/integrations/onec/documents').status_code==200
    assert author.post('/api/tickets',json=payload(catalog['value'][0]['Ref_Key'])).status_code==422
    body=payload(catalog['value'][0]['Ref_Key']);body['onec_scope']=catalog['scope']
    assert author.post('/api/tickets',json=body).status_code==201
    failed[0]=True
    assert admin.post('/api/integrations/onec/sync').status_code==502
    assert len(author.get('/api/integrations/onec/items').json()['value'])==6
    state=admin.get('/api/integrations/onec').json()
    assert state['jobs'][0]['state']=='error'
    assert 'secret-must-not-leak' not in str(state)
    assert requests


def test_odata_pagination_and_invalid_response(monkeypatch):
    import httpx, pytest
    from uuid import UUID
    from app.services import onec_client
    monkeypatch.setenv('ONEC_ODATA_URL','https://onec.test/odata/standard.odata')
    monkeypatch.setenv('ONEC_USERNAME','reader');monkeypatch.setenv('ONEC_PASSWORD','secret')
    seen=[]
    def handle(request):
        skip=int(request.url.params['$skip']);seen.append(skip)
        return httpx.Response(200,json={'value':[{'Ref_Key':str(UUID(int=i+1)),'Description':'item'} for i in range(skip,min(skip+200,201))]})
    real=httpx.Client
    monkeypatch.setattr(onec_client.httpx,'Client',lambda **kw:real(transport=httpx.MockTransport(handle),**kw))
    assert len(onec_client.ODataClient().all('items'))==201 and seen==[0,200]
