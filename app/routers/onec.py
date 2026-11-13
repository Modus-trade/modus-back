from fastapi import APIRouter, Query
from ..security import CurrentUser
from ..services.onec import ITEMS

router = APIRouter(prefix='/api/integrations/onec', tags=['1C demo'])

@router.get('/items')
def items(user: CurrentUser, q: str = Query(default='', max_length=200)):
    terms = q.casefold().split()
    return {'mode':'mock', 'value':[item for item in ITEMS if all(
        term in (item['Description']+' '+item['Code']+' '+item['Артикул']).casefold()
        for term in terms)]}
