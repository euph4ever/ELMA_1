"""Read nested incident ZIPs; export only derived, de-identified evidence.
An incomplete download can be inspected with --partial, but final reports use a complete ZIP.
"""
import argparse,json,pathlib,struct,zlib,zipfile,io,hashlib,re,collections,datetime

PATTERNS={
 'traffic_limit_disconnect':r'Realtime disconnect received.*DisconnectReason\.trafficLimitReached',
 'server_config_http400':r'Error getting server config:.*status=400',
 'push_services_unavailable':r'Cloud messaging service unavailable: Neither Google Play Services nor HMS',
 'background_fcm_assumption':r'Skip WS local notification: app in background, FCM will notify',
 'auth401':r'Received 401 error for ',
 'request_retry_success':r'Request retry successful',
 'network_reset':r'Connection reset by peer',
}
TIMESTAMP=re.compile(r'20\d\d-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?')
def key(text):return hashlib.sha256(text.encode()).hexdigest()[:16]
def main():
 p=argparse.ArgumentParser();p.add_argument('--archive',type=pathlib.Path,required=True);p.add_argument('--inventory',type=pathlib.Path);p.add_argument('--cache',type=pathlib.Path,default=pathlib.Path('log_cache'));p.add_argument('--output',type=pathlib.Path,default=pathlib.Path('.'));p.add_argument('--partial',action='store_true');args=p.parse_args();args.cache.mkdir(parents=True,exist_ok=True);args.output.mkdir(parents=True,exist_ok=True)
 if args.inventory:
  inventory=json.loads(args.inventory.read_text(encoding='utf-8'))
 else:
  with zipfile.ZipFile(args.archive) as z:
   inventory=[dict(name=i.filename,size=i.file_size,compressed=i.compress_size,offset=i.header_offset,method=i.compress_type,crc=i.CRC) for i in z.infolist()]
 items=[x for x in inventory if x['name'].endswith('.zip')];rx={k:re.compile(v) for k,v in PATTERNS.items()}
 failures=[];records=[];available=args.archive.stat().st_size
 with args.archive.open('rb') as archive:
  for j,item in enumerate(items):
   cache=args.cache/(key(item['name'])+'.json')
   if cache.exists():records.append(json.loads(cache.read_text(encoding='utf-8')));continue
   if item['offset']+item['compressed']+30>available:failures.append(item['name']);continue
   try:
    archive.seek(item['offset']);header=archive.read(30);parts=struct.unpack('<4s5H3L2H',header)
    if parts[0]!=b'PK\x03\x04':raise ValueError('incomplete local header')
    archive.seek(parts[-2]+parts[-1],1);packed=archive.read(item['compressed'])
    raw=zlib.decompress(packed,-15) if item['method']==8 else packed
    if len(raw)!=item['size'] or zlib.crc32(raw)!=item['crc']:raise ValueError('outer CRC mismatch')
    del packed
    inner=zipfile.ZipFile(io.BytesIO(raw));files=[];inner_inventory=[]
    for member in inner.infolist():
     if member.is_dir():continue
     typ='app_log' if member.filename.lower().endswith('.log') else ('native_log' if 'peepster' in member.filename.lower() else ('vpn_access' if 'access' in member.filename.lower() else ('vpn_error' if 'error' in member.filename.lower() else 'other')))
     inner_inventory.append({'type':typ,'bytes':member.file_size})
     if typ!='app_log':continue
     digest=hashlib.sha256();events=[];counts=collections.Counter();line_count=0;platforms=set();versions=set();traffic_fields=collections.Counter();last_time=None
     with inner.open(member) as stream:
      for line_count,line in enumerate(stream,1):
       digest.update(line);text=line.decode('utf-8',errors='replace');match=TIMESTAMP.search(text)
       if match:last_time=match.group()
       if 'Running on Os.' in text:
        plat=re.search(r'Running on Os\.([A-Za-z]+)',text);ver=re.search(r'App version ([\d.]+)',text)
        if plat:platforms.add(plat.group(1))
        if ver:versions.add(ver.group(1))
       for kind,pattern in rx.items():
        if pattern.search(text):counts[kind]+=1;events.append({'kind':kind,'time':last_time,'line':line_count,'file':key(member.filename)})
       for field in ['trafficLimitReached','dailyTraffic','monthlyTraffic','trafficUsed','traffic_used','bytesReceived','bytesSent']:
        if field in text:traffic_fields[field]+=1
     files.append({'sha256':digest.hexdigest(),'file':key(member.filename),'lines':line_count,'platforms':sorted(platforms),'versions':sorted(versions),'events':events,'counts':dict(counts),'traffic_fields':dict(traffic_fields)})
    rec={'archive':key(item['name']),'user_id':item['name'].split('/')[1],'inner_files':inner_inventory,'files':files}
    cache.write_text(json.dumps(rec,ensure_ascii=False),encoding='utf-8');records.append(rec)
    del inner,raw
   except (ValueError,zlib.error,zipfile.BadZipFile,EOFError,struct.error) as e:
    failures.append(item['name'])
    if not args.partial:print('UNREADABLE',key(item['name']),type(e).__name__,flush=True)
   if (j+1)%50==0:print('ARCHIVES',j+1,'/',len(items),'read',len(records),flush=True)
 # Deduplicate exact files and repeated timestamped events across incident exports.
 owners=collections.defaultdict(set)
 for rec in records:
  for file in rec['files']:owners[file['sha256']].add(rec['user_id'])
 ambiguous={h for h,users_for_hash in owners.items() if len(users_for_hash)>1}
 ambiguous_occurrences=0
 file_hashes=set();event_keys=set();events=[];file_types=collections.Counter();file_bytes=collections.Counter();duplicates=0;lines=0;platforms=collections.Counter();versions=collections.Counter();field_counts=collections.Counter()
 for rec in records:
  for item in rec['inner_files']:file_types[item['type']]+=1;file_bytes[item['type']]+=item['bytes']
  for file in rec['files']:
   if file['sha256'] in ambiguous:ambiguous_occurrences+=1;continue
   if file['sha256'] in file_hashes:duplicates+=1;continue
   file_hashes.add(file['sha256']);lines+=file['lines'];platforms.update(file['platforms']);versions.update(file['versions']);field_counts.update(file['traffic_fields'])
   for ev in file['events']:
    ek=(rec['user_id'],ev['time'],ev['kind']) if ev['time'] else (rec['user_id'],file['sha256'],ev['line'],ev['kind'])
    if ek in event_keys:continue
    event_keys.add(ek);events.append(dict(ev,UserKey=key(rec['user_id']),ArchiveKey=rec['archive']))
 counts=collections.Counter(e['kind'] for e in events);users=collections.defaultdict(set);user_events=collections.defaultdict(list)
 for e in events:users[e['kind']].add(e['UserKey']);user_events[e['UserKey']].append(e)
 paired=[];pushpaired=[]
 for user,evs in user_events.items():
  valid=[(datetime.datetime.fromisoformat(e['time']),e) for e in evs if e['time']];valid.sort(key=lambda x:(x[0],x[1]['kind']))
  last_limit=None;had_unavailable=False
  for ts,e in valid:
   if e['kind']=='traffic_limit_disconnect':last_limit=(ts,e)
   if e['kind']=='server_config_http400' and last_limit:
    delta=(ts-last_limit[0]).total_seconds()/60
    if 0<=delta<=120:paired.append(dict(UserKey=user,LimitTime=last_limit[1]['time'],ErrorTime=e['time'],DelayMinutes=delta,LimitArchive=last_limit[1]['ArchiveKey'],ErrorArchive=e['ArchiveKey']))
   if e['kind']=='push_services_unavailable':had_unavailable=True
   if e['kind']=='background_fcm_assumption' and had_unavailable:pushpaired.append(dict(UserKey=user,Time=e['time']))
 import csv
 def write_csv(name,rows,cols):
  with (args.output/name).open('w',encoding='utf-8',newline='') as f:
   w=csv.DictWriter(f,fieldnames=cols);w.writeheader();w.writerows(rows)
 write_csv('log_patterns.csv',[dict(Pattern=k,UniqueTimestampedRecords=counts[k],Users=len(users[k])) for k in PATTERNS],['Pattern','UniqueTimestampedRecords','Users'])
 write_csv('limit_error_sequences.csv',paired,['UserKey','LimitTime','ErrorTime','DelayMinutes','LimitArchive','ErrorArchive'])
 summary={'total_incident_archives':len(items),'read_incident_archives':len(records),'unread_incident_archives':len(failures),'folder_users_total':len(set(x['name'].split('/')[1] for x in items)),'folder_users_read':len(set(x['user_id'] for x in records)),'users_with_app_logs':len(set(x['user_id'] for x in records if x['files'])),'unique_app_log_files':len(file_hashes),'duplicate_app_log_files':duplicates,'unique_app_log_lines':lines,'inner_file_types':dict(file_types),'inner_file_bytes':dict(file_bytes),'patterns':{k:{'records':counts[k],'users':len(users[k])} for k in PATTERNS},'platform_file_counts':dict(platforms),'app_version_file_counts':dict(versions),'traffic_fields':dict(field_counts),'limit_then_config400_records_120min':len(paired),'limit_then_config400_users_120min':len(set(x['UserKey'] for x in paired)),'unavailable_then_background_fcm_records':len(pushpaired),'unavailable_then_background_fcm_users':len(set(x['UserKey'] for x in pushpaired))}
 summary['excluded_ambiguous_file_contents']=len(ambiguous)
 summary['excluded_ambiguous_file_occurrences']=ambiguous_occurrences
 (args.output/'log_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(summary,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
