"""Pure read-only summaries of saved repeat diagnostics; rates are percentages."""
import statistics

CLASSES = ('QUIET', 'NORMAL', 'ACTIVE')
BUCKETS = ('100%', '80–90%', '60–70%', '<=50%')


def agreement_bucket(votes):
    modal = max(votes.values())
    return '100%' if modal == 10 else '80–90%' if modal >= 8 else '60–70%' if modal >= 6 else '<=50%'


def summarize(rows):
    full = [d for d in rows if d['completed'] == 10]
    scored = [d for d in full if 'ensemble_brier' in d]
    def avg(ds, key):
        return statistics.mean(d[key] for d in ds) if ds else None
    def pct(n, total):
        return 100*n/total if total else None
    runs = sum(d['completed'] for d in rows)
    flips = sum(d['flips'] for d in rows)
    result = dict(stability_n=len(full), unanimous=sum(d['distinct_choices']==1 for d in full),
                  two_classes=sum(d['distinct_choices']==2 for d in full),
                  three_classes=sum(d['distinct_choices']==3 for d in full),
                  choice_changing=sum(d['distinct_choices']>1 for d in full),
                  flips=flips, flip_denominator=runs, flip_rate_pct=pct(flips,runs),
                  mean_agreement_pct=avg(full,'agreement_pct'),
                  ensemble_unchanged=sum(d['original_argmax']==d['ensemble_argmax'] for d in full),
                  ensemble_changed=sum(d['original_argmax']!=d['ensemble_argmax'] for d in full),
                  wrong_to_correct=sum(d['single_error']==1 and d['ensemble_error']==0 for d in scored),
                  correct_to_wrong=sum(d['single_error']==0 and d['ensemble_error']==1 for d in scored),
                  single_accuracy_pct=100*(1-avg(scored,'single_error')) if scored else None,
                  ensemble_accuracy_pct=100*(1-avg(scored,'ensemble_error')) if scored else None)
    result['transition_matrix'] = [dict(original=c, **{k:sum(d['votes'][k] for d in rows if d['original_argmax']==c) for k in CLASSES}) for c in CLASSES]
    result['agreement_performance'] = []
    for bucket in BUCKETS:
        ds = [d for d in full if d['agreement_bucket']==bucket]
        ev = [d for d in ds if 'ensemble_brier' in d]
        result['agreement_performance'].append(dict(bucket=bucket, snapshots=len(ds), evaluated_n=len(ev), ensemble_accuracy_pct=100*(1-avg(ev,'ensemble_error')) if ev else None, ensemble_brier=avg(ev,'ensemble_brier')))
    result['class_instability'] = []
    for c in CLASSES:
        ds = [d for d in rows if d['original_argmax']==c]
        fs = [d for d in ds if d['completed']==10]
        total = sum(d['completed'] for d in ds)
        result['class_instability'].append(dict(original=c, snapshots=len(ds), stability_n=len(fs), unanimous_rate_pct=pct(sum(d['distinct_choices']==1 for d in fs),len(fs)), mean_agreement_pct=avg(fs,'agreement_pct'), flips=sum(d['flips'] for d in ds), completed_runs=total, flip_rate_pct=pct(sum(d['flips'] for d in ds),total)))
    # Descriptive comparison, not a significance test or a causal claim.
    stable = [d for d in scored if d['distinct_choices']==1]
    changing = [d for d in scored if d['distinct_choices']>1]
    result['stability_performance'] = [dict(group=name, evaluated_n=len(ds), ensemble_accuracy_pct=100*(1-avg(ds,'ensemble_error')) if ds else None, ensemble_brier=avg(ds,'ensemble_brier')) for name,ds in [('unanimous',stable),('choice-changing',changing)]]
    if stable and changing:
        delta = avg(changing,'ensemble_brier')-avg(stable,'ensemble_brier')
        accuracy_delta = 100*(avg(stable,'ensemble_error')-avg(changing,'ensemble_error'))
        result['association'] = f'Choice-changing minus unanimous: ensemble Brier {delta:+.4f}; accuracy {accuracy_delta:+.2f} percentage points. Higher Brier / lower accuracy means worse outcomes. Descriptive only; no significance or causal inference.'
    else:
        result['association'] = 'Insufficient evaluated snapshots in both groups to compare instability with outcomes.'
    return result


def paired_analysis(diagnostics):
    targets = set.intersection(*[{d['target'] for d in diagnostics if d['predictor']==m and d['completed']==10} for m in ('openai','jev')])
    return len(targets), {m:summarize([d for d in diagnostics if d['predictor']==m and d['target'] in targets]) for m in ('openai','jev')}
