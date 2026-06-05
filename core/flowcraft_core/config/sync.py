"""Config Export/Import + Workflow Marketplace."""
from __future__ import annotations
import json, logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from flowcraft_core.storage.database import Database
logger = logging.getLogger(__name__)

class ConfigExporter:
    def __init__(self, db: Database, data_dir: Path):
        self.db, self.data_dir = db, Path(data_dir)
    def export_all(self) -> dict:
        return {"version":"1.0","exported_at":datetime.now(timezone.utc).isoformat(),
                "settings":self._export_settings(),"workflows":self._export_workflows()}
    def _export_settings(self) -> dict:
        row=self.db.fetch_one("SELECT value_json FROM settings WHERE key='app_settings'")
        if row: d=json.loads(dict(row)["value_json"]); d.pop("api_key",None); return d
        return {}
    def _export_workflows(self) -> list:
        return [dict(r) for r in self.db.fetch_all("SELECT * FROM workflow_templates WHERE status='active'")]
    def export_to_file(self, path: Path) -> Path:
        path.write_text(json.dumps(self.export_all(),ensure_ascii=False,indent=2),encoding="utf-8")
        return path
    def import_from_file(self, path: Path) -> dict:
        data=json.loads(path.read_text(encoding="utf-8")); stats={"workflows":0,"settings":0}
        for wf in data.get("workflows",[]):
            if not self.db.fetch_one("SELECT id FROM workflow_templates WHERE id=?",(wf.get("id",""),)):
                self.db.insert_json("workflow_templates",wf); stats["workflows"]+=1
        if data.get("settings"):
            curr=self._export_settings(); m={**curr,**data["settings"]}; m.pop("api_key",None)
            self.db.insert_json("settings",{"key":"app_settings","value_json":json.dumps(m,ensure_ascii=False),"updated_at":datetime.now(timezone.utc).isoformat()}); stats["settings"]=1
        return stats

class WorkflowMarketplace:
    def __init__(self, db: Database, data_dir: Path):
        self.db=db; self.dir=Path(data_dir)/"marketplace"; self.dir.mkdir(parents=True,exist_ok=True)
    def publish(self, wid: str) -> dict:
        row=self.db.fetch_one("SELECT * FROM workflow_templates WHERE id=?",(wid,))
        if not row: return {"status":"error","message":"Not found"}
        wf=dict(row); (self.dir/f"{wid}.json").write_text(json.dumps(wf,ensure_ascii=False,indent=2),encoding="utf-8")
        self.db.update("workflow_templates","id",wid,{"status":"published"})
        return {"status":"published","workflow_id":wid}
    def unpublish(self, wid: str) -> dict:
        f=self.dir/f"{wid}.json"
        if f.exists(): f.unlink()
        self.db.update("workflow_templates","id",wid,{"status":"active"})
        return {"status":"unpublished","workflow_id":wid}
    def browse(self, search: str="") -> list[dict]:
        r=[]
        for f in self.dir.glob("*.json"):
            try:
                wf=json.loads(f.read_text(encoding="utf-8"))
                if search and search.lower() not in json.dumps(wf).lower(): continue
                sd=wf.get("steps_json","[]")
                if isinstance(sd,str): sd=json.loads(sd)
                r.append({"id":wf.get("id",""),"name":wf.get("name",""),"description":wf.get("description",""),
                          "author":wf.get("author",""),"version":wf.get("version","1.0.0"),
                          "steps":len(sd) if isinstance(sd,list) else 0,"risk_summary":wf.get("risk_summary","LOW")})
            except: continue
        return r
    def download(self, wid: str) -> dict|None:
        f=self.dir/f"{wid}.json"
        if not f.exists(): return None
        wf=json.loads(f.read_text(encoding="utf-8"))
        if not self.db.fetch_one("SELECT id FROM workflow_templates WHERE id=?",(wid,)):
            self.db.insert_json("workflow_templates",wf)
        return wf
