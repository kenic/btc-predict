import unittest
from repeated_analysis import agreement_bucket, summarize, paired_analysis
from volatility import winner, brier


def diagnostic(target, votes, original='QUIET', ensemble='QUIET', actual='QUIET', model='openai'):
    return dict(target=target,predictor=model,completed=sum(votes), votes=dict(zip(('QUIET','NORMAL','ACTIVE'),votes)), original_argmax=original,ensemble_argmax=ensemble,flips=sum(votes)-votes[('QUIET','NORMAL','ACTIVE').index(original)],distinct_choices=sum(v>0 for v in votes),agreement_pct=10*max(votes),agreement_bucket=agreement_bucket(dict(enumerate(votes))),single_error=int(original!=actual),ensemble_error=int(ensemble!=actual),ensemble_brier=.2)


class RepeatedAnalysisTests(unittest.TestCase):
    def test_choices_flips_changes_and_class_summary(self):
        rows=[diagnostic(1,[10,0,0]),diagnostic(2,[4,6,0],ensemble='NORMAL',actual='NORMAL'),diagnostic(3,[4,3,3],original='NORMAL',actual='NORMAL')]
        r=summarize(rows)
        self.assertEqual([r[k] for k in ('unanimous','two_classes','three_classes','choice_changing')],[1,1,1,2])
        self.assertEqual((r['flips'],r['flip_denominator']),(13,30))
        self.assertAlmostEqual(r['flip_rate_pct'],100*13/30)
        self.assertEqual((r['ensemble_unchanged'],r['ensemble_changed']),(1,2))
        self.assertEqual((r['wrong_to_correct'],r['correct_to_wrong']),(1,1))
        self.assertEqual(r['transition_matrix'][0],dict(original='QUIET',QUIET=14,NORMAL=6,ACTIVE=0))
        self.assertEqual(r['class_instability'][0]['unanimous_rate_pct'],50)
        self.assertEqual(r['class_instability'][1]['flip_rate_pct'],70)
        self.assertEqual(r['class_instability'][2]['flip_rate_pct'],None)
        self.assertEqual(r['agreement_performance'][2]['evaluated_n'],1)

    def test_bucket_boundaries(self):
        for votes, expected in [([10,0,0],'100%'),([9,1,0],'80–90%'),([8,2,0],'80–90%'),([7,3,0],'60–70%'),([6,4,0],'60–70%'),([5,5,0],'<=50%'),([4,3,3],'<=50%')]:
            self.assertEqual(agreement_bucket(dict(enumerate(votes))),expected)

    def test_partial_exclusion_and_pair_intersection(self):
        rows=[diagnostic(1,[10,0,0]),diagnostic(2,[10,0,0]),diagnostic(1,[0,10,0],model='jev'),diagnostic(3,[10,0,0],model='jev')]
        partial=diagnostic(2,[0,9,0],model='jev')
        rows.append(partial)
        n,paired=paired_analysis(rows)
        self.assertEqual(n,1)
        self.assertEqual(paired['jev']['flip_denominator'],10)
        r=summarize([partial])
        self.assertEqual(r['stability_n'],0)
        self.assertEqual(r['flip_rate_pct'],100)
        self.assertEqual(r['ensemble_changed'],0)

    def test_saved_vectors_integration_and_read_only(self):
        # Use the existing isolated DB fixture; do not call a model or runner.
        from test_next_stages import NextStagesTests
        import next_stages as stages
        import next_stage_views as views
        fixture=NextStagesTests()
        fixture.setUp()
        try:
            with stages.database() as c:
                source=dict(c.execute('SELECT * FROM volatility_predictions LIMIT 1').fetchone())
                source['p_quiet'],source['p_normal'],source['p_active']=.6,.3,.1
                import json
                c.execute('INSERT INTO phase4r_sources(source_id,source_json) VALUES (?,?)',(source['id'],json.dumps(source)))
                for i in range(10):
                    c.execute("INSERT INTO phase4r_runs(source_id,repeat_no,predictor,target_candle_time,started_at,status,p_quiet,p_normal,p_active,argmax_class) VALUES (?,?,?,?,'saved','complete',.1,.2,.7,'ACTIVE')",(source['id'],i+1,'openai',source['target_candle_time']))
            before=fixture.db.read_bytes()
            data=views.report()
            self.assertEqual(data['phase4r']['openai']['wrong_to_correct'],1)
            self.assertEqual(data['phase4r']['openai']['ensemble_changed'],1)
            self.assertAlmostEqual(data['phase4r']['openai']['ensemble_brier'],brier([.1,.2,.7],'ACTIVE'))
            self.assertEqual(data['diagnostics'][0]['ensemble_argmax'],winner([.1,.2,.7]))
            self.assertIn('class-specific instability',views.page(section='repeated'))
            self.assertEqual(fixture.db.read_bytes(),before)
        finally:
            fixture.tearDown()
