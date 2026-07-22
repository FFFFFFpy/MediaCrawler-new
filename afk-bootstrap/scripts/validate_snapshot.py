#!/usr/bin/env python3
import argparse,json,sys
from pathlib import Path
MIN={"afk-arena":240,"afk-journey":110}
def main():
 a=argparse.ArgumentParser(); a.add_argument("root",type=Path); r=a.parse_args().root; e=[]; total=0
 for g,n in MIN.items():
  p=r/g/"heroes.json"
  if not p.exists(): e.append(f"missing {p}"); continue
  xs=json.loads(p.read_text(encoding="utf-8")); total+=len(xs)
  if len(xs)<n:e.append(f"{g}: {len(xs)} < {n}")
  keys=[(x.get("game"),x.get("slug")) for x in xs]
  if len(keys)!=len(set(keys)):e.append(f"{g}: duplicate keys")
  for x in xs:
   if not x.get("name"):e.append(f"{g}/{x.get('slug')}: no name")
   if not (x.get("source") or {}).get("url"):e.append(f"{g}/{x.get('slug')}: no source")
   if x.get("record_type")!="template" and not x.get("skills"):e.append(f"{g}/{x.get('slug')}: no skills")
  t=[x.get("slug") for x in xs if x.get("record_type")=="template"]
  if g=="afk-arena" and t!=["hero-template"]:e.append(f"arena templates: {t}")
  if g=="afk-journey" and t:e.append(f"journey templates: {t}")
 q=json.loads((r/"quality_report.json").read_text(encoding="utf-8"))
 if q.get("detail_page_failures")!=0:e.append(f"detail failures: {q.get('detail_page_failures')}")
 if q.get("record_count")!=total:e.append("record count mismatch")
 if e: print("\n".join("ERROR: "+x for x in e),file=sys.stderr); raise SystemExit(1)
 print(f"Validation passed: {total} records")
if __name__=="__main__":main()
