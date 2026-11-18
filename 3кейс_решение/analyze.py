
import argparse,json,hashlib,re
from pathlib import Path
import pandas as pd
import numpy as np

def load(root,name,columns=None,chunksize=None):
    names=(root/'db_structure'/f'{name}.csv').read_text(encoding='utf-8-sig').splitlines()[0].split(';')
    return pd.read_csv(root/f'{name}.csv',sep=';',header=None,names=names,usecols=columns,low_memory=False,chunksize=chunksize)

def main():
 p=argparse.ArgumentParser();p.add_argument('--data',type=Path,default=Path(__file__).resolve().parent.parent/'3кейс/data');p.add_argument('--output',type=Path,default=Path(__file__).resolve().parent);a=p.parse_args();r=a.data;out=a.output;out.mkdir(parents=True,exist_ok=True)
 for name in ['orders','users','subscribe','transactions','devices','sessions']:
  for source in [r/f'{name}.csv',r/'db_structure'/f'{name}.csv']:
   if not source.exists():raise FileNotFoundError(f'Required source is missing: {source}')
 o=load(r,'orders',['id','user_id','product_id','amount','status','created_at','updated_at','trial','provider_id','pre_payment'])
 u=load(r,'users',['id','created_at','trial_used_at','deleted_at','is_was_guest'])
 s=load(r,'subscribe',['id','user_id','product_id','created_at','expired_time','order_id'])
 t=load(r,'transactions',['id','order_id','amount','status','created_at','updated_at'])
 for frame in [o,u,s,t]:
  for col in ['created_at','updated_at','trial_used_at']:
   if col in frame:frame[col]=pd.to_datetime(frame[col],format='mixed',errors='coerce',utc=True).dt.tz_localize(None)
 assert not any(x.id.duplicated().any() for x in [o,u,s,t])
 completed=t[t.status=='completed'];confirmed=set(completed.order_id)
 valid=o[(o.status=='paid')&(o.amount>0)&o.id.isin(confirmed)].copy()
 buys=valid[valid.trial=='f'];trials=valid[valid.trial=='t']
 firstbuy=buys.groupby('user_id').updated_at.min();firsttrial=trials.groupby('user_id').updated_at.min()
 profiles=u.set_index('id');buyers=firstbuy.to_frame('FirstPurchase').join(firsttrial.rename('FirstTrial')).join(profiles[['created_at','trial_used_at']])
 buyers['TrialBefore']=(buyers.FirstTrial<buyers.FirstPurchase)
 buyers['MarkerBefore']=buyers.trial_used_at<buyers.FirstPurchase
 buyers['AnyTrialBefore']=buyers.TrialBefore|buyers.MarkerBefore
 buyers['RegistrationDays']=(buyers.FirstPurchase-buyers.created_at).dt.total_seconds()/86400
 buyers['TrialDays']=(buyers.FirstPurchase-buyers.FirstTrial).dt.total_seconds()/86400
 cohort=firsttrial.to_frame('FirstTrial').join(firstbuy.rename('FirstPurchase'))

 cohort=cohort[cohort.FirstPurchase.isna()|(cohort.FirstPurchase>=cohort.FirstTrial)]
 cutoff=pd.Timestamp('2026-09-29T00:00:00')
 conversion=[]
 for days in [7,14,30]:
  c=cohort[cohort.FirstTrial<=cutoff-pd.Timedelta(days=days)]
  converted=c.FirstPurchase.notna()&(c.FirstPurchase<=c.FirstTrial+pd.Timedelta(days=days))
  conversion.append(dict(WindowDays=days,EligibleUsers=len(c),ConvertedUsers=int(converted.sum()),Conversion=float(converted.mean())))
 pd.DataFrame(conversion).to_csv(out/'trial_conversion.csv',index=False)
 mature=cohort[cohort.FirstTrial<=cutoff-pd.Timedelta(days=30)]
 fixed_conversion=[]
 for days in [7,14,30]:
  ok=mature.FirstPurchase.notna()&(mature.FirstPurchase<=mature.FirstTrial+pd.Timedelta(days=days))
  fixed_conversion.append(dict(WindowDays=days,EligibleUsers=len(mature),ConvertedUsers=int(ok.sum()),Conversion=float(ok.mean())))
 pd.DataFrame(fixed_conversion).to_csv(out/'trial_conversion_fixed_cohort.csv',index=False)
 monthly=[]
 for month,c in cohort[cohort.FirstTrial<=cutoff-pd.Timedelta(days=30)].groupby(cohort.FirstTrial.dt.to_period('M')):
  converted=c.FirstPurchase.notna()&(c.FirstPurchase<=c.FirstTrial+pd.Timedelta(days=30))
  monthly.append(dict(TrialMonth=str(month),EligibleUsers=len(c),Converted30Days=int(converted.sum()),Conversion30Days=float(converted.mean())))
 pd.DataFrame(monthly).to_csv(out/'trial_cohorts.csv',index=False)
 buyerstats=[]
 for name,c in [('all',buyers),('trial_before',buyers[buyers.AnyTrialBefore]),('no_trial_before',buyers[~buyers.AnyTrialBefore])]:
  ages=c.loc[c.RegistrationDays>=0,'RegistrationDays']
  adjusted=c.RegistrationDays+3/24;adjusted=adjusted[adjusted>=0]
  buyerstats.append(dict(Group=name,Buyers=len(c),ValidRawIntervals=len(ages),RawMedianDays=ages.median(),ValidAdjustedIntervals=len(adjusted),AdjustedP25Days=adjusted.quantile(.25),AdjustedMedianDays=adjusted.median(),AdjustedP75Days=adjusted.quantile(.75),AdjustedP90Days=adjusted.quantile(.9)))
 pd.DataFrame(buyerstats).to_csv(out/'purchase_timing.csv',index=False)

 paidtimes={uid:g.updated_at.sort_values().to_numpy(dtype='datetime64[ns]') for uid,g in valid.groupby('user_id')}
 failed=o[o.status=='failed'].copy();failed['Recovered24h']=False;failed['RecoveryHours']=np.nan
 failed=failed[failed.updated_at<=cutoff-pd.Timedelta(days=1)].copy()
 for uid,idx in failed.groupby('user_id').groups.items():
  arr=paidtimes.get(uid)
  if arr is None:continue
  times=failed.loc[idx,'updated_at'].to_numpy(dtype='datetime64[ns]');pos=np.searchsorted(arr,times,side='left');exists=pos<len(arr)
  delta=np.full(len(times),np.nan);delta[exists]=(arr[pos[exists]]-times[exists])/np.timedelta64(1,'h')
  failed.loc[idx,'RecoveryHours']=delta;failed.loc[idx,'Recovered24h']=(delta>=0)&(delta<=24)
 recovery=failed.groupby('user_id').Recovered24h.any()
 pending=o[o.status=='pending'].copy();pending['AgeDays']=(cutoff-pending.created_at).dt.total_seconds()/86400
 status=pd.DataFrame([dict(Status=k,Orders=int(v),Share=float(v/len(o))) for k,v in o.status.value_counts().items()]);status.to_csv(out/'order_statuses.csv',index=False)
 sm=s.merge(o[['id','status','user_id','updated_at']],left_on='order_id',right_on='id',how='left',suffixes=('_sub','_order'),validate='many_to_one')
 gap=(sm.created_at-sm.updated_at).dt.total_seconds()
 trialdays=buyers.loc[buyers.TrialBefore,'TrialDays']
 quality={'users':len(u),'orders':len(o),'subscriptions':len(s),'transactions':len(t),'orders_user_not_found':int((~o.user_id.isin(u.id)).sum()),'subscriptions_order_not_found':int((~s.order_id.isin(o.id)).sum()),'subscriptions_duplicate_order_rows':int(s.order_id.duplicated().sum()),'subscription_user_mismatch':int((sm.user_id_sub!=sm.user_id_order).sum()),'transactions_order_not_found':int((~t.order_id.isin(o.id)).sum()),'subscriptions_by_order_status':sm.status.value_counts(dropna=False).to_dict(),'paid_zero_amount_orders':int(((o.status=='paid')&(o.amount==0)).sum()),'paid_positive_without_completed_transaction':int(((o.status=='paid')&(o.amount>0)&~o.id.isin(confirmed)).sum()),'subscription_creation_minus_order_update_median_sec':gap.median(),'negative_registration_purchase_intervals':int((buyers.RegistrationDays<0).sum()),'first_order':str(o.created_at.min()),'last_order':str(o.created_at.max()),'cutoff_for_mature_cohorts':str(cutoff)}
 (out/'data_quality.json').write_text(json.dumps(quality,ensure_ascii=False,indent=2),encoding='utf-8')
 summary={'quality':quality,'confirmed_nontrial_orders':len(buys),'confirmed_trial_orders':len(trials),'purchase_users':len(buyers),'trial_users':len(firsttrial),'prior_trial_order_users':int(buyers.TrialBefore.sum()),'prior_trial_any_users':int(buyers.AnyTrialBefore.sum()),'no_prior_trial_users':int((~buyers.AnyTrialBefore).sum()),'never_trial_in_dump_users':int((buyers.FirstTrial.isna()&buyers.trial_used_at.isna()).sum()),'nontrial_buyers_after_trial_launch':int((buyers.FirstPurchase>=firsttrial.min()).sum()),'no_prior_trial_after_trial_launch':int(((buyers.FirstPurchase>=firsttrial.min())&~buyers.AnyTrialBefore).sum()),'trial_delay_days_quantiles':{str(q):float(trialdays.quantile(q)) for q in [.1,.25,.5,.75,.9,.95]},'purchase_before_72h_users':int((trialdays<3).sum()),'purchase_72h_to_73h_users':int(((trialdays>=3)&(trialdays<3+1/24)).sum()),'same_user_multiple_trials':int((trials.groupby('user_id').size()>1).sum()),'conversion':conversion,'failed_orders_with_complete_24h':len(failed),'failed_orders_recovered_24h':int(failed.Recovered24h.sum()),'users_with_failed_orders':len(recovery),'users_with_failure_then_success_24h':int(recovery.sum()),'pending_older_30d':int((pending.AgeDays>=30).sum()),'buyerstats':buyerstats}
 summary['fixed_cohort_conversion']=fixed_conversion
 summary['negative_registration_below_minus3h']=int((buyers.RegistrationDays < -3/24).sum())
 marker=firsttrial.to_frame('Trial').join(profiles.trial_used_at)
 summary['trial_marker_minus_paid_median_seconds']=float((marker.trial_used_at-marker.Trial).dt.total_seconds().median())

 if (r/'devices.csv').exists():
  dev=load(r,'devices',['id','user_id','device_identifier','device_name','client_type','os','brand'])
  raw_count=len(dev);dev.device_identifier=dev.device_identifier.astype('string').str.strip().str.lower()
  validid=dev.device_identifier.notna()&~dev.device_identifier.isin(['','null','unknown','none','0','00000000-0000-0000-0000-000000000000'])
  dv=dev.loc[validid].copy();dv['Client']=dv.client_type.fillna('missing').astype(str)
  pairs=dv.drop_duplicates(['Client','device_identifier','user_id'])
  devices_per=pairs.groupby(['Client','device_identifier']).user_id.nunique()
  linked=pairs.merge(firsttrial.rename('TrialAt'),left_on='user_id',right_index=True,how='inner')
  grp=linked.groupby(['Client','device_identifier']);tc=grp.user_id.nunique();shared=tc[tc>=2]
  consecutive_devices=0;consecutive_pairs=0;sharedusers=set();candidateusers=set()
  for key in shared.index:
   g=grp.get_group(key).drop_duplicates('user_id').sort_values('TrialAt');sharedusers.update(g.user_id)
   delta=g.TrialAt.diff().dt.total_seconds()/3600
   hits=delta.between(72,14*24)
   if hits.any():
    consecutive_devices+=1;consecutive_pairs+=int(hits.sum());candidateusers.update(g.user_id)
  summary['devices']={'rows':raw_count,'unique_row_ids':int(dev.id.nunique()),'excluded_identifier_rows':int((~validid).sum()),'unique_device_keys':len(devices_per),'shared_device_keys':int((devices_per>=2).sum()),'max_users_per_device_key':int(devices_per.max()),'keys_with_multiple_trial_accounts':len(shared),'trial_accounts_on_shared_keys':len(sharedusers),'keys_with_consecutive_trials_72h_to_14d':consecutive_devices,'consecutive_trial_pairs':consecutive_pairs,'trial_accounts_on_candidate_keys':len(candidateusers),'client_type_counts':dev.client_type.value_counts(dropna=False).to_dict(),'device_user_not_found':int((~dev.user_id.isin(u.id)).sum())}
  pd.DataFrame([dict(TrialAccounts=int(n),DeviceKeys=int(c)) for n,c in tc.value_counts().sort_index().items()]).to_csv(out/'device_trial_counts.csv',index=False)
  uuid=pairs.device_identifier.str.fullmatch(r'[\da-f]{8}(?:-[\da-f]{4}){3}-[\da-f]{12}',na=False)
  uuidpairs=pairs[uuid].copy();uuidpairs=uuidpairs.merge(devices_per.rename('AllAccounts'),left_on=['Client','device_identifier'],right_index=True)
  uuidlinked=uuidpairs.merge(firsttrial.rename('TrialAt'),left_on='user_id',right_index=True)
  strict=uuidlinked[uuidlinked.AllAccounts<=5]
  sn=strict.groupby(['Client','device_identifier']).user_id.nunique();keys=sn[sn>=2].index
  candidates=[];candidate_users=set()
  for k,g in strict.groupby(['Client','device_identifier']):
   if k not in keys:continue
   g=g.drop_duplicates('user_id').sort_values('TrialAt');delta=g.TrialAt.diff().dt.total_seconds()/3600;hits=delta.between(72,336)
   if hits.any():
    candidate_users.update(g.user_id)
    candidates.append(dict(DeviceKey=hashlib.sha256((str(k[0])+':'+str(k[1])).encode()).hexdigest()[:16],TrialAccounts=len(g),AllAccounts=int(g.AllAccounts.iloc[0]),ConsecutivePairs=int(hits.sum())))
  pd.DataFrame(candidates,columns=['DeviceKey','TrialAccounts','AllAccounts','ConsecutivePairs']).to_csv(out/'repeat_trial_candidates.csv',index=False)
  summary['devices']['uuid_rows']=int(dev.device_identifier.str.fullmatch(r'[\da-f]{8}(?:-[\da-f]{4}){3}-[\da-f]{12}',na=False).sum())
  summary['devices']['build_number_shape_rows']=int(dev.device_identifier.str.contains(r'\d{6}\.\d',na=False).sum())
  summary['devices']['uuid_shared_trial_keys']=int((uuidlinked.groupby(['Client','device_identifier']).user_id.nunique()>=2).sum())
  summary['devices']['uuid_keys_max5accounts_shared_trials']=len(keys)
  summary['devices']['conservative_candidate_keys']=len(candidates)
  summary['devices']['conservative_candidate_trial_accounts']=len(candidate_users)
  top=dev.groupby('device_identifier').agg(Accounts=('user_id','nunique'),Rows=('id','size'),DeviceNames=('device_name','nunique'),Brands=('brand','nunique')).sort_values('Accounts',ascending=False).head(10).reset_index(drop=True)
  top.index=top.index+1;top.to_csv(out/'device_identifier_collisions.csv',index_label='Rank')
 if (r/'sessions.csv').exists():
  parts=[];n=0;orphans=0;missing=0
  for chunk in load(r,'sessions',['user_id','device_id'],chunksize=300000):
   n+=len(chunk);orphans+=int((~chunk.user_id.isin(u.id)).sum());missing+=int(chunk.device_id.isna().sum())
   parts.append(chunk[chunk.device_id.notna()&chunk.user_id.isin(firsttrial.index)].drop_duplicates())
  links=pd.concat(parts).drop_duplicates();card=links.groupby('device_id').user_id.nunique()
  summary['sessions']={'rows':n,'user_not_found':orphans,'missing_device':missing,'trial_users':int(links.user_id.nunique()),'device_rows_with_multiple_trial_users':int((card>=2).sum()),'trial_device_rows':len(card)}
 (out/'analysis_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
 print(json.dumps(summary,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
