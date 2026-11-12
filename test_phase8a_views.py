import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import phase8a_views as view

T=1794459600

class Phase8aViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        score={'n':1,'accuracy':1,'brier':.04,'logloss':.22}
        self.r={'as_of':T+14490,'prediction_enabled':True,
            'schedule':[{'cutoff':T,'status':'complete','calls':20},{'cutoff':T-3600,'status':'preflight_skipped','calls':0}],
            'assets':{s:{'recorded':1,'evaluated':1,'engines':{e:{'paired_hourly_descriptive':score,'paired_nonoverlap_primary':{'n':0}} for e in view.ENGINES}} for s in view.SYMBOLS},
            'answers':[{'symbol':'BTC-USD','engine':e,'cutoff':T,'target_end':T+14400,'probability_up':.8,'actual_up':1,'correct':True,'reference_price':100,'end_price':101,'brier':.04,'logloss':.22} for e in view.ENGINES],
            'private':'secret-unpublished'}
        self.summary={'known_cost_estimate_usd':.09,'unknown_cost_calls':5,'raw':'secret-unpublished',
            'assets':{s:{'news_count':3,'direct_news_count':0,'news':[{'title':'secret-unpublished'}],
                'results':[{'engine':e,'probability_up':.8,'status':'ok','raw':'secret-unpublished','latency_ms':1000} for e in view.ENGINES]} for s in view.SYMBOLS}}
        self.save()
    def save(self):
        (self.root/'report.json').write_text(json.dumps(self.r))
        (self.root/'prediction-status.json').write_text(json.dumps({'cutoff':T,'status':'complete'}))
        p=self.root/'runs'/str(T);p.mkdir(parents=True,exist_ok=True);(p/'summary.json').write_text(json.dumps(self.summary))
    def tearDown(self):self.tmp.cleanup()
    def test_public_projection_and_no_writes_or_network(self):
        before={str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch('urllib.request.urlopen',side_effect=AssertionError('network forbidden')):
            r=view.report(self.root,now=T+14500);h=view.render(r)
        self.assertEqual(len(r['predictions']),4);self.assertEqual(len(r['answers']),4)
        self.assertNotIn('secret-unpublished',json.dumps(r)+h)
        self.assertNotIn('raw',json.dumps(r));self.assertIn('的中',h);self.assertIn('18:00',h)
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
    def test_filters_missing_stale_and_skip(self):
        self.assertEqual(view.report(self.root,'SOL-USD',now=T+20000)['answers'],[])
        self.assertTrue(view.report(self.root,now=T+20000)['stale'])
        h=view.render(view.report(self.root,cutoff=str(T-3600)))
        self.assertIn('予測なし',h);self.assertIn('見送り',h)
        for symbol,cutoff in [('bad',None),('BTC-USD','../../.env'),('BTC-USD','1')]:
            with self.assertRaises(ValueError):view.report(self.root,symbol,cutoff)
        self.assertEqual(view.report(self.root/'missing')['status'],'unavailable')
    def test_corrupt_report_and_unknown_result_strings_do_not_leak(self):
        self.summary['assets']['BTC-USD']['results'][0]['status']='<script>secret-unpublished</script>'
        self.save();self.assertNotIn('<script>',view.render(view.report(self.root)))
        (self.root/'report.json').write_text('{broken')
        self.assertEqual(view.report(self.root)['status'],'unavailable')
        (self.root/'report.json').write_text('{"assets":"invalid"}')
        self.assertEqual(view.report(self.root)['status'],'unavailable')
    def test_symlink_summary_rejected(self):
        p=self.root/'runs'/str(T)/'summary.json';p.unlink();p.symlink_to(self.root/'report.json')
        self.assertEqual(view.report(self.root)['predictions'],[])
    def test_routes(self):
        import app
        with patch.object(app,'PHASE8A_WEB_ROOT',self.root):
            client=app.app.test_client()
            for symbol in view.SYMBOLS:
                r=client.get('/phase8a/?symbol='+symbol)
                self.assertEqual(r.status_code,200)
                self.assertIn('href="/phase8a/" aria-current="page"',r.get_data(as_text=True))
            r=client.get('/phase8a/data.json');self.assertEqual(r.status_code,200)
            self.assertNotIn('secret-unpublished',r.get_data(as_text=True))
            self.assertEqual(client.get('/phase8a/?symbol=BAD').status_code,400)
            self.assertIn('no-store',r.headers['Cache-Control'])
