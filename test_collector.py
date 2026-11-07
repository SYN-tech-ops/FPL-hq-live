"""Offline tests for the publisher boundary, failure handling and GW rollover."""
import contextlib
import io
import json
from datetime import datetime, timedelta, timezone
from email.message import Message
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import collector as c

NOW = datetime(2026,11,7,10,tzinfo=timezone.utc)


def raw_for(gw=10):
    start = NOW-timedelta(weeks=gw-1, hours=14)
    events = [dict(id=i, deadline_time=c.stamp(start+timedelta(weeks=i-1)),
                   is_current=i==gw,is_next=i==gw+1,finished=i<gw,data_checked=i<gw)
              for i in range(1,39)]
    teams = [dict(id=i, name=f"Club {i}",short_name=str(i)) for i in range(1,21)]
    positions = [dict(id=i+1,singular_name_short=p) for i,p in enumerate(("GKP","DEF","MID","FWD"))]
    types = [1,2,2,2,3,3,3,3,4,4,4,1,3,2,2]
    players = [dict(dict.fromkeys(c.PLAYER,0), id=i+1, team=(i%20)+1,
                    element_type=t,now_cost=50,web_name=f"P{i+1}",status="a", news="")
               for i,t in enumerate(types)]
    live = [dict(id=p["id"],stats=dict.fromkeys(c.LIVE,0)) for p in players]
    fixtures = []
    for h in teams:
        for a in teams:
            if h==a:
                continue
            fixtures.append(dict(dict.fromkeys(c.FIXTURE,None),id=len(fixtures)+1,team_h=h["id"],
                                 team_a=a["id"],event=gw,started=False,finished=False,finished_provisional=False))
    data = {"bootstrap-static/":dict(events=events,elements=players,teams=teams,element_types=positions),
            "fixtures/":fixtures,f"event/{gw}/live/":{"elements":live}}
    for entry in c.ENTRIES:
        path = f"entry/{entry}/"
        data[path] = dict(dict.fromkeys(c.PROFILE,0),id=entry,name=str(entry),current_event=gw,started_event=1)
        history = [dict(dict.fromkeys(c.HISTORY,0),event=i) for i in range(1,gw+1)]
        data[path+"history/"] = dict(current=history,chips=[])
        data[path+"transfers/"] = []
        picks = [dict(element=i,position=i,multiplier=2 if i==10 else int(i<=11),
                      is_captain=i==10,is_vice_captain=i==7) for i in range(1,16)]
        data[path+f"event/{gw}/picks/"] = dict(entry_history=history[-1].copy(),picks=picks,
                                               active_chip=None,automatic_subs=[])
    return data


class FeedTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.raw = raw_for()

    def run_feed(self, old, new, data=None, now=NOW):
        with contextlib.redirect_stderr(io.StringIO()),contextlib.redirect_stdout(io.StringIO()):
            return c.refresh(self.root/old,self.root/new,(data or self.raw).__getitem__,lambda:now)

    def test_rollover_failure_retains_gw_then_recovers(self):
        self.assertEqual(self.run_feed("empty","first"),0)
        first = c.prior(self.root/"first","latest.json")
        next_week = raw_for(11)
        # A valid-looking old squad served at the new URL must fail.
        path = "entry/601071/event/11/picks/"
        next_week[path]["entry_history"]["event"] = 10
        self.assertEqual(self.run_feed("first","failed",next_week),2)
        status = c.prior(self.root/"failed","status.json")
        retained = c.prior(self.root/"failed","latest.json")
        self.assertEqual((status["current_gameweek"],status["retained_gameweek"]),(11,10))
        self.assertEqual(retained,dict(first,freshness="stale"))
        self.assertEqual(status["accounts"]["601071"],"unavailable")
        self.assertFalse((self.root/"failed/archive").exists())
        next_week[path]["entry_history"]["event"] = 11
        self.assertEqual(self.run_feed("failed","recovered",next_week, NOW+timedelta(seconds=30)),0)
        recovered = c.prior(self.root/"recovered","latest.json")
        self.assertGreater(recovered["collected_at_utc"],first["collected_at_utc"])
        self.assertEqual(recovered["current_gameweek"],11)
        self.assertEqual(len(recovered["accounts"]),3)
        for file,digest in recovered["files_sha256"].items():
            self.assertEqual(sha256((self.root/"recovered"/file).read_bytes()).hexdigest(),digest)

    def test_invalid_shapes_and_unavailable_do_not_publish(self):
        for i, mutate in enumerate((
            lambda d:d["entry/601071/event/10/picks/"]["picks"].pop(),
            lambda d:d["entry/603617/history/"]["current"].pop(),
            lambda d:d["event/10/live/"]["elements"].pop(),
            lambda d:d["entry/1989422/"].update(id=0),
            lambda d:d.update({"entry/601071/transfers/":{"detail":"maintenance"}}),
            lambda d:d.update({"fixtures/":[]}),
            lambda d:d["bootstrap-static/"]["events"][9].update(is_current=False),
            lambda d:d.pop("entry/601071/event/10/picks/"),
        )):
            with self.subTest(i=i):
                d = raw_for()
                mutate(d)
                self.assertEqual(self.run_feed("empty",str(i),d),2)
                self.assertFalse((self.root/str(i)/"latest.json").exists())
                self.assertFalse(c.prior(self.root/str(i),"status.json")["complete"])

    def test_schedule_and_deadline_transition(self):
        self.run_feed("empty","first")
        folder = self.root/"first"
        self.assertFalse(c.due(folder,NOW+timedelta(hours=1)))
        self.assertTrue(c.due(folder,NOW+timedelta(hours=6)))
        self.assertTrue(c.busy(NOW,[{"kickoff_time":c.stamp(NOW)}],[]))
        self.assertEqual(self.run_feed("first","roll",now=NOW+timedelta(days=8)),2)

    @patch("collector.time.sleep")
    def test_transport_retries_and_blocks(self, _sleep):
        class Response(io.BytesIO):
            status = 200
            headers = Message()
        Response.headers["Content-Type"] = "application/json"
        api = c.PublicAPI()
        def error(code, retry=None):
            h = Message()
            if retry: h["Retry-After"] = retry
            return HTTPError(c.BASE,code,"upstream",h,None)
        with patch.object(api,"open",side_effect=[error(503),Response(b'{"ok":true}')]) as op:
            self.assertEqual(api("bootstrap-static/"),{"ok":True})
            self.assertEqual(op.call_count,2)
        for code,delay in ((403,None),(429,"180")):
            with patch.object(api,"open",side_effect=error(code,delay)) as op:
                with self.assertRaises(c.Blocked): api("fixtures/")
                self.assertEqual(op.call_count,1)
        with patch.object(api,"open",return_value=Response(b"<html>maintenance</html>")):
            with self.assertRaises(ValueError): api("fixtures/")
        with patch.object(api,"open",side_effect=error(403)),contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(c.refresh(self.root/"none",self.root/"denied",api,lambda:NOW),2)
            self.assertEqual(c.prior(self.root/"denied","status.json")["state"],"unavailable")


if __name__=="__main__":
    unittest.main()
