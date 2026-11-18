
import argparse, json, hashlib
from pathlib import Path
from collections import Counter
import pandas as pd

CUTOFF=pd.Timestamp('2026-07-01T00:00:00+03:00')
ROLES={'Проверка заявки':'specialist','Согласование руководителем':'manager','Доработка заявки':'requester','Согласование бюджета':'finance','Проверка договора':'legal','Исполнение заявки':'procurement','Внеплановая проверка':'auditor'}
def work_hours(a,b):
    total=0.; day=a.normalize()
    while day<b:
        if day.weekday()<5:
            lo=max(a,day+pd.Timedelta(hours=9)); hi=min(b,day+pd.Timedelta(hours=18))
            total+=max(0,(hi-lo).total_seconds()/3600)
        day+=pd.Timedelta(days=1)
    return total

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--input',type=Path,default=Path(__file__).resolve().parent.parent/'1кейс/students.csv');parser.add_argument('--output',type=Path,default=Path(__file__).resolve().parent)
    args=parser.parse_args();out=args.output;out.mkdir(parents=True,exist_ok=True)
    d=pd.read_csv(args.input);d.Timestamp=pd.to_datetime(d.Timestamp);d=d.sort_values(['ProcessInstanceId','EventIndex'])
    assert not d.EventId.duplicated().any() and not d.duplicated(['ProcessInstanceId','EventIndex']).any()
    raw_events=len(d);future=d[d.Timestamp>CUTOFF].copy()
    future.to_csv(out/'events_after_cutoff.csv',index=False)
    d=d[d.Timestamp<=CUTOFF].copy()
    route_rows=[]
    for pid,g in d.groupby('ProcessInstanceId'):
        observed=g[g.EventType.isin(['automatic_completed','task_completed','process_completed'])]
        route_rows.append({'RequestType':g.RequestType.iloc[0],'Completed':bool((g.EventType=='process_completed').any()),'Route':' > '.join(observed.ActivityName)})
    routes=pd.DataFrame(route_rows).value_counts(['RequestType','Completed','Route']).reset_index(name='NumberOfCases')
    routes['ShareWithinType']=routes.NumberOfCases/routes.RequestType.map(pd.DataFrame(route_rows).RequestType.value_counts())
    routes.to_csv(out/'route_statistics.csv',index=False)
    for pid,g in d.groupby('ProcessInstanceId'):
        assert g.EventIndex.tolist()==list(range(1,len(g)+1)) and g.Timestamp.is_monotonic_increasing
        assert all(g[x].nunique()==1 for x in ['RequestType','Amount','TemplateVersion'])
        assert (g.EventType=='process_started').sum()==1
    c=d[d.EventType=='task_created'];e=d[d.EventType=='task_completed']
    assert c.TaskId.notna().all() and not c.TaskId.duplicated().any() and not e.TaskId.duplicated().any()
    assert set(e.TaskId)<=set(c.TaskId)
    t=c.merge(e[['TaskId','Timestamp','Role','UserId','Result','ActivityId','ProcessInstanceId']],on='TaskId',how='left',suffixes=('','_end'),validate='one_to_one')
    closed=t.Timestamp_end.notna()
    assert (t.loc[closed,'ActivityId']==t.loc[closed,'ActivityId_end']).all()
    assert (t.loc[closed,'ProcessInstanceId']==t.loc[closed,'ProcessInstanceId_end']).all()
    t['Open']=~closed;t.Timestamp_end=t.Timestamp_end.fillna(CUTOFF)
    t['CalendarHours']=(t.Timestamp_end-t.Timestamp).dt.total_seconds()/3600
    assert (t.CalendarHours>=0).all()
    t['WorkHours']=[work_hours(a,b) for a,b in zip(t.Timestamp,t.Timestamp_end)]
    stats=[];limits={}
    for key,g in t[~t.Open].groupby(['RequestType','ActivityName']):
        q1,med,q3=g.WorkHours.quantile([.25,.5,.75]);hi=max(q3+3*(q3-q1),5*med)
        limits[key]=(med,hi,len(g))
        stats.append(dict(RequestType=key[0],ActivityName=key[1],CompletedTasks=len(g),MedianWorkHours=med,MedianCalendarHours=g.CalendarHours.median(),Q1=q1,Q3=q3,UpperWorkHours=hi,LowerWorkHours=med/20))
    pd.DataFrame(stats).to_csv(out/'duration_statistics.csv',index=False)
    rows=[]
    def add(pid,kind,explanation):rows.append(dict(ProcessInstanceId=pid,AnomalyType=kind,Explanation=explanation))
    for pid,g in d.groupby('ProcessInstanceId'):
        typ=g.RequestType.iloc[0];tasks=t[t.ProcessInstanceId==pid];events=g[g.EventType=='task_completed'];acts=events.ActivityName.tolist()
        end=g[g.EventType=='process_completed'];success=len(end)>0 and end.Result.iloc[-1]=='fulfilled'
        if success:
            required=['Проверка заявки','Согласование руководителем','Согласование бюджета']+(['Проверка договора'] if typ=='contract' else [])+['Исполнение заявки']
            for act in required:
                if act not in acts:add(pid,'Пропущен обязательный этап',f'Заявка {typ} завершена fulfilled, но нет задания «{act}»: ни создания, ни закрытия.' if not (tasks.ActivityName==act).any() else f'Заявка {typ} завершена fulfilled без закрытия этапа «{act}».')
            execution=tasks[tasks.ActivityName=='Исполнение заявки']
            if len(execution):
                x=execution.iloc[0]
                for act in required[:-1]:
                    prior=tasks[(tasks.ActivityName==act)&~tasks.Open]
                    if len(prior) and not (prior.Timestamp_end<=x.Timestamp).any():
                        r=prior.iloc[-1]
                        add(pid,'Нарушен порядок этапов',f'«Исполнение заявки» создано {x.Timestamp.isoformat()} (EventIndex={x.EventIndex}) до закрытия «{act}» {r.Timestamp_end.isoformat()}; обязательная проверка должна предшествовать исполнению.')
        for r in events.itertuples():
            if r.Role!=ROLES.get(r.ActivityName,r.Role):
                starter=g[g.EventType=='process_started'].UserId.iloc[0]
                add(pid,'Несоответствие роли',f'«{r.ActivityName}», EventIndex={r.EventIndex}: роль {r.Role}, ожидается {ROLES[r.ActivityName]}; исполнитель '+('совпадает с инициатором заявки.' if r.UserId==starter else 'не совпадает с инициатором заявки.'))
        returns=int((events.Result=='return').sum())
        if returns>=3:add(pid,'Многократные возвраты',f'Руководитель вернул заявку {returns} раз; зафиксировано {acts.count("Доработка заявки")} доработок и {acts.count("Проверка заявки")} проверок. В основной массе историй 0–1 возврат. Цикл допустим, но частота требует проверки причин.')
        for a,b in zip(events.itertuples(),list(events.itertuples())[1:]):
            if a.ActivityName==b.ActivityName:
                add(pid,'Повтор без возврата',f'Два последовательных закрытия «{a.ActivityName}», EventIndex={a.EventIndex} и {b.EventIndex}, с разными TaskId ({a.TaskId}; {b.TaskId}). Между ними нет возврата и доработки; это не дубликат EventId.')
        audit=events[events.ActivityName=='Внеплановая проверка']
        if len(audit):add(pid,'Внеплановые проверки — уточнить',f'Выполнено внеплановых проверок: {len(audit)}; EventIndex: '+', '.join(map(str,audit.EventIndex))+'. Роль auditor допустима. Проверки прерывают обычный маршрут до и после бюджета; требуется основание их назначения. Нарушение по журналу не доказано.')
        fast=[]
        for r in tasks.itertuples():
            med,hi,n=limits[(typ,r.ActivityName)]
            if n<20:continue
            if r.WorkHours>hi:
                kind='Длительное незавершённое задание' if r.Open else 'Длительное задание'
                add(pid,kind,f'«{r.ActivityName}», TaskId={r.TaskId}, тип {typ}: '+('на дату среза прошло не менее ' if r.Open else 'длительность ')+f'{r.WorkHours:.3f} рабочего и {r.CalendarHours:.3f} календарного часа. Медиана {med:.3f} рабочего часа, порог {hi:.3f} (n={n}).')
            if not r.Open and r.WorkHours<med/20 and r.CalendarHours<med/20:
                fast.append(f'«{r.ActivityName}» {r.CalendarHours*3600:.0f} с при медиане {med*3600:.0f} с')
        if fast:add(pid,'Подозрительно быстрое выполнение','; '.join(fast)+'. Длительность меньше 1/20 медианы своего этапа и типа заявки и по рабочему, и по календарному времени. Проверить автоматизацию или формальное закрытие.')
    result=pd.DataFrame(rows).drop_duplicates().sort_values(['AnomalyType','ProcessInstanceId','Explanation'])
    assert set(result.ProcessInstanceId)<=set(d.ProcessInstanceId)
    result.to_csv(out/'anomalies.csv',index=False,encoding='utf-8')
    counts=result.groupby('AnomalyType').agg(Rows=('ProcessInstanceId','size'),Processes=('ProcessInstanceId','nunique')).reset_index()
    base=d.groupby('ProcessInstanceId').first()
    rets=d[d.Result=='return'].groupby('ProcessInstanceId').size().reindex(base.index,fill_value=0)
    summary={'source_sha256':hashlib.sha256(args.input.read_bytes()).hexdigest(),'raw_events':raw_events,'excluded_events':len(future),'excluded_processes':future.ProcessInstanceId.nunique(),'events':len(d),'processes':len(base),'completed':int((d.EventType=='process_completed').sum()),'anomaly_rows':len(result),'candidate_processes':result.ProcessInstanceId.nunique(),'types':base.RequestType.value_counts().to_dict(),'returns':rets.value_counts().sort_index().to_dict(),'counts':counts.to_dict('records'),'examples':result.groupby('AnomalyType').first().reset_index().to_dict('records'),'open':t[t.Open][['ProcessInstanceId','ActivityName','WorkHours','CalendarHours']].to_dict('records')}
    (out/'analysis_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
