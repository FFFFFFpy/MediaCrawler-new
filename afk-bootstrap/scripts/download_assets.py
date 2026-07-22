#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, json, logging, random, re, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
GAMES=("afk-arena","afk-journey")
def sess():
 s=requests.Session(); r=Retry(total=5,backoff_factor=.8,status_forcelist=(408,425,429,500,502,503,504),allowed_methods=frozenset({"GET"}),respect_retry_after_header=True); a=HTTPAdapter(max_retries=r,pool_connections=8,pool_maxsize=8); s.mount("https://",a); s.headers["User-Agent"]="Mozilla/5.0 AFKGlobalSnapshotAssetArchive/1.0"; return s
def safe(url,i): return re.sub(r"[^A-Za-z0-9._-]+","_",Path(urlparse(url).path).name or f"asset-{i}.bin")
def dl(root,t):
 time.sleep(random.uniform(.04,.18)); out=root/t["game"]/"images"/"assets"/t["slug"]/t["kind"]; out.mkdir(parents=True,exist_ok=True); p=out/safe(t["url"],t["i"])
 try:
  q=sess().get(t["url"],timeout=60); q.raise_for_status(); p.write_bytes(q.content); return {**t,"status":"ok","path":p.relative_to(root).as_posix(),"bytes":len(q.content),"sha256":hashlib.sha256(q.content).hexdigest(),"content_type":q.headers.get("content-type")}
 except Exception as e: logging.warning("asset failed %s: %s",t["url"],e); return {**t,"status":"error","error":f"{type(e).__name__}: {e}"}
def main():
 ap=argparse.ArgumentParser(); ap.add_argument("root",type=Path); ap.add_argument("--workers",type=int,default=4); a=ap.parse_args(); tasks=[]; seen=set()
 for g in GAMES:
  for r in json.loads((a.root/g/"heroes.json").read_text(encoding="utf-8")):
   xs=list(r.get("images") or []); u=r.get("primary_image_url")
   if u and not any(x.get("url")==u for x in xs): xs.append({"url":u,"kind":"primary","alt":r.get("name")})
   for i,x in enumerate(xs):
    u=x.get("url"); k=(g,u)
    if not u or not u.startswith("https://www.afk.global/") or k in seen: continue
    seen.add(k); kind=re.sub(r"[^a-z0-9_-]+","-",(x.get("kind") or "asset").lower()).strip("-") or "asset"; tasks.append({"game":g,"slug":r["slug"],"name":r.get("name"),"kind":kind,"url":u,"alt":x.get("alt"),"i":i})
 logging.basicConfig(level=logging.INFO,format="%(levelname)s %(message)s"); results=[]
 with ThreadPoolExecutor(max_workers=max(1,a.workers)) as ex:
  for f in as_completed([ex.submit(dl,a.root,t) for t in tasks]): results.append(f.result())
 results.sort(key=lambda x:(x["game"],x["slug"],x["kind"],x["url"])); ok=sum(x["status"]=="ok" for x in results); m={"asset_count":len(results),"downloaded":ok,"failed":len(results)-ok,"results":results}; (a.root/"asset_manifest.json").write_text(json.dumps(m,ensure_ascii=False,indent=2),encoding="utf-8"); print(json.dumps({k:m[k] for k in ("asset_count","downloaded","failed")},indent=2))
if __name__=="__main__": main()
