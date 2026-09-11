import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
from sources import planificacion_sales as sales, planificacion_source as ps
from sources.health import stamp
from assistant_engine import respond


class SalesTests(unittest.TestCase):
    def setUp(self):
        self.data = {'dates':['2026-08-30','2026-09-01','2026-09-11','2026-09-12'], 'daily':[
            {'client':'1','action':'CORE','date':'2026-08-30','bultos':100},
            {'client':'1','action':'CORE','date':'2026-09-01','bultos':10},
            {'client':'1','action':'CORE','date':'2026-09-11','bultos':-2},
            {'client':'1','action':'CORE','date':'2026-09-12','bultos':50}]}

    def test_current_month_net_and_future_exclusion(self):
        result=sales.purchases(self.data,'1','CORE',500,today='2026-09-11')
        self.assertEqual(result['bought'],8)
        self.assertEqual(result['base_remaining'],492)
        self.assertTrue(result['future_excluded'])
        self.assertEqual(result['cutoff'],'2026-09-11')

    def test_no_month_is_not_zero(self):
        self.assertIsNone(sales.purchases(self.data,'1','CORE',500,today='2026-10-01'))
        self.assertIsNone(sales.purchases(None,'1','CORE',500,today='2026-09-11'))

    def test_no_customer_sales_is_zero_with_valid_source(self):
        self.assertEqual(sales.purchases(self.data,'2','CORE',500,today='2026-09-11')['bought'],0)

    def test_extension_date_inclusive_and_separate_from_base(self):
        ext={'active':True,'date':'2026-09-11'}
        got=sales.purchases(self.data,'1','CORE',500,ext,today='2026-09-11')
        self.assertEqual(got['bought'],8)
        self.assertEqual(got['second_bought'],-2)
        self.assertEqual(got['second_remaining'],502)

    def test_exceeded_not_clipped(self):
        self.assertEqual(sales.purchases(self.data,'1','CORE',5,today='2026-09-11')['base_remaining'],-3)

    def test_quantity_column_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'sales.txt'
            path.write_text('Bultos Promedio\n10\n')
            with self.assertRaisesRegex(ValueError,'Cantidades Totales'):
                sales.load_daily(path,Path(tmp)/'missing.xlsx')

    def test_brand_override_and_business_exclusion(self):
        raw=pd.DataFrame({
            'Descripción Período':['11/09/2026']*5,
            'Cod. Cliente':['100572']*5,
            'Descripción.3':['QUILMES','QUILMES 1890','BRAHMA','OTRA','QUILMES'],
            'Descripción.8':['CERVEZAS','CZA','UNG','CZA','CZA'],
            'Cantidades Totales':['10,5','3,0','100','2','-1,5']})
        aux=pd.DataFrame({'MARCA':['OTRA'],'SEGMENTO':['CORE']})
        with patch.object(sales.p,'read_tabular',return_value=raw),patch.object(sales.pd,'read_excel',return_value=aux):
            data=sales.load_daily(Path('source.txt'),Path('aux.xlsx'))
        self.assertEqual(sales.purchases(data,'100572','CORE',500,today='2026-09-11')['bought'],11)
        self.assertEqual(sales.purchases(data,'100572','VALUE',500,today='2026-09-11')['bought'],3)

    def test_purchase_followup_uses_bultos_not_repago_hl(self):
        today=stamp()[:10]
        snapshot={'updated_at':stamp(),'customers':{'100572':{'id':'100572','name':'CLEMENTE','canal':'AUTOSERVICIO'}},
                  'rules':{'AUTOSERVICIO':500},'extensions':{},
                  'sales':{'dates':[today],'daily':[{'client':'100572','action':'CORE','date':today,'bultos':10}]}}
        with patch.object(ps,'_snapshot',snapshot),patch('requests.sessions.Session.request',side_effect=AssertionError('query network')):
            answer,_,ctx=respond('tope 100572')
            self.assertIn('10,00 bultos netos',answer)
            self.assertIn('490,00',answer)
            answer,_,ctx=respond('cuantos lleva comprados al dia de hoy?',ctx)
            self.assertEqual(ctx['active_topic'],'tope')
            self.assertIn('10,00 bultos netos',answer)
            answer,_,ctx=respond('cuanto compro en value?',ctx)
            self.assertIn('0,00 bultos netos',answer)
            self.assertNotIn('CORE:',answer)


if __name__=='__main__':
    unittest.main()
