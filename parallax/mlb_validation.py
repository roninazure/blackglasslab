"""Chronological, market-independent MLB V1/V2 validation."""
from __future__ import annotations
import json, math
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode
from .mlb import MLB_API, MLBGameFact, V2State, advance_v2_state, game_probability_v1, v2_probability_from_state

def _season_schedule(season: int, transport: Callable[[str], dict[str, Any]]) -> list[dict[str, Any]]:
    payload = transport("/schedule?" + urlencode({"sportId": 1, "startDate": f"{season}-03-20", "endDate": f"{season}-11-01", "hydrate": "team"}))
    return [g for day in payload.get("dates", []) for g in day.get("games", [])]

def _metric(ps: list[float], ys: list[int]) -> dict[str, float | None]:
    if not ps: return {"brier": None, "log_loss": None, "accuracy": None}
    return {"brier": sum((p-y)**2 for p,y in zip(ps,ys))/len(ps), "log_loss": sum(-math.log(p if y else 1-p) for p,y in zip(ps,ys))/len(ps), "accuracy": sum((p >= .5)==bool(y) for p,y in zip(ps,ys))/len(ps)}

def _calibration(ps: list[float], ys: list[int], width: float=.1) -> list[dict[str, float|int]]:
    out=[]
    for bucket in range(math.ceil(1 / width)):
        low=bucket*width; high=min(1.0,(bucket+1)*width); ix=[i for i,p in enumerate(ps) if low <= p < high or (high == 1.0 and low <= p <= high)]
        if ix: out.append({"lower":low,"upper":high,"count":len(ix),"mean_probability":sum(ps[i] for i in ix)/len(ix),"observed_home_rate":sum(ys[i] for i in ix)/len(ix)})
    return out

def _ece(ps: list[float], ys: list[int]) -> float|None:
    return sum(b["count"]/len(ps)*abs(b["mean_probability"]-b["observed_home_rate"]) for b in _calibration(ps,ys)) if ps else None

def _auc(ps: list[float], ys: list[int]) -> float|None:
    pos=[p for p,y in zip(ps,ys) if y]; neg=[p for p,y in zip(ps,ys) if not y]
    return sum((p>n)+.5*(p==n) for p in pos for n in neg)/(len(pos)*len(neg)) if pos and neg else None

def _fit_platt(ps: list[float], ys: list[int]) -> tuple[float,float]:
    if not ps or len(set(ys))<2: return 0.0,1.0
    xs=[math.log(p/(1-p)) for p in ps]; a,b=0.0,1.0
    for _ in range(30):
        g0=g1=h00=h01=h11=0.0
        for x,y in zip(xs,ys):
            q=1/(1+math.exp(-max(-30,min(30,a+b*x)))); w=max(1e-6,q*(1-q)); g0+=q-y; g1+=(q-y)*x; h00+=w; h01+=w*x; h11+=w*x*x
        det=h00*h11-h01*h01
        if det<=1e-12: break
        da=(h11*g0-h01*g1)/det; db=(-h01*g0+h00*g1)/det; a-=da; b=max(.15,min(3.0,b-db))
        if abs(da)+abs(db)<1e-7: break
    return a,b

def _platt(p: float, ab: tuple[float,float]) -> float:
    a,b=ab; x=math.log(p/(1-p)); return max(.05,min(.95,1/(1+math.exp(-max(-30,min(30,a+b*x))))))

def build_cache(seasons: tuple[int,...], transport: Callable[[str], dict[str,Any]]) -> dict[str,Any]:
    rows=[]
    for season in seasons:
        for g in _season_schedule(season,transport):
            if g.get("status",{}).get("abstractGameState") != "Final" or g.get("gameType") != "R": continue
            t=g.get("teams",{}); h=t.get("home",{}); a=t.get("away",{}); ht=h.get("team",{}); at=a.get("team",{})
            if not ht.get("id") or not at.get("id") or "isWinner" not in h: continue
            rows.append({"game_id":str(g["gamePk"]),"season":season,"start_time":g["gameDate"],"home_team":ht.get("name"),"away_team":at.get("name"),"home_id":str(ht["id"]),"away_id":str(at["id"]),"home_runs":int(h.get("score",0)),"away_runs":int(a.get("score",0)),"home_won":bool(h["isWinner"])})
    rows.sort(key=lambda r:(r["start_time"],r["game_id"]))
    return {"source":MLB_API,"seasons":list(seasons),"feature_window":"strictly prior completed official games","features":["online Elo","prior-game win rate","prior-game runs scored/allowed","home field"],"rejected_features":{"starting_pitcher":"historical pregame availability not proven","bullpen":"not cheaply reconstructible without player usage joins","park_weather_lineups_injuries":"pregame timestamps not proven"},"rows":rows}

def _v2_predictions(rows: list[dict[str,Any]]) -> tuple[list[float],list[int]]:
    state=V2State(); ps=[]; ys=[]
    for r in sorted(rows,key=lambda x:(x["start_time"],x["game_id"])):
        normalized={"home_id":r.get("home_id") or r.get("home_team"),"away_id":r.get("away_id") or r.get("away_team"),"home_won":r["home_won"],"home_runs":r.get("home_runs",r.get("home_run_diff_per_game",0)),"away_runs":r.get("away_runs",r.get("away_run_diff_per_game",0))}
        ps.append(v2_probability_from_state(str(normalized["home_id"]),str(normalized["away_id"]),state)); ys.append(int(r["home_won"])); advance_v2_state(state,normalized)
    return ps,ys

def evaluate_cache(cache: dict[str,Any]) -> dict[str,Any]:
    rows=sorted(cache.get("rows",[]),key=lambda r:(r["start_time"],r["game_id"])); raw,ys=_v2_predictions(rows); train=[i for i,r in enumerate(rows) if int(r["season"])==2024]; ab=_fit_platt([raw[i] for i in train],[ys[i] for i in train]); v2=[_platt(p,ab) if int(r["season"])>=2025 else p for p,r in zip(raw,rows)]
    seasons=sorted({int(r["season"]) for r in rows}); v1=[]
    for r in rows:
        def agg(team):
            prior=[q for q in rows if int(q["season"])==int(r["season"])-1 and team in {q["home_team"],q["away_team"]}]
            if not prior:return .5,0.
            w=sum(int((q["home_team"]==team)==q["home_won"]) for q in prior)/len(prior); d=sum(((q.get("home_runs",0)-q.get("away_runs",0)) if q["home_team"]==team else q.get("away_runs",0)-q.get("home_runs",0)) for q in prior)/len(prior); return w,d
        hw,hd=agg(r["home_team"]); aw,ad=agg(r["away_team"]); v1.append(game_probability_v1(MLBGameFact(r["game_id"],r["start_time"][:10],r["start_time"],r["home_team"],r["away_team"],hw,aw,hd,ad,home_won=r["home_won"])))
    reports={}
    for s in seasons:
        ix=[i for i,r in enumerate(rows) if int(r["season"])==s]; prior=[r for r in rows if int(r["season"])<s]; br=sum(int(r["home_won"]) for r in prior)/len(prior) if prior else .5; y=[ys[i] for i in ix]; p=[v2[i] for i in ix]; reports[str(s)]={"predictions":len(ix),"baseline":_metric([br]*len(ix),y),"v1":_metric([v1[i] for i in ix],y),"v2":_metric(p,y),"v2_calibration":_calibration(p,y),"v2_ece":_ece(p,y),"v2_auc":_auc(p,y),"v2_distribution":{"min":min(p),"max":max(p),"mean":sum(p)/len(p),"bands":{label:sum(1 for x in p if lo<=x<hi)/len(p) for label,lo,hi in (("50-55%",.5,.55),("55-60%",.55,.6),("60-70%",.6,.7),(">70%",.7,1.01))}}}
    final=reports.get("2025",{}) or next(iter(reports.values()),{}); return {"model_version":"mlb-pregame-elo-v2","predictions":len(rows),"season_reports":reports,"baseline":final.get("baseline"),"model":final.get("v2"),"v1":final.get("v1"),"v2_calibration":final.get("v2_calibration"),"model_calibration":final.get("v2_calibration"),"v2_ece":final.get("v2_ece"),"v2_auc":final.get("v2_auc"),"probability_distribution":final.get("v2_distribution"),"calibrator_training_season":2024}

def write_cache(cache: dict[str,Any], path: str|Path) -> None:
    Path(path).write_text(json.dumps(cache,separators=(",",":"),sort_keys=True)+"\n",encoding="utf-8")
