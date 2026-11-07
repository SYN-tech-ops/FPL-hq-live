"""Public FPL feed for one season. Python 3.11+, standard library only."""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
import json
from pathlib import Path
import socket
from http.client import HTTPException
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler

BASE = "https://fantasy.premierleague.com/api/"
ENTRIES = {603617: "Haramball", 1989422: "Voodoo", 601071: "Ankit"}
SEASON = "2026/27"
PLAYER = """id web_name first_name second_name element_type team now_cost status news news_added
chance_of_playing_this_round chance_of_playing_next_round total_points minutes starts goals_scored
assists clean_sheets bonus bps saves defensive_contribution expected_goals expected_assists
expected_goal_involvements expected_goals_conceded selected_by_percent penalties_order
direct_freekicks_order corners_and_indirect_freekicks_order""".split()
FIXTURE = """id event team_h team_a kickoff_time started finished finished_provisional
team_h_score team_a_score team_h_difficulty team_a_difficulty""".split()
PROFILE = """id name current_event started_event summary_overall_points summary_overall_rank
summary_event_points last_deadline_bank last_deadline_value last_deadline_total_transfers""".split()
HISTORY = """event points total_points rank overall_rank bank value event_transfers
event_transfers_cost points_on_bench""".split()
PICK = "element position multiplier is_captain is_vice_captain".split()
LIVE = """minutes total_points goals_scored assists clean_sheets goals_conceded own_goals penalties_saved
penalties_missed yellow_cards red_cards saves bonus bps defensive_contribution starts expected_goals
expected_assists""".split()


class Invalid(ValueError):
    pass


class Blocked(Invalid):
    """Stop the whole collection if the public API denies access or rate limits it."""


def check(condition, message):
    if not condition:
        raise Invalid(message)


def utc():
    return datetime.now(timezone.utc)


def stamp(value):
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def instant(value):
    t = datetime.fromisoformat(value)
    check(t.tzinfo is not None, "timestamp without timezone")
    return t


def fields(row, names):
    check(isinstance(row, dict), "expected JSON object")
    missing = set(names) - row.keys()
    check(not missing, f"missing fields {sorted(missing)}")
    return {k: row[k] for k in names}


def rows(value, empty=False):
    check(isinstance(value, list) and (empty or value), "missing or empty row list")
    return value


def index(records, key="id"):
    result = {r[key]: r for r in records}
    check(len(result) == len(records), f"duplicate {key}")
    return result


def integer(value):
    return type(value) is int


def parse_json(raw):
    def invalid_constant(s):
        raise Invalid(f"invalid JSON constant {s}")
    return json.loads(raw, parse_constant=invalid_constant)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class PublicAPI:
    def __init__(self):
        self.open = build_opener(NoRedirect()).open
        self.last = 0.0

    def __call__(self, path):
        request = Request(BASE + path, headers={"Accept": "application/json",
                          "User-Agent": "FPL-HQ-Live/1 (public FPL collector)"})
        for attempt in range(3):
            time.sleep(max(0, 1 - (time.monotonic() - self.last)))
            self.last = time.monotonic()
            delay = 2 ** (attempt + 1)
            try:
                with self.open(request, timeout=15) as response:
                    check(response.status == 200, f"HTTP {response.status}")
                    check(response.headers.get_content_type() == "application/json", "expected application/json")
                    raw = response.read(12_000_001)
                    check(len(raw) <= 12_000_000, "response exceeds size limit")
                    return parse_json(raw)
            except HTTPError as e:
                if e.code in (401, 403):
                    raise Blocked(f"HTTP {e.code}") from e
                if e.code not in (429, 500, 502, 503, 504):
                    raise Invalid(f"HTTP {e.code}") from e
                retry = e.headers.get("Retry-After")
                if retry:
                    try:
                        delay = max(delay, int(retry) if retry.isdigit() else
                                    (parsedate_to_datetime(retry) - utc()).total_seconds())
                    except (TypeError, ValueError):
                        raise Blocked("invalid Retry-After; defer until next run") from e
                if delay > 30 or (e.code == 429 and attempt == 2):
                    raise Blocked(f"HTTP {e.code}; defer until next run") from e
                reason = f"HTTP {e.code}"
            except (URLError, TimeoutError, socket.error, HTTPException) as e:
                reason = type(e).__name__
            if attempt < 2:
                time.sleep(delay)
        raise Invalid(f"{reason} after three attempts")


def numbers(record, keys, nullable=False):
    for key in keys:
        value = record[key]
        if value is None and nullable:
            continue
        check(integer(value), f"invalid integer for {key}")


def bootstrap(raw, now):
    events = [fields(e, "id deadline_time is_current is_next finished data_checked".split())
              for e in rows(raw["events"])]
    by_gw = index(events)
    check(set(by_gw) == set(range(1, 39)), "incomplete event calendar")
    deadlines = [instant(by_gw[i]["deadline_time"]) for i in range(1, 39)]
    check(deadlines[0].year == 2026 and deadlines[-1].year == 2027, "unexpected season")
    check(deadlines == sorted(deadlines), "unordered deadlines")
    for e in events:
        check(all(type(e[k]) is bool for k in ("is_current","is_next","finished","data_checked")),
              "invalid event flags")
    current = [e for e in events if e["is_current"]]
    check(len(current) == 1, "current GW not yet published or ambiguous")
    event = current[0]
    gw = event["id"]
    check(deadlines[gw-1] <= now and (gw == 38 or now < deadlines[gw]),
          "deadline transition: FPL current event has not caught up")
    clubs = [fields(r, ("id","name","short_name")) for r in rows(raw["teams"])]
    positions = [fields(r, ("id","singular_name_short")) for r in rows(raw["element_types"])]
    team_ids, pos_ids = index(clubs), index(positions)
    check(len(team_ids) == 20 and set(pos_ids) == {1,2,3,4}, "invalid clubs or positions")
    players = [fields(r, PLAYER) for r in rows(raw["elements"])]
    players_by_id = index(players)
    for p in players:
        check(integer(p["id"]) and p["team"] in team_ids and p["element_type"] in pos_ids
              and integer(p["now_cost"]) and p["now_cost"] > 0 and isinstance(p["web_name"], str)
              and isinstance(p["status"], str) and integer(p["total_points"]), "invalid player")
        numbers(p, ("total_points","minutes","starts","goals_scored","assists","clean_sheets",
                    "bonus","bps","saves","defensive_contribution"))
        for k in ("expected_goals","expected_assists","expected_goal_involvements","expected_goals_conceded"):
            check(0 <= float(p[k]) < 1000, f"invalid {k}")
    return event, events, clubs, positions, players_by_id


def verify_fixtures(raw, ids, events):
    fixtures = [fields(r, FIXTURE) for r in rows(raw)]
    index(fixtures)
    # Each PL club meets each other club once at home, even if a date is TBC.
    pairs = {(f["team_h"],f["team_a"]) for f in fixtures}
    check(len(fixtures) == len(pairs) == len(ids)*(len(ids)-1), "incomplete EPL fixtures")
    for f in fixtures:
        check(f["team_h"] in ids and f["team_a"] in ids and f["team_h"] != f["team_a"]
              and (f["event"] is None or f["event"] in events), "invalid fixture reference")
        for k in ("started","finished","finished_provisional"):
            check(f[k] is None or type(f[k]) is bool, f"invalid fixture {k}")
        numbers(f, ("team_h_score","team_a_score"), nullable=True)
        if f["kickoff_time"] is not None:
            instant(f["kickoff_time"])
    return fixtures


def verify_live(raw, players):
    live = {}
    for r in rows(raw["elements"]):
        pid = r["id"]
        stats = fields(r["stats"], LIVE)
        check(pid in players and pid not in live and integer(stats["minutes"])
              and stats["minutes"] >= 0 and integer(stats["total_points"]), "invalid live row")
        numbers(stats, (k for k in LIVE if not k.startswith("expected_")))
        for k in ("expected_goals","expected_assists"):
            check(0 <= float(stats[k]) < 1000, f"invalid live {k}")
        live[pid] = stats
    check(live.keys() == players.keys(), "live roster and bootstrap roster differ")
    return live


def account(entry, gw, read, players, clubs, positions, live):
    path = f"entry/{entry}/"
    profile = fields(read(path), PROFILE)
    check(profile["id"] == entry and profile["current_event"] == gw, "wrong entry or profile GW")
    check(isinstance(profile["name"], str) and all(integer(profile[k]) for k in
          ("summary_overall_points","summary_overall_rank","last_deadline_bank","last_deadline_value")),
          "invalid manager profile")
    h = read(path+"history/")
    history = [fields(r, HISTORY) for r in rows(h["current"])]
    for r in history:
        check(all(integer(v) for v in r.values()), "invalid history numbers")
    rounds = index(history, "event")
    check(set(rounds) == set(range(profile["started_event"],gw+1)), "incomplete entry history")
    chips = [fields(r, ("event","name","time")) for r in rows(h["chips"], empty=True)]
    check(all(r["event"] in rounds and isinstance(r["name"], str) for r in chips), "invalid chip history")
    transfer_keys = "entry event time element_in element_in_cost element_out element_out_cost".split()
    transfers = [fields(r, transfer_keys) for r in rows(read(path+"transfers/"), empty=True)]
    for tr in transfers:
        check(tr["entry"] == entry and tr["event"] in rounds and tr["element_in"] in players
              and tr["element_out"] in players and integer(tr["element_in_cost"])
              and integer(tr["element_out_cost"]), "invalid transfer")
        instant(tr["time"])
    doc = read(path+f"event/{gw}/picks/")
    locked = fields(doc["entry_history"], HISTORY)
    check(locked["event"] == gw and all(integer(v) for v in locked.values()), "wrong picks GW or history")
    squad = sorted([fields(r, PICK) for r in rows(doc["picks"])], key=lambda p:p["position"])
    check([p["position"] for p in squad] == list(range(1,16)), "bad squad slots")
    check(len(index(squad, "element")) == 15, "duplicate player in squad")
    for p in squad:
        player = players[p["element"]]
        check(type(p["is_captain"]) is bool and type(p["is_vice_captain"]) is bool
              and integer(p["multiplier"]) and 0 <= p["multiplier"] <= 3, "invalid pick flags")
        p.update(name=player["web_name"], club=clubs[player["team"]]["short_name"],
                 position_type=positions[player["element_type"]]["singular_name_short"],
                 gw_points=live[p["element"]]["total_points"], minutes=live[p["element"]]["minutes"])
    for flag in ("is_captain","is_vice_captain"):
        check(sum(p[flag] for p in squad)==1, "missing or duplicate armband")
    check(not any(p["is_captain"] and p["is_vice_captain"] for p in squad), "identical armbands")
    types = [players[p["element"]]["element_type"] for p in squad]
    check(Counter(types) == {1:2,2:5,3:5,4:3} and types[:11].count(1)==1 and
          types[:11].count(2)>=3 and types[:11].count(3)>=2 and types[:11].count(4)>=1,
          "illegal squad or XI")
    chip = doc["active_chip"]
    check(chip is None or isinstance(chip,str), "invalid active chip")
    subs = [fields(r, ("entry","element_in","element_out","event"))
            for r in rows(doc["automatic_subs"], empty=True)]
    check(all(r["entry"]==entry and r["event"]==gw and r["element_in"] in players and
              r["element_out"] in players for r in subs), "invalid automatic substitutions")
    return dict(label=ENTRIES[entry], profile=profile, picks_gameweek=gw, picks=squad,
                active_chip=chip, automatic_subs=subs, gw_summary=locked,
                history=history, chips=chips, transfers=transfers)


def json_bytes(obj):
    return (json.dumps(obj, ensure_ascii=False, allow_nan=False, separators=(",", ":"))+"\n").encode()


def write(folder, path, obj):
    destination = folder / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Make each catalog row a line for efficient git deltas.
    if isinstance(obj,list):
        raw = b"[\n" + b" ,\n".join(json_bytes(r).rstrip() for r in obj) + b"\n]\n"
    else:
        raw = json_bytes(obj)
    destination.write_bytes(raw)
    return sha256(raw).hexdigest()


def prior(folder, name):
    path = folder / name
    return parse_json(path.read_bytes()) if path.exists() else None


def busy(now, fixtures, events):
    times = [instant(f["kickoff_time"]) for f in fixtures if f["kickoff_time"]]
    times += [instant(e["deadline_time"]) for e in events]
    return any(-timedelta(hours=1) <= now-t <= timedelta(hours=3) for t in times)


def due(folder, now):
    try:
        h = prior(folder, "status.json")
        if not h or h["state"]!="ok":
            return True
        age = now-instant(h["collected_at_utc"])
        if age >= timedelta(hours=5, minutes=45):
            return True
        return busy(now, prior(folder,"fixtures.json"), prior(folder,"deadlines.json"))
    except (KeyError, TypeError, ValueError, OSError):
        return True


def refresh(previous, out, fetch=None, clock=utc):
    """Emit a complete candidate, or health + retained latest, into an empty directory."""
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise RuntimeError("output must be an empty staging directory")
    fetch = fetch or PublicAPI()
    started = clock()
    attempted = []
    states = {str(i): "not_attempted" for i in ENTRIES}
    errors = []
    gw = None

    def read(path):
        attempted.append(BASE + path)
        return fetch(path)

    try:
        event, events, teams, positions, players = bootstrap(read("bootstrap-static/"), started)
        gw = event["id"]
        fixtures = verify_fixtures(read("fixtures/"), index(teams), index(events))
        live = verify_live(read(f"event/{gw}/live/"), players)
        accounts = {}
        for entry in ENTRIES:
            try:
                accounts[str(entry)] = account(entry,gw,read,players,index(teams),index(positions),live)
                states[str(entry)] = "validated"
            except (ValueError, KeyError, TypeError, IndexError, OSError) as exc:
                states[str(entry)] = "unavailable"
                errors.append({"entry_id":entry,"source":attempted[-1],"error":str(exc)[:250]})
                if isinstance(exc,Blocked):
                    break
        check(not errors, "one or more accounts unavailable")
        ended = clock()
        check(all(e["id"] <= gw or instant(e["deadline_time"]) > ended for e in events),
              "deadline passed during collection")
    except (ValueError, KeyError, TypeError, IndexError, OSError) as exc:
        if not errors:
            errors.append({"source":attempted[-1] if attempted else BASE,"error":str(exc)[:250]})
        old = prior(previous,"latest.json")
        collected = old["collected_at_utc"] if old else None
        digest = None
        if old:
            old["freshness"] = "stale"
            digest = write(out,"latest.json",old)
        status = dict(season=SEASON, state="stale" if old else "unavailable",
                      attempted_at_utc=stamp(started), collected_at_utc=collected,
                      current_gameweek=gw, retained_gameweek=old["current_gameweek"] if old else None,
                      complete=False, accounts=states, errors=errors, sources=attempted, latest_sha256=digest)
        write(out,"status.json",status)
        print(json.dumps(status),file=sys.stderr)
        return 2

    collected = stamp(ended)
    expires = ended + timedelta(hours=2 if busy(ended,fixtures,events) else 8)
    expires = min([expires]+[instant(e["deadline_time"]) for e in events if e["id"]>gw])
    scoring = "completed" if event["finished"] and event["data_checked"] else "provisional"
    history = {i:{"history":a.pop("history"), "transfers":a.pop("transfers")}
               for i,a in accounts.items()}
    for i,a in accounts.items():
        a["gw_transfers"] = [t for t in history[i]["transfers"] if t["event"]==gw]
    tables = {"deadlines.json":events, "history.json":history, "players.json":list(players.values()), "fixtures.json":fixtures,
              "live.json":[{"id":pid, **stats} for pid,stats in live.items()]}
    files = {name:write(out,name,obj) for name,obj in tables.items()}
    latest = dict(schema_version=1,season=SEASON,competition="Fantasy Premier League",
                  collected_at_utc=collected,refresh_started_at_utc=stamp(started),
                  current_gameweek=gw,scoring_state=scoring,freshness="fresh",expires_at_utc=stamp(expires),
                  provenance="official_public_fpl",money_unit="0.1 GBP million",
                  accounts=accounts,account_status=states,teams=teams,positions=positions,
                  current_deadline_utc=event["deadline_time"],
                  next_deadline_utc=next((e["deadline_time"] for e in events if e["is_next"]),None),
                  files_sha256=files,sources=attempted)
    digest = write(out,"latest.json",latest)
    archive = {k:latest[k] for k in ("schema_version","season","current_gameweek","collected_at_utc","scoring_state","sources")}
    archive["accounts"] = {i:{k:a[k] for k in ("label","profile","gw_summary","picks","active_chip","automatic_subs")}
                           for i,a in accounts.items()}
    archive_path = f"archive/2026-27/gw-{gw:02}"
    if not (previous / (archive_path+"-first.json")).exists():
        write(out,archive_path+"-first.json",archive)
    write(out,archive_path+".json",archive)
    write(out,"status.json",dict(schema_version=1,season=SEASON,state="ok",complete=True,
          attempted_at_utc=stamp(started),collected_at_utc=collected,expires_at_utc=stamp(expires),
          current_gameweek=gw,scoring_state=scoring,accounts=states,errors=[],latest_sha256=digest))
    print(f"Validated GW{gw}: {len(accounts)} entries, {len(players)} players, {len(fixtures)} fixtures")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous",type=Path,default=Path("data"))
    parser.add_argument("--out",type=Path,default=Path("candidate"))
    parser.add_argument("--scheduled",action="store_true")
    args = parser.parse_args()
    if args.scheduled and not due(args.previous,utc()):
        print("Between refresh windows.")
    else:
        sys.exit(refresh(args.previous,args.out))
