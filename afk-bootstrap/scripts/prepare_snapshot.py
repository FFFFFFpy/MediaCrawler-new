#!/usr/bin/env python3
from __future__ import annotations
import argparse,csv,json,shutil
from pathlib import Path
GAMES=("afk-arena","afk-journey")
def w(p,v): p.parent.mkdir(parents=True,exist_ok=True); p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
def clean(v):
 if isinstance(v,dict): return {k:clean(x) for k,x in v.items() if k not in {"raw_text_lines","heading_sections","raw_lines"}}
 if isinstance(v,list): return [clean(x) for x in v]
 return v
def main():
 ap=argparse.ArgumentParser(); ap.add_argument("root",type=Path); ap.add_argument("--current",type=Path,default=Path("data/current")); a=ap.parse_args(); old={}
 if (a.current/"all_heroes.json").exists(): old={(x["game"],x["slug"]):x for x in json.loads((a.current/"all_heroes.json").read_text(encoding="utf-8"))}
 if a.current.exists(): shutil.rmtree(a.current)
 allr=[]; games=[]
 for g in GAMES:
  src=a.root/g; dst=a.current/g; rs=[clean(x) for x in json.loads((src/"heroes.json").read_text(encoding="utf-8"))]; allr+=rs; w(dst/"heroes.json",rs); shutil.copy2(src/"heroes.csv",dst/"heroes.csv")
  for n in ("report.json","character_urls.json","failures.json"):
   if (src/n).exists(): shutil.copy2(src/n,dst/n)
  for r in rs: w(dst/"heroes"/f"{r['slug']}.json",r)
  games.append({"game":g,"entries":len(rs),"characters":sum(x.get("record_type")!="template" for x in rs),"templates":[x["slug"] for x in rs if x.get("record_type")=="template"],"with_skills":sum(bool(x.get("skills")) for x in rs)})
 allr.sort(key=lambda x:(x.get("game",""),(x.get("name") or x.get("slug") or "").casefold())); w(a.current/"all_heroes.json",allr)
 with (a.current/"all_heroes.csv").open("w",encoding="utf-8-sig",newline="") as f:
  fs=["game","name","slug","title","record_type","faction","class","type","role","rarity","skill_count","source_url"]; z=csv.DictWriter(f,fieldnames=fs); z.writeheader()
  for r in allr:
   d=r.get("details") or {}; z.writerow({"game":r.get("game"),"name":r.get("name"),"slug":r.get("slug"),"title":r.get("title"),"record_type":r.get("record_type"),"faction":d.get("faction"),"class":d.get("class"),"type":d.get("type"),"role":d.get("role"),"rarity":d.get("rarity"),"skill_count":len(r.get("skills") or []),"source_url":(r.get("source") or {}).get("url")})
 m=json.loads((a.root/"manifest.json").read_text(encoding="utf-8")); date=next((m.get(k," ")[:10] for k in ("completed_at","reparsed_at","started_at") if len(m.get(k,""))>=10),a.root.name[-10:]); m["snapshot_date"]=date; m["repository_view"]={"record_count":len(allr),"games":games}; w(a.current/"manifest.json",m)
 am=json.loads((a.root/"asset_manifest.json").read_text(encoding="utf-8")) if (a.root/"asset_manifest.json").exists() else {"asset_count":0,"downloaded":0,"failed":0,"results":[]}; q={"snapshot_date":date,"record_count":len(allr),"games":games,"detail_page_failures":sum(len(json.loads((a.root/g/"failures.json").read_text(encoding="utf-8"))) for g in GAMES),"assets":{k:am.get(k,0) for k in ("asset_count","downloaded","failed")},"asset_failures":[x for x in am.get("results",[]) if x.get("status")!="ok"],"known_anomalies":["AFK Arena includes hero-template; retained as template and excluded from playable counts.","Source 404 assets are recorded, not fabricated."]}; w(a.current/"quality_report.json",q)
 new={(x["game"],x["slug"]):x for x in allr}; add=sorted(set(new)-set(old)); rem=sorted(set(old)-set(new)); ch=sorted(k for k in set(new)&set(old) if new[k]!=old[k]); w(a.current/"changes.json",{"baseline":not bool(old),"added":[{"game":g,"slug":s} for g,s in add],"removed":[{"game":g,"slug":s} for g,s in rem],"changed":[{"game":g,"slug":s} for g,s in ch],"unchanged":len(set(new)&set(old))-len(ch)})
 print(date)
if __name__=="__main__": main()
