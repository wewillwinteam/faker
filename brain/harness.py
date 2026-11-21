"""The harness: runs every agent each decision, gives control to the highest bid (the agent already in control gets
a small bonus so the mech does not flip-flop), then adds the reflexes every agent shares: upgrade picks from the kit,
grabbing a better weapon in reach, getting unstuck, reloading between fights.

Telemetry for the evaluator: set MECH_TELEMETRY=<folder> and each match writes one .jsonl of events (agent switches,
spotted, damage taken and dealt by agent, strikes with their gate numbers, kills, weapons taken). Off on the server."""
import json
import math
import os
import time

from .world import World, ang_diff
from .tracker import Tracker
from .reasoner import Reasoner
from . import situation
from . import agents as A


class Harness:
    def __init__(self, hello, nav_map, params):
        self.P = params
        self.kit_id = os.environ.get("MECH_KIT") or params.get("kit", "shock")
        self.kit = params["kits"][self.kit_id]
        self.w = World(hello, nav_map)
        self.w.log = self.log
        self.tracker = Tracker(self.w)
        self.reasoner = Reasoner(params["reasoner"])
        self.w.tracker, self.w.reasoner = self.tracker, self.reasoner
        self.w.intent = {}
        self.agents = [cls(params["agents"][cls.name], self.kit) for cls in A.ALL]
        self.cur = None
        self.sticky = params.get("harness", {}).get("sticky", 0.08)
        self.upg_taken = []
        self.last_pos = None
        self.stuck_t = 0.0
        self.unstick_until = -1.0
        self.unstick_dir = (1.0, 0.0)
        self.was_seen = False
        # run-or-stay: decided whenever there is a threat, then locked so the mech sticks with it
        hp_ = params.get("harness", {})
        self.lock_s = hp_.get("lock_s", 3.0)
        self.stay_joining = 0
        self.stance = None          # None | "run" | "stay"
        self.stance_until = -1.0
        self.stance_why = ""
        self.calm_since = None
        self.think_t = -99.0
        self.tel = None
        d = os.environ.get("MECH_TELEMETRY")
        if d:
            try:
                os.makedirs(d, exist_ok=True)
                self.tel = open(os.path.join(d, "tel-%d-%d.jsonl" % (os.getpid(), int(time.time() * 1000))), "w")
            except Exception:
                self.tel = None
        self.log("start", kit=self.kit_id)

    def log(self, k, **kv):
        if self.tel is None:
            return
        kv.update(k=k, t=round(getattr(self.w, "t", 0.0), 2), a=self.cur)
        try:
            self.tel.write(json.dumps(kv, separators=(",", ":")) + "\n")
            self.tel.flush()
        except Exception:
            pass

    def decide_stance(self, w):
        """Run or stay, from the fight read (brain/situation.py: who has a line on us, who is coming, who wins the
        trade), never from an HP line. Re-weighed every decision; once picked it holds for lock_s seconds. Only a
        read that turns badly against us (lost outright, or new enemies joined) can turn 'stay' into 'run' early."""
        esc = self.P["agents"]["escape"]
        rd = situation.read(w, self.tracker, self.reasoner, esc)
        w.fight_read = rd
        want, why = rd["verdict"], rd["why"]
        # calm: clear the stance after 1.5 s with no threat and no lock
        if want is None:
            self.calm_since = self.calm_since if self.calm_since is not None else w.t
        else:
            self.calm_since = None
        if self.stance is None:
            if want:
                self.set_stance(w, want, why)
        elif w.t < self.stance_until:
            panic = rd["margin"] is not None and (rd["margin"] <= 0.0 or rd["joining"] > self.stay_joining)
            if self.stance == "stay" and panic and want == "run":
                self.set_stance(w, "run", "panic: " + why)
        else:
            if want is None:
                if self.calm_since is not None and w.t - self.calm_since >= 1.5:
                    self.log("stance", stance=None, why="clear", prev=self.stance)
                    self.stance, self.stance_why = None, "clear"
            elif want != self.stance:
                self.set_stance(w, want, why)
        return want, why

    def set_stance(self, w, s, why):
        self.log("stance", stance=s, why=why, prev=self.stance, lock=self.lock_s)
        self.stance, self.stance_why, self.stance_until = s, why, w.t + self.lock_s
        rd = getattr(w, "fight_read", None) or {}
        self.stay_joining = rd.get("joining", 0)

    def pick_upgrade(self, offer):
        ids = [c["id"] for c in offer["choices"]]
        want = list(self.kit["upgrades"])
        for u in self.upg_taken:       # each listed entry is used once, so a repeated entry means "take it twice"
            if u in want:
                want.remove(u)
        i = min(range(len(ids)), key=lambda i: want.index(ids[i]) if ids[i] in want else 99)
        self.upg_taken.append(ids[i])
        return i

    def act(self, obs):
        w = self.w
        w.update(obs)
        me = w.me

        # telemetry from this tick's events
        for ev in w.events:
            ty = ev["type"]
            if ty == "damaged":
                self.log("dmg", amt=ev.get("amount"), rear=bool(ev.get("rear")), by=ev.get("by"), w=ev.get("weapon"), hp=round(me["hp"]))
            elif ty == "hit" and ev.get("target", 999) < 100:
                self.log("hit", amt=ev.get("amount"), rear=bool(ev.get("rear")), tgt=ev.get("target"), w=ev.get("weapon"))
            elif ty == "kill":
                self.log("kill", tgt=ev.get("target"))
            elif ty == "took":
                self.log("took", kind=ev.get("kind"))
        if w.tick % 40 == 0:
            self.log("hb", hp=round(me["hp"]), alive=w.alive, cubes=me["cubes"], sec=w.sec)
        seen = bool(w.seen_by)
        if seen and not self.was_seen:
            self.log("spot", by=[e["id"] for e in w.seen_by], n_line=len([e for e in w.recent if w.has_line(e, w.x, w.y, w.z)]))
        self.was_seen = seen

        self.tracker.update()
        want, why = self.decide_stance(w)
        w.stance, w.stance_until = self.stance, self.stance_until
        R, changed = self.reasoner.decide(w, self.tracker, self.stance)
        w.intent = R
        if changed:
            self.log("reason", intent=R["intent"], focus=R["focus"], ttk=R["ttk"], fire_ok=R["fire_ok"], why=R["why"])

        # agents bid, the harness picks; the locked stance outranks everyone else
        best, bp = None, None
        bids = {}
        for ag in self.agents:
            if self.stance == "stay" and ag.name == "escape":
                continue
            try:
                r = ag.propose(w)
            except Exception as e:  # one broken agent must not stop the mech
                self.log("error", agent=ag.name, err=str(e)[:120])
                r = None
            if r is None:
                continue
            bid, p = r
            bids[ag.name] = round(bid, 2)
            if ag.name == self.cur:
                bid += self.sticky
            if self.stance == "run" and ag.name == "escape":
                bid += 1.0
            if self.stance == "stay" and ag.name == "combat":
                bid += 0.5
            if bp is None or bid > bp:
                best, bp, chosen = ag, bid, p
        if best is None:
            chosen = A.plan()
            name = "none"
        else:
            name = best.name
        if name != self.cur:
            self.cur = name
            self.log("sw", hp=round(me["hp"]), note=chosen.get("note", ""), alive=w.alive)

        if w.t - self.think_t >= 0.5:   # a steady stream of what the brain is weighing, for the live view
            self.think_t = w.t
            self.log("think", stance=self.stance, lock=round(max(0.0, self.stance_until - w.t), 1), want=want, why=why,
                     bids=bids, hp=round(me["hp"]), seen=len(w.seen_by), sh=len(w.shooters),
                     p=round(w.p_undetected(w.x, w.y, w.z), 2), note=chosen.get("note", ""), sec=w.sec,
                     intent=R["intent"], reason=R["why"], focus=R["focus"], ttk=R["ttk"], fire_ok=R["fire_ok"],
                     tracks=self.tracker.view(), read=getattr(w, "fight_read", None))

        act = {"tick": w.tick}
        if obs["offer"]:
            act["pick"] = self.pick_upgrade(obs["offer"])

        # reflex: take a better weapon in reach (kit order)
        order = self.kit.get("secondary", [])
        rank = {k: i for i, k in enumerate(order)}
        cur = rank.get(w.sec, 99) if w.sec else 99
        for p in obs["pickups"]:
            k = p["kind"]
            if k in rank and rank[k] < cur and k not in w.held and math.dist(p["pos"][:2], (w.x, w.y)) < w.reach and abs(p["pos"][2] - w.z) < 4:
                sec = w.weapons[1]
                if sec is None:
                    act["take"] = p["id"]
                elif not (sec["kind"] == "laser" and (sec.get("charge") or 0) > 0):
                    act["take"], act["drop"] = p["id"], 1
                break

        mv = chosen["move"]
        boost, jump = chosen["boost"], chosen["jump"]
        # reflex: unstick
        if self.last_pos is not None and math.hypot(mv[0], mv[1]) > 0.5:
            if math.dist(self.last_pos, (w.x, w.y)) < 0.25:
                self.stuck_t += 0.1
            else:
                self.stuck_t = max(0.0, self.stuck_t - 0.2)
            if self.stuck_t > 1.2:
                a = math.atan2(mv[1], mv[0]) + (1.2 if (w.tick // 40) % 2 else -1.2)
                self.unstick_dir = (math.cos(a), math.sin(a))
                self.unstick_until = w.t + 1.2
                self.stuck_t = 0.0
        else:
            self.stuck_t = 0.0
        self.last_pos = (w.x, w.y)
        if w.t < self.unstick_until and name not in ("combat",):
            mv = [self.unstick_dir[0] * 0.6 + mv[0] * 0.4, self.unstick_dir[1] * 0.6 + mv[1] * 0.4]
            if me["grounded"]:
                jump = True
            elif me["energy"] > 25:
                boost = True
        # reflex: reload between fights
        g = w.weapons[0]
        fire = chosen["fire"]
        if name not in ("combat", "escape") and g and g.get("ammo", 12) < 9 and g.get("reload", 0) == 0 and not fire[0]:
            act["reload"] = True
        aim = chosen["aim"] if chosen["aim"] is not None else [me["aim"][0], 0.0]
        act.update(move=mv, boost=boost, jump=jump, aim=aim, fire=fire)
        return act, name, chosen.get("note", "")
