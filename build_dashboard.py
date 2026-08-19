#!/usr/bin/env python3
"""
Fetches the athlete's Strava activities and regenerates docs/index.html —
a self-contained HTML training dashboard (Chart.js via CDN, no build step).

Requires these environment variables (set as GitHub Actions secrets):
  STRAVA_CLIENT_ID
  STRAVA_CLIENT_SECRET
  STRAVA_REFRESH_TOKEN

Run locally for testing:
  STRAVA_CLIENT_ID=... STRAVA_CLIENT_SECRET=... STRAVA_REFRESH_TOKEN=... \
      python3 build_dashboard.py
"""
import os
import sys
import json
import datetime
from collections import defaultdict
import urllib.request
import urllib.parse

STRAVA_API = "https://www.strava.com/api/v3"


def get_access_token():
    client_id = os.environ["STRAVA_CLIENT_ID"]
    client_secret = os.environ["STRAVA_CLIENT_SECRET"]
    refresh_token = os.environ["STRAVA_REFRESH_TOKEN"]

    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode()

    req = urllib.request.Request(
        "https://www.strava.com/oauth/token", data=data, method="POST"
    )
    with urllib.request.urlopen(req) as resp:
        payload = json.loads(resp.read())
    return payload["access_token"]


def api_get(path, token, params=None):
    url = f"{STRAVA_API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def fetch_all_activities(token, max_pages=10, per_page=100):
    activities = []
    page = 1
    while page <= max_pages:
        batch = api_get(
            "/athlete/activities", token, {"page": page, "per_page": per_page}
        )
        if not batch:
            break
        activities.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
    return activities


def fmt_stat(a):
    sport = a["sport_type"]
    dist_km = a["distance"] / 1000
    minutes = a["moving_time"] / 60
    if sport == "Ride":
        speed = dist_km / (a["moving_time"] / 3600) if a["moving_time"] else 0
        return f"{dist_km:.1f} km \u00b7 {speed:.1f} km/h"
    if sport == "Walk":
        return f"{dist_km:.2f} km \u00b7 {int(minutes)} min"
    if sport == "Run":
        speed = dist_km / (a["moving_time"] / 3600) if a["moving_time"] else 0
        return f"{dist_km:.2f} km \u00b7 {speed:.1f} km/h"
    return f"{int(minutes)} min"


def type_class(sport):
    return {"Ride": "ride", "Walk": "walk", "Run": "run"}.get(sport, "weight")


def build_data(activities, athlete, gear_list):
    by_type = defaultdict(lambda: {"count": 0, "distance": 0, "time": 0, "cal": 0, "elev": 0})
    by_month = defaultdict(lambda: {"count": 0, "distance": 0, "time": 0, "cal": 0})
    by_month_elev = defaultdict(float)
    by_week = defaultdict(int)

    for a in activities:
        sport = a["sport_type"]
        cal = a.get("calories") or 0
        by_type[sport]["count"] += 1
        by_type[sport]["distance"] += a["distance"]
        by_type[sport]["time"] += a["moving_time"]
        by_type[sport]["cal"] += cal
        by_type[sport]["elev"] += a.get("total_elevation_gain") or 0

        start_local = a["start_date_local"]
        month = start_local[:7]
        by_month[month]["count"] += 1
        by_month[month]["distance"] += a["distance"]
        by_month[month]["time"] += a["moving_time"]
        by_month[month]["cal"] += cal
        by_month_elev[month] += a.get("total_elevation_gain") or 0

        d = datetime.date.fromisoformat(start_local[:10])
        y, w, _ = d.isocalendar()
        by_week[f"{y}-w{w}"] += 1

    rides = [a for a in activities if a["sport_type"] == "Ride"]
    rides_sorted = sorted(rides, key=lambda x: x["start_date_local"])

    total_distance_km = sum(a["distance"] for a in activities) / 1000
    total_time_hrs = sum(a["moving_time"] for a in activities) / 3600
    total_cal = sum(a.get("calories") or 0 for a in activities)

    highlights = {}
    if rides:
        longest_ride = max(rides, key=lambda x: x["distance"])
        fastest_ride = max(rides, key=lambda x: x["distance"] / max(x["moving_time"], 1))
        highlights["longest_ride"] = {
            "km": longest_ride["distance"] / 1000,
            "date": longest_ride["start_date_local"][:10],
            "kmh": (longest_ride["distance"] / 1000) / (longest_ride["moving_time"] / 3600),
        }
        highlights["fastest_ride"] = {
            "kmh": (fastest_ride["distance"] / 1000) / (fastest_ride["moving_time"] / 3600),
            "date": fastest_ride["start_date_local"][:10],
            "km": fastest_ride["distance"] / 1000,
        }
    walks = [a for a in activities if a["sport_type"] == "Walk"]
    if walks:
        longest_walk = max(walks, key=lambda x: x["distance"])
        highlights["longest_walk"] = {
            "km": longest_walk["distance"] / 1000,
            "date": longest_walk["start_date_local"][:10],
            "min": longest_walk["moving_time"] / 60,
        }
    if activities:
        most_cal = max(activities, key=lambda x: x.get("calories") or 0)
        highlights["most_cal"] = {
            "cal": most_cal.get("calories") or 0,
            "date": most_cal["start_date_local"][:10],
            "name": most_cal["name"],
        }

    log = []
    for a in sorted(activities, key=lambda x: x["start_date_local"], reverse=True):
        log.append({
            "date": a["start_date_local"][5:10],
            "name": a["name"],
            "type": type_class(a["sport_type"]),
            "stat": fmt_stat(a),
        })

    months_sorted = sorted(by_month.keys())
    weeks_sorted = sorted(by_week.keys())

    return {
        "athlete": athlete,
        "gear": gear_list,
        "total_distance_km": total_distance_km,
        "total_time_hrs": total_time_hrs,
        "total_cal": total_cal,
        "count": len(activities),
        "by_type": by_type,
        "months_sorted": months_sorted,
        "by_month": by_month,
        "by_month_elev": by_month_elev,
        "weeks_sorted": weeks_sorted,
        "by_week": by_week,
        "rides_sorted": rides_sorted,
        "highlights": highlights,
        "log": log,
        "generated_at": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    }


def render_html(d):
    month_labels = json.dumps([m[5:7] for m in d["months_sorted"]])
    month_distance = json.dumps([round(d["by_month"][m]["distance"] / 1000, 1) for m in d["months_sorted"]])
    elev_data = json.dumps([round(d["by_month_elev"][m], 1) for m in d["months_sorted"]])
    week_labels = json.dumps(["w" + w.split("-w")[1] for w in d["weeks_sorted"]])
    week_counts = json.dumps([d["by_week"][w] for w in d["weeks_sorted"]])

    ride_labels = json.dumps([r["start_date_local"][5:10] for r in d["rides_sorted"]])
    ride_distance = json.dumps([round(r["distance"] / 1000, 1) for r in d["rides_sorted"]])
    ride_speed = json.dumps([
        round((r["distance"] / 1000) / (r["moving_time"] / 3600), 1) for r in d["rides_sorted"]
    ])

    log_json = json.dumps(d["log"], ensure_ascii=False)

    bt = d["by_type"]

    def sport_card(key, label):
        v = bt.get(key)
        if not v:
            return f'<div class="sport-card {key.lower()}"><div class="name">{label}</div><div class="primary mono">0</div></div>'
        km = v["distance"] / 1000
        hrs = v["time"] / 3600
        return f'''<div class="sport-card {type_class(key)}">
        <div class="name">{label}</div>
        <div class="primary mono">{km:.1f}<small> km</small></div>
        <div class="secondary mono"><span>{v['count']} act.</span><span>{hrs:.1f}h</span><span>+{v['elev']:.0f}m</span></div>
      </div>'''

    sport_cards = "\n".join([
        sport_card("Ride", "Ride"),
        sport_card("Walk", "Walk"),
        sport_card("Run", "Run"),
        sport_card("WeightTraining", "Weight training"),
    ])

    hl = d["highlights"]
    hl_html = ""
    if "longest_ride" in hl:
        v = hl["longest_ride"]
        hl_html += f'''<div class="highlight-card"><div class="hl-lbl">Longest ride</div>
        <div class="hl-val mono">{v['km']:.1f}<small>km</small></div>
        <div class="hl-sub mono">{v['date']} \u00b7 {v['kmh']:.1f} km/h avg</div></div>'''
    if "fastest_ride" in hl:
        v = hl["fastest_ride"]
        hl_html += f'''<div class="highlight-card"><div class="hl-lbl">Fastest ride</div>
        <div class="hl-val mono">{v['kmh']:.1f}<small>km/h</small></div>
        <div class="hl-sub mono">{v['date']} \u00b7 {v['km']:.1f} km</div></div>'''
    if "longest_walk" in hl:
        v = hl["longest_walk"]
        hl_html += f'''<div class="highlight-card"><div class="hl-lbl">Longest walk</div>
        <div class="hl-val mono">{v['km']:.2f}<small>km</small></div>
        <div class="hl-sub mono">{v['date']} \u00b7 {v['min']:.0f} min</div></div>'''
    if "most_cal" in hl:
        v = hl["most_cal"]
        hl_html += f'''<div class="highlight-card"><div class="hl-lbl">Biggest burn</div>
        <div class="hl-val mono">{v['cal']:.0f}<small>cal</small></div>
        <div class="hl-sub mono">{v['date']} \u00b7 {v['name']}</div></div>'''

    athlete = d["athlete"]
    name = f"{athlete.get('firstname','')} {athlete.get('lastname','')}".strip() or "Athlete"
    city = athlete.get("city") or ""
    country = athlete.get("country") or ""
    location = ", ".join([p for p in [city, country] if p])
    gear_line = ""
    if d["gear"]:
        g = d["gear"][0]
        gear_line = f" \u00b7 {g.get('name','Bike')} \u00b7 {g.get('distance',0)/1000:.1f} km on frame"

    html = HTML_TEMPLATE.format(
        name=name,
        location=location,
        gear_line=gear_line,
        generated_at=d["generated_at"],
        total_distance_km=f"{d['total_distance_km']:.0f}",
        total_time_hrs=f"{d['total_time_hrs']:.1f}",
        total_activities=d["count"],
        total_cal=f"{d['total_cal']/1000:.1f}",
        sport_cards=sport_cards,
        highlight_cards=hl_html,
        month_labels=month_labels,
        month_distance=month_distance,
        elev_data=elev_data,
        week_labels=week_labels,
        week_counts=week_counts,
        ride_labels=ride_labels,
        ride_distance=ride_distance,
        ride_speed=ride_speed,
        log_json=log_json,
    )
    return html


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Training Dashboard \u2014 {name}</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root{{
    --asphalt-0:#ffffff; --asphalt-1:#f7f7f8; --line:#e2e2e5;
    --readout:#fc4c02; --readout-dim:#c93e01; --amber:#fc4c02;
    --sage:#2e2e2e; --run:#0074d9;
    --text-1:#242428; --text-2:#6b6b70; --text-3:#9a9aa0;
  }}
  *{{box-sizing:border-box;}}
  body{{background:var(--asphalt-0);color:var(--text-1);font-family:'Helvetica Neue',Arial,sans-serif;margin:0;padding:0 0 60px 0;}}
  .mono{{font-family:'SFMono-Regular','Consolas',Menlo,monospace;font-variant-numeric:tabular-nums;}}
  .bezel{{max-width:760px;margin:0 auto;padding:28px 20px 18px;border-bottom:1px solid var(--line);}}
  .bezel-top{{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;}}
  .eyebrow{{font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:var(--readout-dim);margin:0 0 6px;}}
  h1{{margin:0;font-size:26px;font-weight:700;}}
  .sub{{margin:4px 0 0;font-size:13px;color:var(--text-2);}}
  .clock{{text-align:right;font-size:12px;color:var(--text-3);}}
  .live-dot{{display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--readout);margin-right:6px;box-shadow:0 0 6px var(--readout);}}
  .readout-strip{{max-width:760px;margin:0 auto;padding:22px 20px 6px;display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--line);border:1px solid var(--line);}}
  .readout{{background:var(--asphalt-1);padding:16px 12px;text-align:center;}}
  .readout .val{{font-size:26px;font-weight:700;color:var(--readout);line-height:1.1;}}
  .readout .val small{{font-size:13px;font-weight:500;color:var(--readout-dim);margin-left:2px;}}
  .readout .lbl{{margin-top:6px;font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--text-3);}}
  section{{max-width:760px;margin:0 auto;padding:34px 20px 0;}}
  .section-head{{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:14px;border-bottom:1px solid var(--line);padding-bottom:8px;}}
  .section-head h2{{font-size:14px;letter-spacing:.1em;text-transform:uppercase;margin:0;font-weight:600;}}
  .section-head .tag{{font-size:11px;color:var(--text-3);}}
  .sport-grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;}}
  .sport-card{{background:var(--asphalt-1);border:1px solid var(--line);border-radius:2px;padding:14px;position:relative;overflow:hidden;}}
  .sport-card::before{{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;}}
  .sport-card.walk::before{{background:var(--sage);}}
  .sport-card.ride::before{{background:var(--amber);}}
  .sport-card.run::before{{background:var(--run);}}
  .sport-card.weight::before{{background:var(--text-2);}}
  .sport-card .name{{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--text-2);margin-bottom:8px;}}
  .sport-card .primary{{font-size:22px;font-weight:700;}}
  .sport-card .primary small{{font-size:12px;color:var(--text-2);font-weight:400;}}
  .sport-card .secondary{{margin-top:6px;font-size:11.5px;color:var(--text-3);display:flex;gap:10px;}}
  .highlight-grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;}}
  .highlight-card{{background:var(--asphalt-1);border:1px solid var(--line);border-radius:2px;padding:14px;}}
  .hl-lbl{{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--text-3);margin-bottom:8px;}}
  .hl-val{{font-size:24px;font-weight:700;color:var(--amber);}}
  .hl-val small{{font-size:12px;font-weight:500;color:var(--text-2);margin-left:2px;}}
  .hl-sub{{margin-top:6px;font-size:11px;color:var(--text-3);}}
  .panel{{background:var(--asphalt-1);border:1px solid var(--line);padding:16px 14px 8px;border-radius:2px;}}
  .panel-note{{font-size:12px;color:var(--text-2);margin:0 0 14px;line-height:1.5;}}
  .chart-wrap{{position:relative;height:220px;}}
  .log{{border:1px solid var(--line);}}
  .log-scroll{{max-height:420px;overflow-y:auto;}}
  .log-row{{display:grid;grid-template-columns:64px 1fr auto;gap:10px;align-items:center;padding:10px 12px;border-bottom:1px solid var(--line);font-size:12.5px;}}
  .log-row:last-child{{border-bottom:none;}}
  .log-row .date{{color:var(--text-3);font-size:11px;}}
  .type-pill{{display:inline-block;width:6px;height:6px;border-radius:50%;margin-right:6px;vertical-align:middle;}}
  .type-pill.walk{{background:var(--sage);}}
  .type-pill.ride{{background:var(--amber);}}
  .type-pill.run{{background:var(--run);}}
  .type-pill.weight{{background:var(--text-2);}}
  .log-row .stat{{color:var(--readout-dim);text-align:right;white-space:nowrap;}}
  footer{{max-width:760px;margin:40px auto 0;padding:16px 20px 0;border-top:1px solid var(--line);font-size:11px;color:var(--text-3);display:flex;justify-content:space-between;}}
  @media (max-width:480px){{.sport-grid{{grid-template-columns:1fr 1fr;}}.readout-strip{{grid-template-columns:repeat(2,1fr);}}h1{{font-size:21px;}}}}
</style>
</head>
<body>
  <div class="bezel">
    <div class="bezel-top">
      <div>
        <p class="eyebrow">Training Computer</p>
        <h1>{name}</h1>
        <p class="sub">{location}{gear_line}</p>
      </div>
      <div class="clock mono"><span class="live-dot"></span>Updated {generated_at}</div>
    </div>
  </div>

  <div class="readout-strip">
    <div class="readout"><div class="val mono">{total_distance_km}<small>km</small></div><div class="lbl">Total distance</div></div>
    <div class="readout"><div class="val mono">{total_time_hrs}<small>hrs</small></div><div class="lbl">Time in motion</div></div>
    <div class="readout"><div class="val mono">{total_activities}</div><div class="lbl">Activities</div></div>
    <div class="readout"><div class="val mono">{total_cal}<small>k</small></div><div class="lbl">Calories burned</div></div>
  </div>

  <section>
    <div class="section-head"><h2>By discipline</h2><span class="tag mono">all-time</span></div>
    <div class="sport-grid">{sport_cards}</div>
  </section>

  <section>
    <div class="section-head"><h2>Highlights</h2><span class="tag mono">bests on record</span></div>
    <div class="highlight-grid">{highlight_cards}</div>
  </section>

  <section>
    <div class="section-head"><h2>Weekly consistency</h2><span class="tag mono">activities per week</span></div>
    <div class="panel"><div class="chart-wrap" style="height:160px;"><canvas id="weeklyChart"></canvas></div></div>
  </section>

  <section>
    <div class="section-head"><h2>Elevation gain</h2><span class="tag mono">meters climbed, by month</span></div>
    <div class="panel"><div class="chart-wrap"><canvas id="elevChart"></canvas></div></div>
  </section>

  <section>
    <div class="section-head"><h2>Monthly volume</h2><span class="tag mono">distance, km</span></div>
    <div class="panel"><div class="chart-wrap"><canvas id="monthlyChart"></canvas></div></div>
  </section>

  <section>
    <div class="section-head"><h2>Ride progression</h2><span class="tag mono">distance &amp; speed</span></div>
    <div class="panel"><div class="chart-wrap"><canvas id="rideChart"></canvas></div></div>
  </section>

  <section>
    <div class="section-head"><h2>Full log</h2><span class="tag mono">all activities</span></div>
    <div class="log mono log-scroll" id="logList"></div>
  </section>

  <footer class="mono"><span>SRC: STRAVA</span><span>AUTO-SYNCED</span></footer>

<script>
Chart.defaults.color = '#6b6b70';
Chart.defaults.font.family = "'SFMono-Regular','Consolas',monospace";
Chart.defaults.font.size = 11;

new Chart(document.getElementById('monthlyChart'), {{
  type:'bar',
  data:{{labels:{month_labels}, datasets:[{{data:{month_distance}, backgroundColor:'#fc4c02', borderRadius:1, maxBarThickness:28}}]}},
  options:{{responsive:true, maintainAspectRatio:false, plugins:{{legend:{{display:false}}}},
    scales:{{x:{{grid:{{display:false}}, border:{{color:'#d5d5d9'}}}}, y:{{grid:{{color:'#ececee'}}, border:{{display:false}}, title:{{display:true,text:'km',color:'#9a9aa0'}}}}}}}}
}});

new Chart(document.getElementById('rideChart'), {{
  type:'line',
  data:{{labels:{ride_labels}, datasets:[
    {{label:'Distance (km)', data:{ride_distance}, borderColor:'#fc4c02', backgroundColor:'rgba(252,76,2,0.08)', yAxisID:'y', tension:0.3, pointRadius:3, fill:true}},
    {{label:'Avg speed (km/h)', data:{ride_speed}, borderColor:'#2e2e2e', backgroundColor:'transparent', yAxisID:'y1', tension:0.3, pointRadius:3, borderDash:[4,3]}}
  ]}},
  options:{{responsive:true, maintainAspectRatio:false, interaction:{{mode:'index',intersect:false}},
    plugins:{{legend:{{display:true, position:'top', align:'end', labels:{{boxWidth:8,boxHeight:8,usePointStyle:true,padding:14}}}}}},
    scales:{{
      x:{{grid:{{display:false}}, border:{{color:'#d5d5d9'}}}},
      y:{{position:'left', grid:{{color:'#ececee'}}, border:{{display:false}}, title:{{display:true,text:'km',color:'#9a9aa0'}}}},
      y1:{{position:'right', grid:{{display:false}}, border:{{display:false}}, title:{{display:true,text:'km/h',color:'#9a9aa0'}}}}
    }}}}
}});

new Chart(document.getElementById('weeklyChart'), {{
  type:'bar',
  data:{{labels:{week_labels}, datasets:[{{data:{week_counts}, backgroundColor:'#2e2e2e', borderRadius:1, maxBarThickness:16}}]}},
  options:{{responsive:true, maintainAspectRatio:false, plugins:{{legend:{{display:false}}}},
    scales:{{x:{{grid:{{display:false}}, border:{{color:'#d5d5d9'}}, ticks:{{maxRotation:0, autoSkip:true, maxTicksLimit:13}}}}, y:{{grid:{{color:'#ececee'}}, border:{{display:false}}, title:{{display:true,text:'activities',color:'#9a9aa0'}}, ticks:{{stepSize:1}}}}}}}}
}});

new Chart(document.getElementById('elevChart'), {{
  type:'line',
  data:{{labels:{month_labels}, datasets:[{{data:{elev_data}, borderColor:'#0074d9', backgroundColor:'rgba(0,116,217,0.08)', tension:0.3, pointRadius:3, fill:true}}]}},
  options:{{responsive:true, maintainAspectRatio:false, plugins:{{legend:{{display:false}}}},
    scales:{{x:{{grid:{{display:false}}, border:{{color:'#d5d5d9'}}}}, y:{{grid:{{color:'#ececee'}}, border:{{display:false}}, title:{{display:true,text:'meters',color:'#9a9aa0'}}}}}}}}
}});

const log = {log_json};
document.getElementById('logList').innerHTML = log.map(r => `
  <div class="log-row">
    <div class="date">${{r.date}}</div>
    <div class="what"><span class="type-pill ${{r.type}}"></span>${{r.name}}</div>
    <div class="stat">${{r.stat}}</div>
  </div>
`).join('');
</script>
</body>
</html>
"""


def main():
    try:
        token = get_access_token()
        athlete = api_get("/athlete", token)
        activities = fetch_all_activities(token)
        try:
            gear_list = []
            for g in athlete.get("bikes", []):
                gid = g["id"]
                gear_list.append(api_get(f"/gear/{gid}", token))
        except Exception:
            gear_list = []

        data = build_data(activities, athlete, gear_list)
        html = render_html(data)

        os.makedirs("docs", exist_ok=True)
        with open("docs/index.html", "w", encoding="utf-8") as f:
            f.write(html)

        print(f"Wrote docs/index.html with {len(activities)} activities.")
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
