"""Demonstration catalog. No connection to a live 1C database."""
from uuid import uuid5, NAMESPACE_URL

ITEMS = [dict(Ref_Key=str(uuid5(NAMESPACE_URL, 'modus:onec:demo:'+code)), Description=name,
              Code=code, Артикул=article, DeletionMark=False)
         for code,name,article in [
             ('УТ-000123','Кабель КСПВ 4×0,5','КСПВ-4-05'),
             ('УТ-000124','Извещатель дымовой ИП 212-141','ИП-212-141'),
             ('УТ-000125','Прибор приёмно-контрольный С2000-М','С2000-М'),
             ('УТ-000126','Кабель UTP Cat.6','UTP-CAT6'),
             ('УТ-000127','Источник питания 12 В, 3 А','БП-12-3'),
             ('УТ-000128','Камера IP купольная 4 Мп','IPC-4MP')]]

def find_item(ref):
    return next((item for item in ITEMS if item['Ref_Key'] == str(ref)), None)
