
import argparse,json,hashlib
from pathlib import Path
from collections import Counter,defaultdict
import pandas as pd

def main():
    p=argparse.ArgumentParser();p.add_argument('--input',type=Path,default=Path(__file__).resolve().parent.parent/'2кейс/students.csv');p.add_argument('--output',type=Path,default=Path(__file__).resolve().parent);a=p.parse_args();out=a.output;out.mkdir(parents=True,exist_ok=True)
    d=pd.read_csv(a.input).sort_values(['ProcessInstanceId','EventIndex']);d.Timestamp=pd.to_datetime(d.Timestamp)
    assert not d.EventId.duplicated().any() and not d.duplicated(['ProcessInstanceId','EventIndex']).any()
    assert not d[d.EventType=='task_completed'].TaskId.duplicated().any()
    assert d.groupby('ActivityId').ActivityName.nunique().max()==1 and d.groupby('ActivityName').ActivityId.nunique().max()==1
    routes=Counter();examples={};edges=Counter();cases=defaultdict(set);branch=Counter();presence=Counter();rework=Counter();amounts=[]
    for pid,g in d.groupby('ProcessInstanceId'):
        assert g.EventIndex.tolist()==list(range(1,len(g)+1)) and g.Timestamp.is_monotonic_increasing
        assert g.EventType.iloc[0]=='process_started' and g.EventType.iloc[-1]=='process_completed'
        assert (g.EventType=='process_started').sum()==(g.EventType=='process_completed').sum()==1
        route=tuple(g.ActivityName);routes[route]+=1;examples.setdefault(route,pid);presence.update(set(route));rework[route.count('Доработка заявки')]+=1
        for row1,row2 in zip(g.itertuples(),list(g.itertuples())[1:]):
            edge=(row1.ActivityName,row2.ActivityName);edges[edge]+=1;cases[edge].add(pid);branch[(row1.ActivityName,str(row1.Result),row2.ActivityName)]+=1
        amounts.append({'ProcessInstanceId':pid,'Amount':float(g.Amount.iloc[0]),'Extra':'Дополнительное согласование' in route,'Success':g.Result.iloc[-1]=='fulfilled'})
    n=d.ProcessInstanceId.nunique()
    variants=pd.DataFrame([{'Variant':f'V{i:02d}','Route':' > '.join(r),'NumberOfCases':c,'Share':c/n} for i,(r,c) in enumerate(routes.most_common(),1)])
    assert variants.NumberOfCases.sum()==n and abs(variants.Share.sum()-1)<1e-12
    variants.to_csv(out/'variants.csv',index=False,encoding='utf-8')
    pd.DataFrame(sorted(edges),columns=['SourceActivity','TargetActivity']).to_csv(out/'student_transitions.csv',index=False)
    pd.DataFrame([{'SourceActivity':s,'TargetActivity':t,'EventPairs':c,'NumberOfCases':len(cases[(s,t)])} for (s,t),c in sorted(edges.items())]).to_csv(out/'transition_frequencies.csv',index=False)
    assert sum(edges.values())==len(d)-n
    amounts=pd.DataFrame(amounts);successful=amounts[amounts.Success]
    summary={'source_sha256':hashlib.sha256(a.input.read_bytes()).hexdigest(),'events':len(d),'processes':n,'activities':len(presence),'edges':len(edges),'activity_presence':dict(presence),'rework_counts':dict(sorted(rework.items())),'endings':d[d.EventType=='process_completed'].ActivityName.value_counts().to_dict(),'variants':[dict(Variant=f'V{i:02d}',Count=c,Example=examples[r]) for i,(r,c) in enumerate(routes.most_common(),1)],'branch_results':[dict(Source=s,Result=r,Target=t,Count=c) for (s,r,t),c in sorted(branch.items())],'extra_amount_min':successful[successful.Extra].Amount.min(),'noextra_amount_max':successful[~successful.Extra].Amount.max(),'extra_count':int(successful.Extra.sum()),'threshold150000_errors':int(((successful.Amount>150000)!=successful.Extra).sum())}
    (out/'analysis_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(summary,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
