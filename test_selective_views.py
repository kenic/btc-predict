import copy
import json
import unittest
from test_phase8a_views import Phase8aViewTests, T
import phase8a_views as a
import phase8b_views as b

class ScoresTests(unittest.TestCase):
    def test_boundaries_empty_all_abstain_and_extremes(self):
        self.assertEqual([a.decision(p) for p in [.4,.4000001,.49,.5,.5999999,.6]],['DOWN','ABSTAIN','ABSTAIN','ABSTAIN','ABSTAIN','UP'])
        s=a.selective_scores([(.4,0),(.6,1),(.49,1),(.5,0)])
        self.assertEqual((s['n'],s['decided_n'],s['abstained_n'],s['accuracy'],s['abstention_rate']),(4,2,2,1,.5))
        self.assertEqual(s['brier'],b.scores([(.4,0),(.6,1),(.49,1),(.5,0)])['brier'])
        self.assertEqual(s['logloss'],b.scores([(.4,0),(.6,1),(.49,1),(.5,0)])['logloss'])
        s=a.selective_scores([(.49,1),(.5,0)]);self.assertIsNone(s['accuracy']);self.assertEqual(s['abstention_rate'],1)
        self.assertIsNone(a.selective_scores([])['abstention_rate'])
        self.assertIsNone(a.selective_scores([])['brier'])
        self.assertEqual(a.selective_scores([(float('nan'),0),(-1,1),(True,0)])['n'],0)
        self.assertGreater(a.selective_scores([(0,1),(1,0)])['logloss'],30)

class SelectiveViewTests(Phase8aViewTests):
    def test_full_history_not_last_80_and_legacy_preserved(self):
        original=copy.deepcopy(self.r['assets'])
        self.r['answers']=[]
        for i in range(25):
            for e in a.ENGINES:
                self.r['answers'].append(dict(symbol='BTC-USD',engine=e,cutoff=T+i*3600,probability_up=.49,actual_up=0,correct=True,status='ok'))
        self.save();r=a.report(self.root)
        self.assertEqual(len(r['answers']),80)
        self.assertEqual(r['selective_evaluation']['hourly']['jev']['n'],25)
        self.assertEqual(r['selective_evaluation']['hourly']['jev']['abstention_rate'],1)
        self.assertIsNone(r['selective_evaluation']['hourly']['jev']['accuracy'])
        self.assertEqual(r['assets']['BTC-USD']['engines']['jev'],{k:a.score(v) for k,v in original['BTC-USD']['engines']['jev'].items()})
        self.r['answers'][0]['status']='error';self.save()
        self.assertEqual(a.report(self.root)['selective_evaluation']['hourly']['jev']['n'],24)

    def test_visible_abstention_does_not_erase_legacy_outcome(self):
        for v in self.summary['assets']['BTC-USD']['results']:v['probability_up']=.49
        for v in self.r['answers']:v.update(probability_up=.49,actual_up=0,correct=True,status='ok')
        self.save();r=a.report(self.root);html=a.render(r)
        self.assertTrue(r['answers'][0]['correct']);self.assertEqual(r['answers'][0]['decision'],'ABSTAIN')
        self.assertIn('見送り（採点対象外）',html);self.assertIn('従来の答え合わせ（50%境界）',html)
        self.assertIn('的中',html);self.assertIn('Brier（全件）',html)

for name in list(vars(Phase8aViewTests)):
    if name.startswith('test_') and name not in vars(SelectiveViewTests):setattr(SelectiveViewTests,name,None)
del Phase8aViewTests
