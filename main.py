"""Site feasibility backend: geocode -> route -> land use along route -> score -> chat."""
import asyncio, math, os, random, re, time
from functools import lru_cache

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Nominatim requires an identifying User-Agent: put a real contact here.
UA = {"User-Agent": "site-feasibility/1.0 (" + os.getenv("CONTACT_EMAIL", "set CONTACT_EMAIL env var") + ")"}
NOMINATIM = "https://nominatim.openstreetmap.org/search"
OSRM = "https://router.project-osrm.org/route/v1/driving"
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

RATE_CRORE_PER_KM = {"township": 40, "canal": 25, "highway": 60}  # indicative, tune locally
N_SEGMENTS = 10          # corridor slices analysed
CORRIDOR_M = 250         # half-width of analysed corridor
ROAD_WIDTH_M = 7         # assumed carriageway width for road length -> area

# Tune these to your land-classification policy.
GREEN_LANDUSE = {"forest", "grass", "meadow", "farmland", "orchard", "vineyard",
                 "village_green", "recreation_ground", "greenfield", "allotments"}
GREEN_NATURAL = {"wood", "scrub", "grassland", "wetland", "heath"}
GREEN_LEISURE = {"park", "garden", "nature_reserve"}
COMMERCIAL_LANDUSE = {"commercial", "retail", "industrial"}

app = FastAPI(title="Site Feasibility")


@app.exception_handler(httpx.HTTPError)
async def upstream_error(request, exc):
    """Map/routing services failed (blocked, rate-limited, timed out): return readable JSON."""
    return JSONResponse({"detail": f"External service error: {type(exc).__name__}: {exc}"}, status_code=502)


@app.exception_handler(Exception)
async def server_error(request, exc):
    return JSONResponse({"detail": f"Server error: {type(exc).__name__}: {exc}"}, status_code=500)


class AnalyzeReq(BaseModel):
    start: str
    end: str
    project: str = "township"


class ChatReq(BaseModel):
    question: str
    analysis: dict
    history: list = []


# ---------- geometry helpers ----------
def hav_m(a, b):
    r = math.radians
    h = math.sin(r(b["lat"] - a["lat"]) / 2) ** 2 + math.cos(r(a["lat"])) * math.cos(r(b["lat"])) * math.sin(r(b["lon"] - a["lon"]) / 2) ** 2
    return 12742000 * math.asin(math.sqrt(h))


def area_m2(g):
    lat0 = math.radians(g[0]["lat"])
    pts = [(math.radians(p["lon"]) * 6371000 * math.cos(lat0), math.radians(p["lat"]) * 6371000) for p in g]
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]))) / 2


def length_m(g):
    return sum(hav_m(a, b) for a, b in zip(g, g[1:]))


# ---------- external services ----------
async def geocode(c: httpx.AsyncClient, q: str):
    m = re.match(r"^\s*(-?\d+(\.\d+)?)\s*,\s*(-?\d+(\.\d+)?)\s*$", q)
    if m:
        return float(m[1]), float(m[3]), q
    r = await c.get(NOMINATIM, params={"q": q, "format": "json", "limit": 1}, headers=UA)
    r.raise_for_status()
    d = r.json()
    if not d:
        raise HTTPException(404, f"Place not found: {q}")
    return float(d[0]["lat"]), float(d[0]["lon"]), d[0]["display_name"]


async def get_route(c: httpx.AsyncClient, a, b):
    r = await c.get(f"{OSRM}/{a[1]},{a[0]};{b[1]},{b[0]}", params={"overview": "full", "geometries": "geojson"})
    r.raise_for_status()
    j = r.json()
    if j.get("code") != "Ok":
        raise HTTPException(422, "No drivable route between these points")
    rt = j["routes"][0]
    return rt["distance"] / 1000, [[lat, lon] for lon, lat in rt["geometry"]["coordinates"]]


@lru_cache(maxsize=256)
def _query(pts_key: str) -> str:
    return f"""[out:json][timeout:40];(
way["landuse"](around:{CORRIDOR_M},{pts_key});
way["natural"](around:{CORRIDOR_M},{pts_key});
way["leisure"](around:{CORRIDOR_M},{pts_key});
way["building"](around:{CORRIDOR_M},{pts_key});
way["highway"](around:{CORRIDOR_M},{pts_key});
);out geom;"""


async def land_use(c, sem, seg):
    step = max(1, len(seg) // 8)
    pts = seg[::step] + ([seg[-1]] if seg[-1] not in seg[::step] else [])
    key = ",".join(f"{lat:.5f},{lon:.5f}" for lat, lon in pts)
    r = None
    for url in OVERPASS_MIRRORS:  # try each public mirror until one answers
        try:
            async with sem:
                resp = await c.post(url, data={"data": _query(key)}, headers=UA, timeout=90)
            if resp.status_code == 200:
                r = resp
                break
        except httpx.HTTPError:
            pass
        await asyncio.sleep(1)
    if r is None:
        raise HTTPException(502, "All Overpass servers are unreachable or busy; check /api/health and retry")
    try:
        elements = r.json().get("elements", [])
    except ValueError:
        raise HTTPException(502, "Overpass returned an invalid response (likely overloaded); retry or use a shorter route")
    mix = dict(green=0.0, commercial=0.0, road=0.0, building=0.0)
    for el in elements:
        g, t = el.get("geometry"), el.get("tags", {})
        if not g or len(g) < 2:
            continue
        if "highway" in t:
            mix["road"] += length_m(g) * ROAD_WIDTH_M
        elif len(g) > 3:  # polygon
            a = area_m2(g)
            if "building" in t:
                mix["building"] += a
            elif t.get("landuse") in COMMERCIAL_LANDUSE:
                mix["commercial"] += a
            elif t.get("landuse") in GREEN_LANDUSE or t.get("natural") in GREEN_NATURAL or t.get("leisure") in GREEN_LEISURE:
                mix["green"] += a
    return mix


# ---------- scoring ----------
def score(km, p):
    s = 100 - max(0, p["green"] - 35) * 1.2 - max(0, p["building"] - 30) - min(20, km / 25) + (5 if p["road"] > 10 else 0)
    return round(max(0, min(100, s)))


def verdict(s):
    return "Recommended" if s >= 70 else "Conditional" if s >= 50 else "Not advised"


def simulate(n, seed):
    """Fallback when land-use data can't be fetched: deterministic DEMO values, not real data."""
    rd, out = random.Random(seed), []
    for i in range(n):
        t = (i + .5) / n
        u = math.exp(-min(t, 1 - t) * 4) * .6
        out.append(dict(green=.45 * (1 - u) + rd.random() * .2, commercial=.05 + .2 * u + rd.random() * .1,
                        road=.1 + rd.random() * .05, building=.15 + .35 * u + rd.random() * .1))
    return out


def normalise(m):
    t = sum(m.values()) or 1
    return {k: round(v / t * 100, 1) for k, v in m.items()}


_cache: dict = {}
_lock = asyncio.Lock()
_last = 0.0


async def _photon(c, q):
    r = await c.get("https://photon.komoot.io/api/", params={"q": q, "limit": 5}, headers=UA)
    r.raise_for_status()
    out = []
    for f in r.json().get("features", []):
        p, (lon, lat) = f["properties"], f["geometry"]["coordinates"]
        parts = [p.get("name"), p.get("street"), p.get("city") or p.get("county"), p.get("state"), p.get("country")]
        out.append({"name": ", ".join(dict.fromkeys(x for x in parts if x)), "lat": lat, "lon": lon})
    return out


async def _nominatim(c, q):
    global _last
    async with _lock:  # Nominatim: max 1 request/second
        wait = 1.1 - (time.monotonic() - _last)
        if wait > 0:
            await asyncio.sleep(wait)
        r = await c.get(NOMINATIM, params={"q": q, "format": "json", "limit": 5}, headers=UA)
        _last = time.monotonic()
    r.raise_for_status()
    return [{"name": d["display_name"], "lat": float(d["lat"]), "lon": float(d["lon"])} for d in r.json()]


@app.get("/api/search")
async def search(q: str):
    """Place autocomplete: Photon first (built for typeahead), Nominatim as fallback. Cached."""
    q = q.strip()
    if len(q) < 3:
        return []
    if q in _cache:
        return _cache[q]
    errors = []
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
        for fn in (_photon, _nominatim):
            try:
                out = await fn(c, q)
                if len(_cache) > 500:
                    _cache.clear()
                _cache[q] = out
                return out
            except Exception as e:
                errors.append(f"{fn.__name__[1:]}: {type(e).__name__} {e}")
    raise HTTPException(502, "Search unavailable. " + " | ".join(errors))


@app.get("/api/health")
async def health():
    """Diagnostic: shows which external services this machine can reach."""
    tests = {"photon": "https://photon.komoot.io/api/?q=mysuru",
             "nominatim": NOMINATIM + "?q=mysuru&format=json&limit=1",
             "osrm": OSRM + "/77.59,12.97;76.64,12.29",
             "wikipedia": WIKI_API + "?action=query&list=search&srsearch=mysuru&format=json"}
    for i, u in enumerate(OVERPASS_MIRRORS):
        tests[f"overpass{i + 1}"] = u.replace("interpreter", "status")
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
        async def chk(u):
            try:
                return (await c.get(u, headers=UA)).status_code
            except Exception as e:
                return type(e).__name__
        res = await asyncio.gather(*[chk(u) for u in tests.values()])
    return dict(zip(tests, res))


@app.post("/api/analyze")
async def analyze(req: AnalyzeReq):
    async with httpx.AsyncClient(timeout=30) as c:
        a = await geocode(c, req.start)
        await asyncio.sleep(1)  # Nominatim: max 1 request/second
        b = await geocode(c, req.end)
        km, line = await get_route(c, a, b)
        n = min(N_SEGMENTS, max(1, len(line) // 2))
        size = math.ceil(len(line) / n)
        segs = [line[i:i + size + 1] for i in range(0, len(line) - 1, size)]
        sem = asyncio.Semaphore(2)  # be polite to the public Overpass server
        source = "openstreetmap"
        if os.getenv("LANDUSE_MODE") == "simulated":
            raw, source = simulate(len(segs), int(km * 100)), "simulated"
        else:
            try:
                raw = await asyncio.gather(*[land_use(c, sem, s) for s in segs])
            except (HTTPException, httpx.HTTPError):
                if os.getenv("STRICT_DATA"):
                    raise
                raw, source = simulate(len(segs), int(km * 100)), "simulated"
    total = {k: sum(m[k] for m in raw) for k in raw[0]}
    p = normalise(total)
    s = score(km, p)
    return {
        "start": a[2], "end": b[2], "start_ll": a[:2], "end_ll": b[:2],
        "distance_km": round(km, 1),
        "cost_crore": round(km * RATE_CRORE_PER_KM.get(req.project, 40)),
        "percent": p, "data_source": source, "score": s, "verdict": verdict(s), "project": req.project,
        "segments": [{"coords": sg, "mix": normalise(m)} for sg, m in zip(segs, raw)],
    }


# ---------- chat ----------
WIKI_API = "https://en.wikipedia.org/w/api.php"
HISTORY_RE = re.compile(r"histor|heritage|ancient|archaeolog|cultur|nearby|nearest|neighbou?r|landmark|monument|what(?:.s| is) near|near the (?:start|destination|end|route|site)|places? (near|around)", re.I)
SENSITIVE = re.compile(r"temple|fort\b|palace|archaeolog|heritage|monument|sanctuary|national park|wildlife|reserve|tomb|mosque|church|wetland|forest", re.I)
GENERIC = re.compile(r"\b(nearest|nearby|near|area|areas|route|start|end|destination|project|site|this|here|around|corridor|place|places)\b")
CATS = ["green", "commercial", "road", "building"]
SYSTEM = (
    "You are an interactive advisor helping a government engineer decide whether to approve an infrastructure project. "
    "The engineer may share opinions, concerns or what-if scenarios. Engage with them: weigh their view against the data, "
    "say plainly where you agree or disagree and why, and do not simply flatter or agree. "
    "Score rule: 100 - 1.2*max(0, green%-35) - max(0, building%-30) - min(20, km/25) + 5 if roads>10%. "
    "Verdicts: >=70 Recommended, 50-69 Conditional, <50 Not advised. If a computed what-if is supplied, use it. "
    "Land-use figures come from OpenStreetMap estimates. If data_source is 'simulated', say the land-use shares are demo values "
    "and not a basis for approval. Keep answers under 150 words and end with one short follow-up question."
)


def _trim(t, n=300):
    t = " ".join(t.split())
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0] + "…"


async def web_search(c, q, n=4):
    """Optional live web results via Brave Search API (set BRAVE_API_KEY). Returns [] if not configured."""
    key = os.getenv("BRAVE_API_KEY")
    if not key:
        return []
    r = await c.get("https://api.search.brave.com/res/v1/web/search", params={"q": q, "count": n},
                    headers={"X-Subscription-Token": key, "Accept": "application/json"})
    r.raise_for_status()
    return [{"title": x.get("title", ""), "url": x.get("url", ""),
             "desc": _trim(re.sub(r"<[^>]+>", "", x.get("description", "")), 200)}
            for x in r.json().get("web", {}).get("results", [])[:n]]


async def wiki_extracts(c, ids, sentences=2):
    r = await c.get(WIKI_API, params={"action": "query", "prop": "extracts", "exintro": 1, "explaintext": 1,
                                      "exsentences": sentences, "pageids": "|".join(map(str, ids)),
                                      "exlimit": len(ids), "format": "json"}, headers=UA)
    r.raise_for_status()
    return {int(k): v.get("extract", "") for k, v in r.json()["query"]["pages"].items()}


async def history_info(c, a, q):
    """History/background of a named place, or of Wikipedia articles near the start, destination or middle."""
    ql = q.lower()
    m_ = re.search(r"(?:histor\w*|heritage|background)\s+(?:of|about|on)\s+(?:the\s+)?([a-z][a-z .'-]{2,40}?)\s*(?:[?.!,]|$)", ql)
    cand = m_[1].strip() if m_ else None
    if cand and not GENERIC.search(cand):
        r = await c.get(WIKI_API, params={"action": "query", "list": "search", "srsearch": cand, "srlimit": 1, "format": "json"}, headers=UA)
        r.raise_for_status()
        hits = r.json()["query"]["search"]
        if hits:
            ex = await wiki_extracts(c, [hits[0]["pageid"]], 5)
            return {"place": hits[0]["title"], "extract": _trim(ex.get(hits[0]["pageid"], ""), 900), "areas": []}
    s, e = a["start_ll"], a["end_ll"]
    mid = [(s[0] + e[0]) / 2, (s[1] + e[1]) / 2]
    if re.search(r"\b(start|begin|beginning|origin)\b", ql):
        pts = [("start", s)]
    elif re.search(r"\b(end|destination|arrival)\b", ql):
        pts = [("destination", e)]
    elif re.search(r"middle|midway|along|corridor", ql):
        pts = [("middle of the route", mid)]
    else:
        pts = [("start", s), ("destination", e)]
    areas = []
    for label, (lat, lon) in pts:
        r = await c.get(WIKI_API, params={"action": "query", "list": "geosearch", "gscoord": f"{lat}|{lon}", "gsradius": 10000,
                                          "gslimit": 4, "format": "json"}, headers=UA)
        r.raise_for_status()
        hits = r.json()["query"]["geosearch"]
        ex = await wiki_extracts(c, [h["pageid"] for h in hits], 2) if hits else {}
        areas.append((label, [{"title": h["title"], "km": round(h["dist"] / 1000, 1), "extract": _trim(ex.get(h["pageid"], ""))} for h in hits]))
    return {"place": None, "areas": areas}


def history_reply(info):
    texts = []
    if info.get("place"):
        out = f"{info['place']}: {info['extract']}"
        texts.append(info["extract"] + " " + info["place"])
    else:
        lines = []
        for label, items in info["areas"]:
            if not items:
                lines.append(f"Near the {label}: no Wikipedia articles found within 10 km.")
                continue
            lines.append(f"Near the {label}:\n" + "\n".join(f"• {i['title']} ({i['km']} km): {i['extract']}" for i in items))
            texts += [i["title"] + " " + i["extract"] for i in items]
        out = "\n\n".join(lines)
    if any(SENSITIVE.search(t) for t in texts):
        out += "\n\nPossible heritage or ecological sensitivity nearby. Check with the Archaeological Survey of India and the forest department before approval."
    return out + "\n\nSource: Wikipedia (CC BY-SA). Not a legal heritage check."


SYSTEM += (" If nearby-area history from Wikipedia is supplied, summarise it for the question, flag any heritage or "
           "ecological sensitivity relevant to approval, and mention the source is Wikipedia.")


def _risks(a):
    p = a["percent"]
    return [x for x in [p["green"] > 40 and "high green cover (environmental clearance delays)",
                        p["building"] > 30 and "high built-up share (acquisition and resettlement)",
                        a["distance_km"] > 150 and "a long corridor (cost and time)",
                        p["road"] < 8 and "weak existing road connectivity"] if x]


def whatif(q, a):
    """Parse scenarios like 'what if green is 20%' or 'if distance is 80 km' and recompute the score."""
    q = q.lower()
    if not re.search(r"\b(if|suppose|assume|what about|reduce|increase|change|cut|make|were|lower|raise)\b", q):
        return None
    ov = {}
    for m_ in re.finditer(r"(green|commercial|road|building)\w*[^0-9]{0,12}(\d+(?:\.\d+)?)\s*(?:%|percent)", q):
        ov[m_[1]] = min(100.0, float(m_[2]))
    for m_ in re.finditer(r"(\d+(?:\.\d+)?)\s*(?:%|percent)\s*(?:of\s+)?(green|commercial|road|building)", q):
        ov[m_[2]] = min(100.0, float(m_[1]))
    km, d = a["distance_km"], re.search(r"(\d+(?:\.\d+)?)\s*km", q)
    if d:
        km = float(d[1])
    if not ov and not d:
        return None
    p = dict(a["percent"])
    rest = [k for k in CATS if k not in ov]
    rest_tot = sum(p[k] for k in rest) or 1
    left = max(0.0, 100 - sum(ov.values()))
    for k in rest:
        p[k] = p[k] / rest_tot * left
    p.update(ov)
    s = score(km, p)
    return {"percent": {k: round(v, 1) for k, v in p.items()}, "km": km, "score": s, "verdict": verdict(s)}


def whatif_reply(w, a):
    ch = [f"{k} {w['percent'][k]}%" for k in CATS if abs(w["percent"][k] - a["percent"][k]) > 0.05]
    if abs(w["km"] - a["distance_km"]) > 0.05:
        ch.append(f"distance {w['km']:g} km")
    return (f"What-if ({', '.join(ch) or 'no change'}): score {w['score']}/100 ({w['verdict']}) vs {a['score']}/100 ({a['verdict']}) now"
            + (", which changes the verdict. " if w["verdict"] != a["verdict"] else ". ")
            + "Other land-use shares were rescaled proportionally. Want to try another scenario?")


def explain(a):
    p, km = a["percent"], a["distance_km"]
    return (f"Score = 100, minus {max(0, p['green'] - 35) * 1.2:.0f} for green land ({p['green']}%, penalised above 35%), "
            f"minus {max(0, p['building'] - 30):.0f} for buildings ({p['building']}%, above 30%), "
            f"minus {min(20, km / 25):.0f} for distance ({km} km), plus {5 if p['road'] > 10 else 0} for existing roads ({p['road']}%) "
            f"= {a['score']}/100. Which factor do you want to challenge?")


def opinion_reply(q, a, note):
    p, s = a["percent"], a["score"]
    yes = re.search(r"approve|go ahead|proceed|accept|support|in favou?r|build it|good idea|worth", q)
    no = re.search(r"reject|against|oppose|cancel|stop|scrap|shouldn'?t|should not|don'?t (approve|build)|not approve|bad idea|too risky|not worth", q)
    rk = "; ".join(_risks(a)) or "no major flags in the model"
    if yes and not no:
        if s >= 70:
            out = f"That fits the data: score {s}/100 with {rk}. Ground survey and impact assessment are still needed. What would change your mind?"
        elif s >= 50:
            out = f"The data supports approval only with conditions (score {s}/100): {rk}. Would you still approve if those were resolved first?"
        else:
            out = f"I'd respectfully disagree: the score is only {s}/100 because of {rk}. Which of these do you think the model overstates? You can test it, e.g. 'what if green is 25%'."
    elif no and not yes:
        if s >= 70:
            out = f"The data doesn't show a strong reason to reject (score {s}/100, {rk}). What is driving your view: environment, cost or displacement?"
        elif s >= 50:
            out = f"That's defensible: {s}/100 is borderline ({rk}). Would a different alignment change your view?"
        else:
            out = f"I agree the data points that way (score {s}/100: {rk}). Want to test an alternative, e.g. 'what if distance is 30 km'?"
    else:
        if re.search(r"green|forest|environment|tree|pollut", q):
            t = f"Green land is {p['green']}% here, " + ("above the 35% level where the score starts to drop, so the data supports your concern." if p["green"] > 35 else "below the 35% threshold, so the data shows no big environmental problem.")
        elif re.search(r"build|resettle|house|displace|people", q):
            t = f"Buildings are {p['building']}%, " + ("above the 30% threshold, so displacement is a real issue." if p["building"] > 30 else "below the 30% threshold, so displacement looks limited.")
        elif re.search(r"cost|expensive|budget", q):
            t = f"Indicative cost is Rs {a['cost_crore']} crore, excluding land acquisition."
        else:
            t = f"Noted. Overall the model gives {s}/100 ({a['verdict']})."
        out = t + " What matters most to you here: environment, cost or displacement?"
    return out + note


def rule_reply(q, a):
    q, p = q.lower(), a["percent"]
    note = " (Land-use shares are simulated demo values, not real data.)" if a.get("data_source") == "simulated" else ""
    w = whatif(q, a)
    if w:
        return whatif_reply(w, a) + note
    if re.search(r"\bwhy\b|explain|how.*score|calculat|breakdown|formula", q):
        return explain(a) + note
    if re.search(r"i think|i feel|i believe|i want|i'd|i would|in my (opinion|view)|my (view|opinion)|personally|we should|i say|i prefer|worried|concerned|i like|i don'?t like", q):
        return opinion_reply(q, a, note)
    if re.search(r"green|forest|environment|tree", q):
        return f"Green land is {p['green']}%. " + ("High: plan an environmental impact assessment or realign through lower-green segments." if p["green"] > 40 else "Moderate; standard environmental clearance should do.") + note
    if re.search(r"build|resettle|acqui|displace", q):
        return f"Buildings are {p['building']}% of the corridor. " + ("Expect heavy acquisition and resettlement cost." if p["building"] > 30 else "Acquisition looks light.") + note
    if re.search(r"cost|budget|money", q):
        return f"Indicative cost is Rs {a['cost_crore']} crore for {a['distance_km']} km, excluding land acquisition."
    if re.search(r"distance|how long|how far|km", q):
        return f"The road route is {a['distance_km']} km."
    if re.search(r"risk|issue|problem|concern", q):
        r = _risks(a)
        return ("Main risks: " + "; ".join(r) + "." if r else "No major red flags in this model. Still verify with a ground survey.") + note
    if re.search(r"next|step|todo|what now", q):
        return "Next steps: 1) ground survey and soil/hydrology study, 2) check real satellite land cover, 3) environmental and social impact assessment, 4) compare 2-3 alternative alignments, 5) public consultation before final approval."
    return (f"Score {a['score']}/100, verdict: {a['verdict']}. "
            + ("I'd recommend approval, subject to a ground survey." if a["score"] >= 70 else "I'd suggest conditional approval: resolve the flagged risks or compare other routes." if a["score"] >= 50 else "I'd advise against this alignment; look for another route.")
            + note + " Share your own view, or try a what-if like 'what if green is 20%'.")


@app.post("/api/chat")
async def chat(req: ChatReq):
    a = {k: v for k, v in req.analysis.items() if k != "segments"}
    key = os.getenv("ANTHROPIC_API_KEY")
    ctx = None
    if HISTORY_RE.search(req.question):
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
                ctx = history_reply(await history_info(c, a, req.question))
                try:
                    ws = await web_search(c, f"{req.question} {a['end'].split(',')[0]}")
                    if ws:
                        ctx += "\n\nWeb results (Brave Search):\n" + "\n".join(f"• {x['title']}: {x['desc']} ({x['url']})" for x in ws)
                except Exception:
                    pass  # web search is optional; Wikipedia answer still works
        except Exception as e:
            return {"reply": f"I couldn't fetch area history right now ({type(e).__name__}). Check your connection to wikipedia.org and try again.", "source": "history-error"}
    if not key:
        return {"reply": ctx or rule_reply(req.question, a), "source": "wikipedia" if ctx else "rules"}
    w = whatif(req.question, a)
    clean = []
    for h in req.history[-10:]:
        if isinstance(h, dict) and h.get("role") in ("user", "assistant"):
            item = {"role": h["role"], "content": str(h.get("content", ""))[:2000]}
            if clean and clean[-1]["role"] == item["role"]:
                clean[-1] = item
            elif clean or item["role"] == "user":
                clean.append(item)
    if clean and clean[-1]["role"] == "user":
        clean.pop()
    user = req.question + (f"\n\n[Computed what-if result: {w}]" if w else "") + (f"\n\n[Nearby-area history from Wikipedia:\n{ctx}]" if ctx else "")
    try:
        from anthropic import AsyncAnthropic
        msg = await AsyncAnthropic(api_key=key).messages.create(
            model=os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5"), max_tokens=600,
            system=SYSTEM + f"\nCurrent analysis: {a}",
            messages=clean + [{"role": "user", "content": user}])
        return {"reply": msg.content[0].text, "source": "claude"}
    except Exception as e:  # no network / bad key / bad model: still answer
        return {"reply": ctx or rule_reply(req.question, a), "source": f"fallback (Claude unavailable: {type(e).__name__})"}


@app.get("/")
def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
