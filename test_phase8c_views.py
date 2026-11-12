import json
from unittest.mock import patch
import phase8c_views as view
from test_phase8b_views import Phase8bViewTests

class Phase8cViewTests(Phase8bViewTests):
    def setUp(self):
        super().setUp();self.c=self.a/'c';self.c.mkdir();self.root=self.c;self.save();self.root=self.a

    def test_three_conditions_and_matching(self):
        r=view.report(self.a,self.b,self.c);html=view.render(r)
        self.assertTrue(r['same_news']);self.assertIn('8c 上昇確率',html)
        self.assertNotIn('secret-unpublished',html+json.dumps(r))
        for label in ('phase8a','phase8b'):
            self.assertEqual(r['comparisons'][label]['hourly']['jev']['phase8c']['legacy']['n'],1)
        self.summary['assets']['BTC-USD']['news_set_id']='different'
        self.root=self.c;self.save();self.root=self.a
        r=view.report(self.a,self.b,self.c)
        self.assertFalse(r['same_news']);self.assertEqual(r['comparisons']['phase8b']['hourly']['jev']['phase8c']['legacy']['n'],0)

    def test_different_horizon_excluded(self):
        for answer in self.r['answers']:answer['target_end']+=3600
        self.root=self.c;self.save();self.root=self.a
        r=view.report(self.a,self.b,self.c)
        self.assertEqual(r['comparisons']['phase8b']['hourly']['jev']['phase8c']['legacy']['n'],0)

    def test_routes_and_no_cohort(self):
        import app
        with patch.object(app,'PHASE8A_WEB_ROOT',self.a),patch.object(app,'PHASE8B_WEB_ROOT',self.b),patch.object(app,'PHASE8C_WEB_ROOT',self.c):
            client=app.app.test_client()
            for symbol in view.base.SYMBOLS:
                response=client.get('/phase8c/?symbol='+symbol);self.assertEqual(response.status_code,200)
                self.assertIn('action="/phase8c/"',response.get_data(as_text=True))
            self.assertEqual(client.get('/phase8c/?symbol=bad').status_code,400)
            self.assertIn('no-store',client.get('/phase8c/data.json').headers['Cache-Control'])
        r=view.report(self.a,self.b,self.c/'missing');html=view.render(r)
        self.assertNotIn('80.0%',html);self.assertIn('初回予測',html)

for name in list(vars(Phase8bViewTests)):
    if name.startswith('test_') and name not in vars(Phase8cViewTests):setattr(Phase8cViewTests,name,None)
del Phase8bViewTests
