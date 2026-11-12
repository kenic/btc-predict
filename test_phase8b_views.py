import copy
import json
import unittest
from unittest.mock import patch
import phase8b_views as view
from test_phase8a_views import Phase8aViewTests, T

class Phase8bViewTests(Phase8aViewTests):
    def setUp(self):
        super().setUp()
        self.a=self.root
        self.b=self.root/'b';self.b.mkdir()
        for answer in self.r['answers']:answer['status']='ok'
        self.summary['assets']['BTC-USD']['news_set_id']='same'
        self.save()
        self.root=self.b;self.save()
        self.root=self.a

    def test_matched_scores_and_public_projection(self):
        with patch('urllib.request.urlopen',side_effect=AssertionError('network')):
            r=view.report(self.a,self.b);html=view.render(r)
        self.assertTrue(r['same_news'])
        self.assertEqual(r['paired']['hourly']['jev']['phase8a']['n'],1)
        self.assertEqual(r['paired']['nonoverlap']['jev']['phase8a']['n'],0)
        self.assertNotIn('secret-unpublished',json.dumps(r)+html)
        self.assertIn('8a 上昇確率',html);self.assertIn('8b 上昇確率',html)
        self.assertIn('/phase8b/?symbol=',html)
        self.assertIn('href="/phase8a/"',html)
        self.assertIn('href="/phase8b/" aria-current="page"',html)
        self.assertIn('action="/phase8b/"',html)

    def test_different_news_or_prices_or_failed_engine_not_comparable(self):
        original=copy.deepcopy(self.r)
        for change in ('price','failed','missing'):
            r=copy.deepcopy(original)
            if change=='price':r['answers'][0]['end_price']=200
            elif change=='failed':r['answers'][0]['status']='error'
            else:r['answers'].pop()
            (self.b/'report.json').write_text(json.dumps(r))
            self.assertEqual(view.report(self.a,self.b)['paired']['hourly']['jev']['phase8a']['n'],0)
        (self.b/'report.json').write_text(json.dumps(original))
        self.summary['assets']['BTC-USD']['news_set_id']='different'
        self.root=self.b;self.save();self.root=self.a
        r=view.report(self.a,self.b)
        self.assertFalse(r['same_news']);self.assertEqual(r['paired']['hourly']['jev']['phase8a']['n'],0)

    def test_missing_bad_cutoff_and_routes(self):
        self.assertEqual(view.report(self.a,self.b/'absent')['phase8b']['status'],'unavailable')
        for s,t in [('bad',None),('BTC-USD','../.env')]:
            with self.assertRaises(ValueError):view.report(self.a,self.b,s,t)
        import app
        with patch.object(app,'PHASE8A_WEB_ROOT',self.a),patch.object(app,'PHASE8B_WEB_ROOT',self.b):
            client=app.app.test_client()
            for symbol in view.base.SYMBOLS:
                self.assertEqual(client.get('/phase8b/?symbol='+symbol).status_code,200)
            r=client.get('/phase8b/data.json');self.assertEqual(r.status_code,200)
            self.assertNotIn('secret-unpublished',r.get_data(as_text=True));self.assertIn('no-store',r.headers['Cache-Control'])
            self.assertEqual(client.get('/phase8b/?symbol=bad').status_code,400)

for name in list(vars(Phase8aViewTests)):
    if name.startswith('test_') and name not in vars(Phase8bViewTests):setattr(Phase8bViewTests,name,None)
del Phase8aViewTests
