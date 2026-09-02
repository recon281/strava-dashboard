#!/usr/bin/env python3
"""
Fetches Strava activities and regenerates docs/index.html + docs/manifest.json.

Features:
  - installable PWA (home-screen app on iPhone)
  - personal records
  - repeated-route detection and progression
  - dark / light theme toggle
  - achievement badges

Env vars required (set as GitHub Actions secrets):
  STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN
"""
import os
import sys
import json
import datetime
from collections import defaultdict
import urllib.request
import urllib.parse

STRAVA_API = "https://www.strava.com/api/v3"


# ----------------------------------------------------------------- API

def get_access_token():
    data = urllib.parse.urlencode({
        "client_id": os.environ["STRAVA_CLIENT_ID"],
        "client_secret": os.environ["STRAVA_CLIENT_SECRET"],
        "refresh_token": os.environ["STRAVA_REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request("https://www.strava.com/oauth/token",
                                 data=data, method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())["access_token"]


def api_get(path, token, params=None):
    url = f"{STRAVA_API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def fetch_all_activities(token, max_pages=10, per_page=100):
    out, page = [], 1
    while page <= max_pages:
        batch = api_get("/athlete/activities", token,
                        {"page": page, "per_page": per_page})
        if not batch:
            break
        out.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
    return out


# ----------------------------------------------------------------- helpers

def kmh(a):
    return (a["distance"] / 1000) / (a["moving_time"] / 3600) if a["moving_time"] else 0


def type_class(sport):
    return {"Ride": "ride", "Walk": "walk", "Run": "run"}.get(sport, "weight")


def fmt_stat(a):
    sport = a["sport_type"]
    dist_km = a["distance"] / 1000
    minutes = a["moving_time"] / 60
    if sport in ("Ride", "Run"):
        return f"{dist_km:.1f} km \u00b7 {kmh(a):.1f} km/h"
    if sport == "Walk":
        return f"{dist_km:.2f} km \u00b7 {int(minutes)} min"
    return f"{int(minutes)} min"


# ----------------------------------------------------------------- analysis

def compute_records(activities):
    """Personal bests, per discipline."""
    recs = []
    rides = [a for a in activities if a["sport_type"] == "Ride"]
    runs = [a for a in activities if a["sport_type"] == "Run"]
    walks = [a for a in activities if a["sport_type"] == "Walk"]

    if rides:
        longest = max(rides, key=lambda x: x["distance"])
        recs.append(("Najdlhšia jazda", f"{longest['distance']/1000:.1f}",
                     "km", longest["start_date_local"][:10]))

        climb = max(rides, key=lambda x: x.get("total_elevation_gain") or 0)
        if (climb.get("total_elevation_gain") or 0) > 0:
            recs.append(("Najväčšie stúpanie", f"{climb['total_elevation_gain']:.0f}",
                         "m", climb["start_date_local"][:10]))

        # fastest average, gated by distance so short sprints don't win
        for threshold, label in ((40, "40 km+"), (20, "20 km+"), (10, "10 km+")):
            pool = [a for a in rides if a["distance"] / 1000 >= threshold]
            if pool:
                best = max(pool, key=kmh)
                recs.append((f"Najrýchlejší priemer ({label})", f"{kmh(best):.1f}",
                             "km/h", best["start_date_local"][:10]))
                break

        longest_time = max(rides, key=lambda x: x["moving_time"])
        recs.append(("Najdlhší čas v sedle",
                     f"{longest_time['moving_time']/3600:.1f}", "h",
                     longest_time["start_date_local"][:10]))

    if runs:
        best_run = max(runs, key=lambda x: x["distance"])
        recs.append(("Najdlhší beh", f"{best_run['distance']/1000:.2f}",
                     "km", best_run["start_date_local"][:10]))

    if walks:
        best_walk = max(walks, key=lambda x: x["distance"])
        recs.append(("Najdlhšia prechádzka", f"{best_walk['distance']/1000:.2f}",
                     "km", best_walk["start_date_local"][:10]))

    if activities:
        burn = max(activities, key=lambda x: x.get("calories") or 0)
        if (burn.get("calories") or 0) > 0:
            recs.append(("Najväčší výdaj", f"{burn['calories']:.0f}",
                         "cal", burn["start_date_local"][:10]))

    return recs


def compute_routes(activities, bucket_km=5, min_repeats=2):
    """
    Strava's activity list doesn't expose route names, so repeated routes are
    inferred from distance: rides falling in the same N-km bucket are treated
    as the same loop. Good enough when you ride a handful of regular routes.
    """
    rides = [a for a in activities if a["sport_type"] == "Ride"]
    buckets = defaultdict(list)
    for a in rides:
        key = int((a["distance"] / 1000) // bucket_km) * bucket_km
        buckets[key].append(a)

    routes = []
    for key, group in buckets.items():
        if len(group) < min_repeats:
            continue
        group.sort(key=lambda x: x["start_date_local"])
        speeds = [kmh(a) for a in group]
        routes.append({
            "label": f"~{key}-{key + bucket_km} km",
            "count": len(group),
            "best": max(speeds),
            "first": speeds[0],
            "last": speeds[-1],
            "delta": speeds[-1] - speeds[0],
            "last_date": group[-1]["start_date_local"][:10],
        })
    routes.sort(key=lambda r: -r["count"])
    return routes


def compute_advice(activities, today=None):
    """
    Rule-based training suggestions derived from recent activity.

    Deliberately conservative: no heart-rate or power data is available from
    the activity list, so everything here keys off frequency, volume, recency
    and the spread of average speeds. Each item is (tone, title, text).
    Tone drives the colour: 'push' = do more, 'rest' = back off, 'info' = neutral.
    """
    today = today or datetime.date.today()
    advice = []
    volume_jumped = False
    returning = False

    def plural_rides(n):
        if n == 1:
            return "1 jazdu"
        if 2 <= n <= 4:
            return f"{n} jazdy"
        return f"{n} jázd"

    rides = sorted([a for a in activities if a["sport_type"] == "Ride"],
                   key=lambda x: x["start_date_local"])
    all_sorted = sorted(activities, key=lambda x: x["start_date_local"])
    if not all_sorted:
        return [("info", "Zatiaľ žiadne dáta",
                 "Keď pribudnú prvé aktivity, objavia sa tu konkrétne odporúčania.")]

    def days_ago(a):
        return (today - datetime.date.fromisoformat(a["start_date_local"][:10])).days

    def in_window(pool, start, end):
        return [a for a in pool if start <= days_ago(a) < end]

    # ---------- recency ----------
    last_any = days_ago(all_sorted[-1])
    last_ride = days_ago(rides[-1]) if rides else None

    if last_any >= 14:
        returning = True
        advice.append(("push", "Dlhšia pauza",
                       f"Posledná aktivita bola pred {last_any} dňami. Po takejto prestávke "
                       "začni kratšie a voľnejšie — jedna pokojná jazda do 45 minút, "
                       "až potom sa vracaj k obvyklému objemu."))
    elif last_ride is not None and last_ride >= 10:
        advice.append(("push", "Čas sadnúť na bicykel",
                       f"Od poslednej jazdy ubehlo {last_ride} dní. Forma na bicykli klesá "
                       "rýchlejšie než pri chôdzi — aj krátka jazda ju udrží."))
    elif last_any <= 1:
        advice.append(("rest", "Čerstvá záťaž",
                       "Trénoval si v posledných 24 hodinách. Ak bola záťaž vyššia, "
                       "dnes stačí voľná prechádzka alebo úplné voľno."))

    # ---------- frequency ----------
    rides_4w = in_window(rides, 0, 28)
    rides_prev_4w = in_window(rides, 28, 56)
    per_week = len(rides_4w) / 4

    if rides:
        if per_week < 1:
            advice.append(("push", "Pridaj frekvenciu",
                           f"Za posledné 4 týždne máš {plural_rides(len(rides_4w))} (~{per_week:.1f}/týždeň). "
                           "Pravidelnosť zlepšuje formu viac než jednotlivé dlhé výjazdy — "
                           "cieľ sú 2 jazdy týždenne, aj keby mali byť kratšie."))
        elif per_week < 2:
            advice.append(("push", "Blízko k dobrému rytmu",
                           f"~{per_week:.1f} jazdy týždenne za posledný mesiac. Tretia kratšia "
                           "jazda v týždni by bola najväčší jednotlivý posun, aký teraz vieš spraviť."))
        else:
            advice.append(("info", "Dobrá frekvencia",
                           f"~{per_week:.1f} jazdy týždenne za posledný mesiac. Toto tempo drž — "
                           "teraz už dáva zmysel riešiť skôr obsah tréningov než ich počet."))

    # ---------- volume trend ----------
    km_4w = sum(a["distance"] for a in rides_4w) / 1000
    km_prev = sum(a["distance"] for a in rides_prev_4w) / 1000
    if km_prev > 5 and km_4w > 5:
        change = (km_4w - km_prev) / km_prev * 100
        if change > 50:
            volume_jumped = True
            advice.append(("rest", "Rýchly nárast objemu",
                           f"Objem stúpol o {change:.0f}% oproti predchádzajúcim 4 týždňom "
                           f"({km_prev:.0f} → {km_4w:.0f} km). Ďalší mesiac radšej udrž súčasnú "
                           "úroveň — skoky nad ~30% za mesiac zvyšujú riziko preťaženia."))
        elif change < -40:
            advice.append(("push", "Objem klesol",
                           f"Za posledné 4 týždne {km_4w:.0f} km oproti {km_prev:.0f} km predtým. "
                           "Ak to nebolo zámerné voľno, vráť sa najprv na predošlú úroveň, "
                           "až potom pridávaj."))

    # ---------- intensity spread ----------
    if len(rides_4w) >= 3:
        speeds = [kmh(a) for a in rides_4w]
        spread = max(speeds) - min(speeds)
        if spread < 2.5:
            advice.append(("push", "Skús intervaly",
                           f"Priemerné rýchlosti posledných jázd sú si veľmi podobné "
                           f"(rozptyl {spread:.1f} km/h). Telo si zvykne a progres sa spomalí. "
                           "Zaraď raz týždenne 4–6× 3 minúty naplno / 2 minúty voľne."))

    # ---------- long ride ----------
    if rides:
        longest_ever = max(a["distance"] for a in rides) / 1000
        longest_recent = max((a["distance"] for a in rides_4w), default=0) / 1000
        if rides_4w and longest_recent < longest_ever * 0.6:
            advice.append(("push", "Chýba dlhá jazda",
                           f"Najdlhšia jazda za posledný mesiac má {longest_recent:.0f} km, "
                           f"pričom tvoje maximum je {longest_ever:.0f} km. Jedna dlhšia jazda "
                           "za 1–2 týždne udržiava vytrvalostný základ."))
        elif rides_4w and longest_recent >= longest_ever * 0.95 and longest_recent >= 40:
            advice.append(("rest", "Po dlhej jazde",
                           f"Nedávno si zajazdil {longest_recent:.0f} km — blízko svojmu maximu. "
                           "Po takom výkone daj aspoň jeden úplne voľný deň a nasledujúcu "
                           "jazdu ber pokojne."))

    # ---------- next session ----------
    if rides_4w:
        avg_recent = sum(a["distance"] for a in rides_4w) / len(rides_4w) / 1000
        if returning:
            advice.append(("info", "Návrh na najbližšiu jazdu",
                           f"Tvoj bežný priemer je {avg_recent:.0f} km, ale po pauze naň neskáč hneď. "
                           f"Prvá jazda nech má zhruba {max(avg_recent * 0.5, 15):.0f} km voľným tempom; "
                           "na plný objem sa vráť až po dvoch–troch jazdách."))
        elif volume_jumped:
            advice.append(("info", "Návrh na najbližšiu jazdu",
                           f"Priemer tvojich posledných jázd je {avg_recent:.0f} km. "
                           f"Keďže objem nedávno výrazne stúpol, drž sa teraz okolo "
                           f"{avg_recent:.0f} km a pridávaj až o pár týždňov — "
                           "s kadenciou nad 85 ot./min."))
        else:
            advice.append(("info", "Návrh na najbližšiu jazdu",
                           f"Priemer tvojich posledných jázd je {avg_recent:.0f} km. "
                           f"Rozumný ďalší krok je ~{avg_recent * 1.1:.0f} km pokojným tempom, "
                           "s kadenciou nad 85 ot./min."))
    elif rides:
        advice.append(("info", "Návrh na návrat",
                       "Začni jazdou do 20 km voľným tempom — ide o rozjazdenie, nie o čas."))

    # ---------- variety ----------
    walks_4w = in_window([a for a in activities if a["sport_type"] == "Walk"], 0, 28)
    strength = in_window([a for a in activities
                          if a["sport_type"] in ("WeightTraining", "Workout")], 0, 56)
    if rides_4w and not strength:
        advice.append(("info", "Sila mimo bicykla",
                       "Za posledné 2 mesiace nemáš žiadny silový tréning. Dvakrát týždenne "
                       "10–15 minút na nohy a stred tela pomáha výkonu aj prevencii bolestí chrbta."))
    elif walks_4w and not rides_4w:
        advice.append(("info", "Základ máš",
                       f"Za mesiac {len(walks_4w)} prechádzok — pohybový základ funguje. "
                       "Doplniť k tomu jednu jazdu týždenne by stačilo na viditeľný posun."))

    return advice[:6]


def compute_badges(activities, by_month, by_month_elev, by_week):
    """Earned achievement pills. Each is (label, tier) — tier drives the color."""
    badges = []
    rides = [a for a in activities if a["sport_type"] == "Ride"]

    if rides:
        longest_km = max(a["distance"] for a in rides) / 1000
        for threshold, tier in ((100, "gold"), (75, "gold"), (50, "silver"), (25, "bronze")):
            if longest_km >= threshold:
                badges.append((f"{threshold}+ km jazda", tier))
                break

        long_rides = [a for a in rides if a["distance"] / 1000 >= 20]
        if long_rides:
            top_speed = max(kmh(a) for a in long_rides)
            for threshold, tier in ((28, "gold"), (25, "silver"), (22, "bronze")):
                if top_speed >= threshold:
                    badges.append((f"Priemer {threshold}+ km/h", tier))
                    break

    if by_month:
        peak_month_km = max(v["distance"] for v in by_month.values()) / 1000
        for threshold, tier in ((200, "gold"), (100, "silver"), (50, "bronze")):
            if peak_month_km >= threshold:
                badges.append((f"{threshold}+ km za mesiac", tier))
                break

    if by_month_elev:
        peak_elev = max(by_month_elev.values())
        for threshold, tier in ((1000, "gold"), (500, "silver"), (250, "bronze")):
            if peak_elev >= threshold:
                badges.append((f"{threshold}+ m stúpania / mesiac", tier))
                break

    if by_week:
        busiest = max(by_week.values())
        for threshold, tier in ((6, "gold"), (4, "silver"), (3, "bronze")):
            if busiest >= threshold:
                badges.append((f"{threshold}+ aktivít za týždeň", tier))
                break

        # longest run of consecutive ISO weeks with at least one activity
        weeks = sorted(by_week.keys())
        streak = best_streak = 0
        prev = None
        for w in weeks:
            year, num = int(w.split("-w")[0]), int(w.split("-w")[1])
            if prev and (year, num) in ((prev[0], prev[1] + 1), (prev[0] + 1, 1)):
                streak += 1
            else:
                streak = 1
            best_streak = max(best_streak, streak)
            prev = (year, num)
        for threshold, tier in ((8, "gold"), (4, "silver"), (2, "bronze")):
            if best_streak >= threshold:
                badges.append((f"{best_streak} týždňov v rade", tier))
                break

    total_km = sum(a["distance"] for a in activities) / 1000
    for threshold, tier in ((1000, "gold"), (500, "gold"), (250, "silver"), (100, "bronze")):
        if total_km >= threshold:
            badges.append((f"{threshold}+ km celkovo", tier))
            break

    return badges


def build_data(activities, athlete, gear_list):
    by_type = defaultdict(lambda: {"count": 0, "distance": 0, "time": 0,
                                   "cal": 0, "elev": 0})
    by_month = defaultdict(lambda: {"count": 0, "distance": 0, "time": 0, "cal": 0})
    by_month_elev = defaultdict(float)
    by_week = defaultdict(int)

    for a in activities:
        sport = a["sport_type"]
        cal = a.get("calories") or 0
        elev = a.get("total_elevation_gain") or 0

        by_type[sport]["count"] += 1
        by_type[sport]["distance"] += a["distance"]
        by_type[sport]["time"] += a["moving_time"]
        by_type[sport]["cal"] += cal
        by_type[sport]["elev"] += elev

        month = a["start_date_local"][:7]
        by_month[month]["count"] += 1
        by_month[month]["distance"] += a["distance"]
        by_month[month]["time"] += a["moving_time"]
        by_month[month]["cal"] += cal
        by_month_elev[month] += elev

        d = datetime.date.fromisoformat(a["start_date_local"][:10])
        y, w, _ = d.isocalendar()
        by_week[f"{y}-w{w}"] += 1

    rides_sorted = sorted([a for a in activities if a["sport_type"] == "Ride"],
                          key=lambda x: x["start_date_local"])

    log = [{
        "date": a["start_date_local"][5:10],
        "name": a["name"],
        "type": type_class(a["sport_type"]),
        "stat": fmt_stat(a),
    } for a in sorted(activities, key=lambda x: x["start_date_local"], reverse=True)]

    return {
        "athlete": athlete,
        "gear": gear_list,
        "total_distance_km": sum(a["distance"] for a in activities) / 1000,
        "total_time_hrs": sum(a["moving_time"] for a in activities) / 3600,
        "total_cal": sum(a.get("calories") or 0 for a in activities),
        "count": len(activities),
        "by_type": by_type,
        "months_sorted": sorted(by_month.keys()),
        "by_month": by_month,
        "by_month_elev": by_month_elev,
        "weeks_sorted": sorted(by_week.keys()),
        "by_week": by_week,
        "rides_sorted": rides_sorted,
        "records": compute_records(activities),
        "advice": compute_advice(activities),
        "routes": compute_routes(activities),
        "badges": compute_badges(activities, by_month, by_month_elev, by_week),
        "log": log,
        "generated_at": datetime.datetime.utcnow().strftime("%d.%m.%Y %H:%M UTC"),
    }


# ----------------------------------------------------------------- render

def render_html(d):
    bt = d["by_type"]

    def sport_card(key, label):
        v = bt.get(key)
        if not v:
            return ""
        return (f'<div class="sport-card {type_class(key)}">'
                f'<div class="name">{label}</div>'
                f'<div class="primary mono">{v["distance"]/1000:.1f}<small> km</small></div>'
                f'<div class="secondary mono"><span>{v["count"]} akt.</span>'
                f'<span>{v["time"]/3600:.1f}h</span><span>+{v["elev"]:.0f}m</span></div></div>')

    sport_cards = "".join(sport_card(k, l) for k, l in (
        ("Ride", "Bicykel"), ("Walk", "Chôdza"),
        ("Run", "Beh"), ("WeightTraining", "Posilňovanie")))

    badges_html = "".join(
        f'<span class="badge {tier}">{label}</span>' for label, tier in d["badges"]
    ) or '<span class="badge-empty">Zatiaľ žiadne odznaky.</span>'

    records_html = "".join(
        f'<div class="rec-card"><div class="rec-lbl">{label}</div>'
        f'<div class="rec-val mono">{val}<small>{unit}</small></div>'
        f'<div class="rec-sub mono">{date}</div></div>'
        for label, val, unit, date in d["records"]
    ) or '<div class="rec-empty">Zatiaľ nedostatok dát.</div>'

    if d["routes"]:
        rows = ""
        for r in d["routes"]:
            arrow = "&#8599;" if r["delta"] > 0.2 else ("&#8600;" if r["delta"] < -0.2 else "&#8594;")
            cls = "up" if r["delta"] > 0.2 else ("down" if r["delta"] < -0.2 else "flat")
            rows += (f'<div class="route-row">'
                     f'<div class="route-label">{r["label"]}<span class="route-count">'
                     f'{r["count"]}x</span></div>'
                     f'<div class="route-stats mono">'
                     f'<span class="rs">najlepší <b>{r["best"]:.1f}</b> km/h</span>'
                     f'<span class="rs {cls}">{arrow} {r["delta"]:+.1f} km/h</span></div>'
                     f'</div>')
        routes_html = rows
        routes_note = ("Jazdy sú zoskupené podľa dĺžky – rovnaká skupina zvyčajne znamená "
                       "rovnaký okruh. Šípka ukazuje zmenu priemernej rýchlosti od prvej "
                       "po poslednú jazdu.")
    else:
        routes_html = '<div class="rec-empty">Zatiaľ málo opakovaných trás.</div>'
        routes_note = "Keď zajazdíš rovnako dlhú trasu aspoň dvakrát, objaví sa tu porovnanie."

    athlete = d["athlete"]
    name = f"{athlete.get('firstname','')} {athlete.get('lastname','')}".strip() or "Athlete"
    location = ", ".join(p for p in [athlete.get("city") or "",
                                     athlete.get("country") or ""] if p)
    gear_line = ""
    if d["gear"]:
        g = d["gear"][0]
        gear_line = f" &#183; {g.get('name','Bike')} &#183; {g.get('distance',0)/1000:.0f} km"

    advice_html = "".join(
        f'<div class="adv-card {tone}"><div class="adv-title">{title}</div>'
        f'<div class="adv-text">{text}</div></div>'
        for tone, title, text in d["advice"]
    )

    replacements = {
        "{{NAME}}": name,
        "{{LOCATION}}": location,
        "{{GEAR_LINE}}": gear_line,
        "{{GENERATED_AT}}": d["generated_at"],
        "{{TOTAL_KM}}": f"{d['total_distance_km']:.0f}",
        "{{TOTAL_HRS}}": f"{d['total_time_hrs']:.1f}",
        "{{TOTAL_ACT}}": str(d["count"]),
        "{{TOTAL_CAL}}": f"{d['total_cal']/1000:.1f}",
        "{{SPORT_CARDS}}": sport_cards,
        "{{BADGES}}": badges_html,
        "{{ADVICE}}": advice_html,
        "{{RECORDS}}": records_html,
        "{{ROUTES}}": routes_html,
        "{{ROUTES_NOTE}}": routes_note,
        "{{MONTH_LABELS}}": json.dumps([m[5:7] + "/" + m[2:4] for m in d["months_sorted"]]),
        "{{MONTH_DISTANCE}}": json.dumps(
            [round(d["by_month"][m]["distance"] / 1000, 1) for m in d["months_sorted"]]),
        "{{ELEV_DATA}}": json.dumps(
            [round(d["by_month_elev"][m], 1) for m in d["months_sorted"]]),
        "{{WEEK_LABELS}}": json.dumps(["t" + w.split("-w")[1] for w in d["weeks_sorted"]]),
        "{{WEEK_COUNTS}}": json.dumps([d["by_week"][w] for w in d["weeks_sorted"]]),
        "{{RIDE_LABELS}}": json.dumps(
            [r["start_date_local"][5:10] for r in d["rides_sorted"]]),
        "{{RIDE_DISTANCE}}": json.dumps(
            [round(r["distance"] / 1000, 1) for r in d["rides_sorted"]]),
        "{{RIDE_SPEED}}": json.dumps([round(kmh(r), 1) for r in d["rides_sorted"]]),
        "{{LOG_JSON}}": json.dumps(d["log"], ensure_ascii=False),
    }

    html = HTML_TEMPLATE
    for k, v in replacements.items():
        html = html.replace(k, v)
    return html


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="sk" data-theme="light">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>Tréningový prehľad – {{NAME}}</title>

<link rel="manifest" href="manifest.json">
<meta name="theme-color" content="#fc4c02">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Tréning">
<link rel="apple-touch-icon" href="apple-touch-icon.png">

<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root, [data-theme="light"]{
    --bg:#ffffff; --surface:#f7f7f8; --line:#e2e2e5;
    --brand:#fc4c02; --brand-dim:#c93e01; --charcoal:#2e2e2e; --run:#0074d9;
    --text-1:#242428; --text-2:#6b6b70; --text-3:#9a9aa0;
    --grid:#ececee; --axis:#d5d5d9;
    --gold:#e8a33d; --silver:#9aa0a6; --bronze:#b07a4f;
  }
  [data-theme="dark"]{
    --bg:#101013; --surface:#191920; --line:#2c2c34;
    --brand:#ff5a1a; --brand-dim:#ff8250; --charcoal:#d8d8de; --run:#3a9bf5;
    --text-1:#f2f2f5; --text-2:#a8a8b2; --text-3:#74747f;
    --grid:#24242c; --axis:#3a3a44;
    --gold:#f0b357; --silver:#b4bac0; --bronze:#c78f61;
  }
  *{box-sizing:border-box;}
  body{background:var(--bg);color:var(--text-1);
    font-family:-apple-system,'Helvetica Neue',Arial,sans-serif;margin:0;
    padding:env(safe-area-inset-top) 0 calc(60px + env(safe-area-inset-bottom));
    transition:background .2s,color .2s;-webkit-font-smoothing:antialiased;}
  .mono{font-family:'SFMono-Regular','Consolas',Menlo,monospace;font-variant-numeric:tabular-nums;}

  .bezel{max-width:760px;margin:0 auto;padding:28px 20px 18px;border-bottom:1px solid var(--line);}
  .bezel-top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;}
  .eyebrow{font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:var(--brand-dim);margin:0 0 6px;}
  h1{margin:0;font-size:26px;font-weight:700;}
  .sub{margin:4px 0 0;font-size:13px;color:var(--text-2);}
  .head-right{display:flex;flex-direction:column;align-items:flex-end;gap:10px;}
  .clock{font-size:11px;color:var(--text-3);text-align:right;}
  .live-dot{display:inline-block;width:6px;height:6px;border-radius:50%;
    background:var(--brand);margin-right:6px;box-shadow:0 0 6px var(--brand);}
  .theme-btn{background:var(--surface);border:1px solid var(--line);color:var(--text-2);
    border-radius:999px;padding:6px 12px;font-size:12px;cursor:pointer;
    display:flex;align-items:center;gap:6px;font-family:inherit;}
  .theme-btn:active{transform:scale(.96);}

  .readout-strip{max-width:760px;margin:0 auto;padding:22px 20px 6px;display:grid;
    grid-template-columns:repeat(4,1fr);gap:1px;background:var(--line);border:1px solid var(--line);}
  .readout{background:var(--surface);padding:16px 12px;text-align:center;}
  .readout .val{font-size:26px;font-weight:700;color:var(--brand);line-height:1.1;}
  .readout .val small{font-size:13px;font-weight:500;color:var(--brand-dim);margin-left:2px;}
  .readout .lbl{margin-top:6px;font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--text-3);}

  section{max-width:760px;margin:0 auto;padding:34px 20px 0;}
  .section-head{display:flex;align-items:baseline;justify-content:space-between;
    margin-bottom:14px;border-bottom:1px solid var(--line);padding-bottom:8px;}
  .section-head h2{font-size:14px;letter-spacing:.1em;text-transform:uppercase;margin:0;font-weight:600;}
  .section-head .tag{font-size:11px;color:var(--text-3);}

  .adv-grid{display:grid;gap:10px;}
  .adv-card{background:var(--surface);border:1px solid var(--line);border-radius:2px;
    padding:14px 16px;border-left:4px solid var(--text-3);}
  .adv-card.push{border-left-color:var(--brand);}
  .adv-card.rest{border-left-color:#2eae5c;}
  .adv-card.info{border-left-color:var(--run);}
  .adv-title{font-size:13.5px;font-weight:700;margin-bottom:6px;}
  .adv-text{font-size:13px;line-height:1.55;color:var(--text-2);}
  .adv-note{font-size:11.5px;color:var(--text-3);line-height:1.5;margin:14px 0 0;}

  .badge-wrap{display:flex;flex-wrap:wrap;gap:8px;}
  .badge{font-size:12px;font-weight:600;padding:7px 12px;border-radius:999px;color:#fff;}
  .badge.gold{background:var(--gold);}
  .badge.silver{background:var(--silver);}
  .badge.bronze{background:var(--bronze);}
  .badge-empty,.rec-empty{font-size:12.5px;color:var(--text-3);}

  .sport-grid,.rec-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;}
  .sport-card{background:var(--surface);border:1px solid var(--line);border-radius:2px;
    padding:14px;position:relative;overflow:hidden;}
  .sport-card::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;}
  .sport-card.walk::before{background:var(--charcoal);}
  .sport-card.ride::before{background:var(--brand);}
  .sport-card.run::before{background:var(--run);}
  .sport-card.weight::before{background:var(--text-2);}
  .sport-card .name{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--text-2);margin-bottom:8px;}
  .sport-card .primary{font-size:22px;font-weight:700;}
  .sport-card .primary small{font-size:12px;color:var(--text-2);font-weight:400;}
  .sport-card .secondary{margin-top:6px;font-size:11.5px;color:var(--text-3);display:flex;gap:10px;}

  .rec-card{background:var(--surface);border:1px solid var(--line);border-radius:2px;padding:14px;}
  .rec-lbl{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--text-3);margin-bottom:8px;}
  .rec-val{font-size:23px;font-weight:700;color:var(--brand);}
  .rec-val small{font-size:12px;font-weight:500;color:var(--text-2);margin-left:3px;}
  .rec-sub{margin-top:6px;font-size:11px;color:var(--text-3);}

  .route-row{display:flex;justify-content:space-between;align-items:center;gap:12px;
    padding:12px 14px;background:var(--surface);border:1px solid var(--line);
    border-radius:2px;margin-bottom:8px;flex-wrap:wrap;}
  .route-label{font-size:13.5px;font-weight:600;}
  .route-count{margin-left:8px;font-size:11px;color:#fff;background:var(--brand);
    padding:2px 7px;border-radius:999px;font-weight:600;}
  .route-stats{display:flex;gap:14px;font-size:12px;color:var(--text-2);}
  .route-stats b{color:var(--text-1);}
  .rs.up{color:#2eae5c;} .rs.down{color:#d9534f;} .rs.flat{color:var(--text-3);}

  .panel{background:var(--surface);border:1px solid var(--line);padding:16px 14px 8px;border-radius:2px;}
  .panel-note{font-size:12px;color:var(--text-2);margin:0 0 14px;line-height:1.5;}
  .chart-wrap{position:relative;height:220px;}

  .log{border:1px solid var(--line);}
  .log-scroll{max-height:420px;overflow-y:auto;-webkit-overflow-scrolling:touch;}
  .log-row{display:grid;grid-template-columns:56px 1fr auto;gap:10px;align-items:center;
    padding:10px 12px;border-bottom:1px solid var(--line);font-size:12.5px;}
  .log-row:last-child{border-bottom:none;}
  .log-row .date{color:var(--text-3);font-size:11px;}
  .type-pill{display:inline-block;width:6px;height:6px;border-radius:50%;margin-right:6px;vertical-align:middle;}
  .type-pill.walk{background:var(--charcoal);}
  .type-pill.ride{background:var(--brand);}
  .type-pill.run{background:var(--run);}
  .type-pill.weight{background:var(--text-2);}
  .log-row .stat{color:var(--brand-dim);text-align:right;white-space:nowrap;}

  footer{max-width:760px;margin:40px auto 0;padding:16px 20px 0;border-top:1px solid var(--line);
    font-size:11px;color:var(--text-3);display:flex;justify-content:space-between;}

  @media (max-width:480px){
    .readout-strip{grid-template-columns:repeat(2,1fr);}
    h1{font-size:21px;}
    .route-stats{width:100%;justify-content:space-between;}
  }
</style>
</head>
<body>
  <div class="bezel">
    <div class="bezel-top">
      <div>
        <p class="eyebrow">Tréningový počítač</p>
        <h1>{{NAME}}</h1>
        <p class="sub">{{LOCATION}}{{GEAR_LINE}}</p>
      </div>
      <div class="head-right">
        <button class="theme-btn" id="themeBtn" type="button">
          <span id="themeIcon">&#127769;</span><span id="themeLabel">Tmavý</span>
        </button>
        <div class="clock mono"><span class="live-dot"></span>{{GENERATED_AT}}</div>
      </div>
    </div>
  </div>

  <div class="readout-strip">
    <div class="readout"><div class="val mono">{{TOTAL_KM}}<small>km</small></div><div class="lbl">Celkovo</div></div>
    <div class="readout"><div class="val mono">{{TOTAL_HRS}}<small>h</small></div><div class="lbl">Čas v pohybe</div></div>
    <div class="readout"><div class="val mono">{{TOTAL_ACT}}</div><div class="lbl">Aktivít</div></div>
    <div class="readout"><div class="val mono">{{TOTAL_CAL}}<small>k</small></div><div class="lbl">Kalórií</div></div>
  </div>

  <section>
    <div class="section-head"><h2>Odporúčania</h2><span class="tag mono">podľa posledných aktivít</span></div>
    <div class="adv-grid">{{ADVICE}}</div>
    <p class="adv-note">Odporúčania sú odvodené z frekvencie, objemu a rozptylu rýchlostí tvojich
    aktivít — nie z tepovej frekvencie ani výkonu, tie Strava v prehľade aktivít neposkytuje.
    Ber ich ako všeobecné vodidlo, nie ako plán od trénera.</p>
  </section>

  <section>
    <div class="section-head"><h2>Odznaky</h2><span class="tag mono">získané výkony</span></div>
    <div class="badge-wrap">{{BADGES}}</div>
  </section>

  <section>
    <div class="section-head"><h2>Osobné rekordy</h2><span class="tag mono">najlepšie výkony</span></div>
    <div class="rec-grid">{{RECORDS}}</div>
  </section>

  <section>
    <div class="section-head"><h2>Podľa športu</h2><span class="tag mono">celkovo</span></div>
    <div class="sport-grid">{{SPORT_CARDS}}</div>
  </section>

  <section>
    <div class="section-head"><h2>Opakované trasy</h2><span class="tag mono">progres</span></div>
    <p class="panel-note">{{ROUTES_NOTE}}</p>
    {{ROUTES}}
  </section>

  <section>
    <div class="section-head"><h2>Progres na bicykli</h2><span class="tag mono">vzdialenosť a rýchlosť</span></div>
    <div class="panel"><div class="chart-wrap"><canvas id="rideChart"></canvas></div></div>
  </section>

  <section>
    <div class="section-head"><h2>Mesačný objem</h2><span class="tag mono">km</span></div>
    <div class="panel"><div class="chart-wrap"><canvas id="monthlyChart"></canvas></div></div>
  </section>

  <section>
    <div class="section-head"><h2>Stúpanie</h2><span class="tag mono">metre, po mesiacoch</span></div>
    <div class="panel"><div class="chart-wrap"><canvas id="elevChart"></canvas></div></div>
  </section>

  <section>
    <div class="section-head"><h2>Pravidelnosť</h2><span class="tag mono">aktivít za týždeň</span></div>
    <div class="panel"><div class="chart-wrap" style="height:160px;"><canvas id="weeklyChart"></canvas></div></div>
  </section>

  <section>
    <div class="section-head"><h2>Denník</h2><span class="tag mono">všetky aktivity</span></div>
    <div class="log mono log-scroll" id="logList"></div>
  </section>

  <footer class="mono"><span>ZDROJ: STRAVA</span><span>AUTOMATICKY AKTUALIZOVANÉ</span></footer>

<script>
/* ---------- theme toggle ---------- */
var root = document.documentElement;
var btn = document.getElementById('themeBtn');
var icon = document.getElementById('themeIcon');
var label = document.getElementById('themeLabel');

function applyTheme(t){
  root.setAttribute('data-theme', t);
  icon.textContent = t === 'dark' ? '\u2600\uFE0F' : '\uD83C\uDF19';
  label.textContent = t === 'dark' ? 'Svetlý' : 'Tmavý';
  try { localStorage.setItem('theme', t); } catch(e){}
  if (window.__charts) window.__charts.forEach(function(c){ restyle(c); });
}

var saved = null;
try { saved = localStorage.getItem('theme'); } catch(e){}
if (!saved && window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
  saved = 'dark';
}

btn.addEventListener('click', function(){
  applyTheme(root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark');
});

/* ---------- log (rendered first, so it never depends on the charts) ---------- */
var log = {{LOG_JSON}};
document.getElementById('logList').innerHTML = log.map(function(r){
  return '<div class="log-row">' +
    '<div class="date">' + r.date + '</div>' +
    '<div class="what"><span class="type-pill ' + r.type + '"></span>' + r.name + '</div>' +
    '<div class="stat">' + r.stat + '</div>' +
  '</div>';
}).join('');

/* ---------- charts ---------- */
function cssVar(n){ return getComputedStyle(root).getPropertyValue(n).trim(); }

function restyle(chart){
  if (!chart || !chart.options) return;
  var grid = cssVar('--grid'), axis = cssVar('--axis'), txt = cssVar('--text-2');
  var scales = chart.options.scales || {};
  Object.keys(scales).forEach(function(k){
    var s = scales[k];
    if (!s) return;
    if (s.grid && s.grid.display !== false) s.grid.color = grid;
    if (s.border) s.border.color = axis;
    if (s.title) s.title.color = cssVar('--text-3');
    if (!s.ticks) s.ticks = {};
    s.ticks.color = txt;
  });
  var lg = chart.options.plugins && chart.options.plugins.legend;
  if (lg && lg.display !== false) {
    if (!lg.labels) lg.labels = {};
    lg.labels.color = txt;
  }
  chart.update('none');
}

Chart.defaults.font.family = "'SFMono-Regular','Consolas',monospace";
Chart.defaults.font.size = 11;

function base(yTitle){
  return {
    responsive:true, maintainAspectRatio:false,
    plugins:{legend:{display:false}},
    scales:{
      x:{grid:{display:false}, border:{}},
      y:{grid:{}, border:{display:false}, title:{display:true, text:yTitle}}
    }
  };
}

var rideChart = new Chart(document.getElementById('rideChart'), {
  type:'line',
  data:{labels:{{RIDE_LABELS}}, datasets:[
    {label:'Vzdialenosť (km)', data:{{RIDE_DISTANCE}}, borderColor:'#fc4c02',
     backgroundColor:'rgba(252,76,2,0.08)', yAxisID:'y', tension:0.3, pointRadius:3, fill:true},
    {label:'Priemer (km/h)', data:{{RIDE_SPEED}}, borderColor:'#8a8a95',
     backgroundColor:'transparent', yAxisID:'y1', tension:0.3, pointRadius:3, borderDash:[4,3]}
  ]},
  options:{
    responsive:true, maintainAspectRatio:false,
    interaction:{mode:'index', intersect:false},
    plugins:{legend:{display:true, position:'top', align:'end',
      labels:{boxWidth:8, boxHeight:8, usePointStyle:true, padding:14}}},
    scales:{
      x:{grid:{display:false}, border:{}},
      y:{position:'left', grid:{}, border:{display:false}, title:{display:true, text:'km'}},
      y1:{position:'right', grid:{display:false}, border:{display:false}, title:{display:true, text:'km/h'}}
    }
  }
});

var monthlyChart = new Chart(document.getElementById('monthlyChart'), {
  type:'bar',
  data:{labels:{{MONTH_LABELS}}, datasets:[{data:{{MONTH_DISTANCE}},
    backgroundColor:'#fc4c02', borderRadius:1, maxBarThickness:28}]},
  options: base('km')
});

var elevChart = new Chart(document.getElementById('elevChart'), {
  type:'line',
  data:{labels:{{MONTH_LABELS}}, datasets:[{data:{{ELEV_DATA}}, borderColor:'#0074d9',
    backgroundColor:'rgba(0,116,217,0.08)', tension:0.3, pointRadius:3, fill:true}]},
  options: base('m')
});

var weeklyOpts = base('aktivít');
weeklyOpts.scales.x.ticks = {maxRotation:0, autoSkip:true, maxTicksLimit:13};
weeklyOpts.scales.y.ticks = {stepSize:1};
var weeklyChart = new Chart(document.getElementById('weeklyChart'), {
  type:'bar',
  data:{labels:{{WEEK_LABELS}}, datasets:[{data:{{WEEK_COUNTS}},
    backgroundColor:'#8a8a95', borderRadius:1, maxBarThickness:16}]},
  options: weeklyOpts
});

window.__charts = [rideChart, monthlyChart, elevChart, weeklyChart];
try { applyTheme(saved || 'light'); } catch(e) { console.error('theme:', e); }
</script>
</body>
</html>
"""

MANIFEST = {
    "name": "Tréningový prehľad",
    "short_name": "Tréning",
    "start_url": "./index.html",
    "display": "standalone",
    "background_color": "#ffffff",
    "theme_color": "#fc4c02",
    "icons": [
        {"src": "icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "icon-512.png", "sizes": "512x512", "type": "image/png"},
    ],
}


def main():
    try:
        token = get_access_token()
        athlete = api_get("/athlete", token)
        activities = fetch_all_activities(token)

        gear_list = []
        for g in athlete.get("bikes", []):
            try:
                gear_list.append(api_get(f"/gear/{g['id']}", token))
            except Exception:
                pass

        data = build_data(activities, athlete, gear_list)

        os.makedirs("docs", exist_ok=True)
        with open("docs/index.html", "w", encoding="utf-8") as f:
            f.write(render_html(data))
        with open("docs/manifest.json", "w", encoding="utf-8") as f:
            json.dump(MANIFEST, f, ensure_ascii=False, indent=2)

        print(f"Wrote docs/index.html ({len(activities)} activities), "
              f"{len(data['badges'])} badges, {len(data['records'])} records, "
              f"{len(data['routes'])} repeated routes.")
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
