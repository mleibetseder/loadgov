"""Memory guard runtime: collects candidates, executes the engine's actions, keeps the paused registry.

Everything that stops, freezes, pauses or terminates something is gated by `[memguard] enforce` (orphan
reaping by `[reaper] enforce`): without it the engines run as a shadow and only `WOULD ...` events are
written. Undoing a pause an earlier enforce run made (thaw, SIGCONT, docker unpause) is always allowed, so
rolling `enforce` back releases what is paused by the normal resume rule (`RELEASED`).

Crash safety: the registry is saved to state.json at every change. Nothing here runs on
stop, so paused work stays paused when the daemon dies; the next start re-adopts the entries (`ADOPTED`)
and resumes them only by the normal rule. `loadgovd --recover --force` (`daemon/recover.py`) is the only
thaw-all.

Socket ops `freeze`, `unfreeze` (owner only, default-deny) and `reap` (anyone; it only frees memory) live
here too, with their REST twins.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import signal
import time

from loadgov.daemon.principal import caller_info, owner_denial
from loadgov.daemon.slot_ops import _err, _text
from loadgov.engines import jvm as jvm_engine
from loadgov.engines import reaper
from loadgov.engines.limits import iso
from loadgov.engines.memguard import Action, Entry, MemGuard, Tree, denied
from loadgov.engines.timers import parse_ttl
from loadgov.host import cgroup, proctree
from loadgov.host.docker import Docker, run_docker
from loadgov.host.signals import AGENT_COMM, Signals

log = logging.getLogger("loadgovd")

CAPABILITIES = ["freeze", "reap"]
RESULTS = {
    "REFUSED": "refused",
    "ADOPTED": "adopted",
    "RELEASED": "released",
    "PAUSE_FAILED": "failed",
    "HOLD": "hold",
    "REPORT": "reported",
}  # fmt: skip; every other real event is "applied"
HA_KINDS = {"PAUSE": "pause", "ABORT": "abort", "LOW_MEMORY": "low_memory", "RESUME": "resume",
            "RELEASED": "resume", "REAP": "reap"}  # fmt: skip


def read_snapshot(path: str) -> str | None:
    try:
        with open(path, errors="replace") as f:
            return f.read(65536)
    except OSError:
        return None


class MemGuardRuntime:
    def __init__(self, d, **options):
        """`options` are test-only: opener, sender, closer (pidfd), docker_runner, snapshot_reader,
        override ((LOW, HIGH) MB for the global rule), monotonic, restrict_cgroup (a cgroup path prefix:
        only job scopes below it are ever touched; legacy trees, containers and JVM daemons are ignored)."""
        self.d = d
        self.cfg, self.reaper_cfg = d.config.memguard, d.config.reaper
        self.monotonic = options.get("monotonic", time.monotonic)
        sig_kw = {k: options[k] for k in ("opener", "sender", "closer") if k in options}
        self.signals = Signals(d.fs, self.cfg.enforce, **sig_kw)
        self.reap_signals = Signals(d.fs, self.reaper_cfg.enforce, **sig_kw)  # orphan TERM/KILL: [reaper]
        self.docker = Docker(options.get("docker_runner", run_docker), self.cfg.enforce)
        self.snapshot_reader = options.get("snapshot_reader", read_snapshot)
        self.restrict: str | None = options.get("restrict_cgroup")
        self.engine = MemGuard(
            d.clock, self.monotonic, self.cfg.projects, options.get("override"), self.cfg.deny_containers
        )
        self.dirty = False
        self.foreign: list[dict] = []
        self._seen_foreign: set[int] = set()
        self._would_reap: set[str] = set()
        self._retry_after: dict[str, float] = {}  # orphan entries that could not be thawed: slow retries
        self._stuck_alerted: set[str] = set()
        self._reap_seen: dict[tuple[str, str], str] = {}
        self._marker_owned = False

    # ---- start: re-adopt -------------------------------------------------------------------------

    def restore(self, raw) -> None:
        """Load the saved registry; keep what is still paused (ADOPTED), drop what is gone (RELEASED)."""
        for e in self.engine.restore(raw):
            if self._still_paused(e):
                self.engine.note("ADOPTED", f"ADOPTED {e.what} ({e.id}, paused since {iso(e.since)})",
                                 False, e.project, entry=e.id)  # fmt: skip
            else:
                self.engine.drop(e.id)
                self.engine.note("RELEASED", f"RELEASED {e.what}: no longer paused or gone", False,
                                 e.project, entry=e.id)  # fmt: skip
                self.dirty = True
        self._sync_marker()

    def _still_paused(self, e: Entry) -> bool:
        if e.how == "freeze":
            return any(cgroup.read_freeze(self.d.fs, t["cgroup"]) for t in e.targets)
        if e.how == "sigstop":
            for t in e.targets:
                st = self.signals.stat(t["pid"])
                if st and st[1] in ("T", "t") and st[2] == t["start"]:
                    return True
            return False
        return True  # docker: there is no cheap way to ask; unpause is harmless when it is running

    def _targets_alive(self, e: Entry) -> bool:
        if e.how == "freeze":
            return any(proctree.scope_pids(self.d.fs, t["cgroup"]) for t in e.targets)
        if e.how == "sigstop":
            return any(self.signals.alive(t["pid"], t["start"]) for t in e.targets)
        return False

    def _session_alive(self, s: dict) -> bool:
        return self.signals.alive(s["pid"], s.get("start"))

    # ---- collecting ------------------------------------------------------------------------------

    def collect_trees(self) -> list[Tree]:
        fs = self.d.fs
        tab = proctree.table(fs)
        kids = proctree.children(tab)
        trees: list[Tree] = []
        used: set[int] = set()
        for j in list(self.d.jobs.jobs.values()):
            if self.restrict and not j.cgroup.startswith(self.restrict):
                continue
            pids = [p for p in proctree.scope_pids(fs, j.cgroup) if p in tab]
            if not pids:
                continue
            procs = [tab[p] for p in pids]
            used.update(pids)
            agent = {p.pid for p in procs if p.comm == AGENT_COMM}
            keep = set(agent)
            for a in agent:
                keep |= proctree.ancestors(tab, a)
            stop = [p for p in procs if p.pid not in keep]
            if j.kind in ("gate", "compile") and not agent:
                how, targets, pool = "freeze", [{"cgroup": j.cgroup}], procs
                cur = fs.cgroup(j.cgroup, "memory.current")
                mb = int(cur) // 2**20 if cur and cur.strip().isdigit() else sum(p.rss_mb for p in procs)
            elif stop:  # the agent and its ancestors are never stopped, the rest of the tree is
                how, pool = "sigstop", stop
                targets, mb = [{"pid": p.pid, "start": p.start} for p in stop], sum(p.rss_mb for p in stop)
            else:
                continue
            owner = min(agent) if agent else j.owner_pid  # a live agent owns the work, not its client
            start = self._start_of(owner)
            session = {"pid": owner, "start": start} if start else None
            trees.append(
                self._tree("scope", j.unit, how, f"scope {j.unit}", j.project, mb, targets, session, pool)
            )
        if self.restrict:
            return trees
        trees += self._legacy(tab, kids, used)
        trees += self._containers()
        return trees

    @staticmethod
    def _tree(cls, key, how, what, project, mb, targets, session, pool) -> Tree:
        rustc = max((p for p in pool if p.comm == "rustc"), key=lambda p: p.rss_mb, default=None)
        return Tree(cls, key, how, what, project, mb, targets, session,
                    rustc.rss_mb if rustc else 0, (rustc.pid, rustc.start) if rustc else None)  # fmt: skip

    def _start_of(self, pid: int) -> int | None:
        st = self.signals.stat(pid)
        return st[2] if st else None

    def _legacy(self, tab, kids, used) -> list[Tree]:
        """Implementer trees found like the legacy guard does, then `claude -p` sessions of
        agent_cwd projects."""
        fs, out, claude_seen = self.d.fs, [], set()
        snap = self.cfg.legacy_snapshot
        for r in tab.values() if snap else ():
            if r.comm != "bash":
                continue
            args = proctree.cmdline(fs, r.pid)
            if not (args and len(args) > 1 and args[1].startswith(snap.prefix)):
                continue
            text = self.snapshot_reader(args[1])
            if not text or not any(ln.startswith(snap.marker) for ln in text.splitlines()):
                continue
            work: dict[int, proctree.P] = {}
            for c in kids.get(r.pid, []):
                if c in tab and tab[c].comm in (AGENT_COMM, "timeout"):
                    if tab[c].comm == AGENT_COMM:
                        claude_seen.add(c)
                    for p in proctree.descendants(tab, kids, c):
                        if p.comm != AGENT_COMM and p.pid not in used:
                            work[p.pid] = p
            if work:
                out.append(self._legacy_tree(r, snap.project, list(work.values())))
        for name, gp in self.cfg.projects.items():
            if not gp.agent_cwd:
                continue
            prefix = gp.agent_cwd.rstrip("/")
            for c in tab.values():
                if c.comm != AGENT_COMM or c.pid in claude_seen or c.pid in used:
                    continue
                args = proctree.cmdline(fs, c.pid)
                where = proctree.cwd(fs, c.pid)
                if not (args and args[:2] == [AGENT_COMM, "-p"] and where):
                    continue
                if where != prefix and not where.startswith(prefix + "/"):
                    continue
                work = [p for p in proctree.descendants(tab, kids, c.pid) if p.comm != AGENT_COMM
                        and p.pid not in used]  # fmt: skip
                if work:
                    out.append(self._legacy_tree(c, name, work))
        return out

    def _legacy_tree(self, root: proctree.P, project: str, work: list) -> Tree:
        return self._tree(
            "legacy", f"legacy:{root.pid}", "sigstop", f"implementer {root.pid} ({project})", project,
            sum(p.rss_mb for p in work), [{"pid": p.pid, "start": p.start} for p in work],
            {"pid": root.pid, "start": root.start}, work,
        )  # fmt: skip

    def _containers(self) -> list[Tree]:
        rules = [(n, re.compile(p.containers)) for n, p in self.cfg.projects.items() if p.containers]
        if not rules:
            return []
        out = []
        for name in self.docker.names():
            if denied(name, self.cfg.deny_containers):
                continue
            for proj, rx in rules:
                if rx.search(name):
                    out.append(Tree("container", name, "docker", f"container {name}", proj, 0,
                                    [{"name": name}]))  # fmt: skip
                    break
        return out

    def collect_jvm(self) -> list[jvm_engine.Jvm]:
        if self.restrict:
            return []
        fs = self.d.fs
        scopes = [j.cgroup.rstrip("/") for j in self.d.jobs.jobs.values()]
        out = []
        for p in proctree.table(fs).values():
            if p.comm != "java":
                continue
            args = proctree.cmdline(fs, p.pid)
            if not (args and jvm_engine.is_build_daemon(p.comm, args)):
                continue
            cg = proctree.cgroup_of(fs, p.pid) or ""
            out.append(jvm_engine.Jvm(p.pid, p.start, any(cg == s or cg.startswith(s + "/") for s in scopes)))
        return out

    # ---- tick ------------------------------------------------------------------------------------

    def tick(self, s: dict) -> None:
        eff = self.d.limits.effective()
        self._expire_human()
        actions = self.engine.step(
            s["memory"]["avail_mb"], s["swap"]["used_mb"], eff["ram_floor_gb"], self.cfg.enforce,
            self.collect_trees, self.collect_jvm,
        )  # fmt: skip
        if any(a.op == "pause" and a.entry.real for a in actions) and not self.d._save_state():
            for a in actions:  # intent first; if it cannot be written, fail closed: pause nothing
                if a.op == "pause" and a.entry.real:
                    self._refuse_pause(a.entry)
            actions = [a for a in actions if a.op != "pause" or not a.entry.real]
        for a in actions:
            self._execute(a)
        self._orphans()
        self._foreign(s)
        self.flush()

    def _execute(self, a: Action) -> None:
        self.dirty = True
        if a.op == "pause":
            if not self._pause(a.entry):
                self.engine.drop(a.entry.id)
                self.engine.events = [ev for ev in self.engine.events if ev.get("entry") != a.entry.id]
                self.engine.note("PAUSE_FAILED", f"PAUSE_FAILED {a.entry.what}", False, a.entry.project)
        elif a.op == "resume":
            self._thaw(a.entry)
        elif a.op == "abort" and not a.would:
            pid, start = a.tree.rustc
            res = self.signals.send(pid, start, signal.SIGTERM)
            log.info("memguard: SIGTERM rustc %d: %s", pid, res)
        elif a.op == "reap" and not a.would:
            for j in a.jvm:
                self.signals.send(j.pid, j.start, signal.SIGTERM)

    def _refuse_pause(self, e: Entry) -> None:
        self.engine.drop(e.id)
        self.engine.events = [ev for ev in self.engine.events if ev.get("entry") != e.id]
        self.engine.note("REFUSED", f"REFUSED pause {e.what}: persist failed", False, e.project)

    def _inside(self, t: dict) -> bool:
        cg = t.get("cgroup") if "cgroup" in t else proctree.cgroup_of(self.d.fs, t.get("pid", 0))
        return bool(cg) and cg.startswith(self.restrict)

    # ---- pause / thaw ----------------------------------------------------------------------------

    def _pause(self, e: Entry) -> bool:
        if not e.real:
            return True
        fs = self.d.fs
        if self.restrict:  # defence in depth for the host tests: nothing outside the prefix, ever
            e.targets = [t for t in e.targets if self._inside(t)]
            if not e.targets:
                return False
        try:
            if e.how == "freeze":
                for t in e.targets:  # backstop: a scope with an agent in it is never frozen
                    for pid in proctree.scope_pids(fs, t["cgroup"]):
                        st = self.signals.stat(pid)
                        if st and st[0] == AGENT_COMM:
                            log.error("memguard: refusing to freeze %s: the agent runs in it", t["cgroup"])
                            return False
                return all(cgroup.write_freeze(fs, t["cgroup"], True, self.cfg.enforce) == "ok"
                           for t in e.targets)  # fmt: skip
            if e.how == "sigstop":
                sent = [t for t in e.targets
                        if self.signals.send(t["pid"], t["start"], signal.SIGSTOP) == "sent"]  # fmt: skip
                e.targets = sent
                return bool(sent)
            return all(self.docker.pause(t["name"]) == "ok" for t in e.targets)
        except (OSError, ValueError) as e2:
            log.error("memguard: cannot pause %s: %s", e.what, e2)
            return False

    def _thaw(self, e: Entry) -> bool:
        """Undo the pause; True when every step reported success (or its target is gone)."""
        if not e.real:
            return True
        ok = True
        for t in e.targets:
            try:
                if e.how == "freeze":
                    cgroup.write_freeze(self.d.fs, t["cgroup"], False, self.cfg.enforce)
                elif e.how == "sigstop":
                    ok &= self.signals.send(t["pid"], t["start"], signal.SIGCONT) in (
                        "sent",
                        "gone",
                        "changed",
                    )
                else:
                    ok &= self.docker.unpause(t["name"]) == "ok"
            except (OSError, ValueError) as err:
                log.warning("memguard: cannot thaw %s: %s", e.what, err)
                ok = False
        return ok

    def _tree_thawed(self, e: Entry, thaw_ok: bool) -> bool:
        """Confirmed not paused: no frozen scope, no process of the entry in state T (a docker pause
        cannot be read cheaply, so it counts as thawed when the unpause succeeded)."""
        if e.how == "freeze":
            for t in e.targets:
                if cgroup.read_freeze(self.d.fs, t["cgroup"]):
                    return False
                for pid in proctree.scope_pids(self.d.fs, t["cgroup"]):
                    st = self.signals.stat(pid)
                    if st and st[1] in ("T", "t"):
                        return False
            return True
        if e.how == "sigstop":
            return not self._still_paused(e)
        return thaw_ok

    # ---- orphan reaper ---------------------------------------------------------------------------

    def _target_pids(self, e: Entry) -> list[tuple[int, int | None]]:
        if e.how == "freeze":
            return [
                (p, self._start_of(p)) for t in e.targets for p in proctree.scope_pids(self.d.fs, t["cgroup"])
            ]  # start recorded now, so a reused pid is "changed", not signalled
        if e.how == "sigstop":
            return [(t["pid"], t["start"]) for t in e.targets]
        return []

    def _orphan_send(self, e: Entry, sig: signal.Signals) -> dict[str, int]:
        """Signal every target; the real results (sent, gone, changed, refused, would, error) are counted."""
        out: dict[str, int] = {}
        for pid, start in self._target_pids(e):
            r = self.reap_signals.send(pid, start, sig)
            r = "error" if r.startswith("error") else r
            out[r] = out.get(r, 0) + 1
        return out

    @staticmethod
    def _summary(res: dict[str, int]) -> str:
        return ", ".join(f"{n} {k}" for k, n in sorted(res.items())) or "no target"

    def _orphans(self) -> None:
        now = self.d.clock()
        for kind, e in reaper.plan(now, self.engine.paused, self._session_alive, self._targets_alive):
            if self._retry_after.get(e.id, 0.0) > now:
                continue  # could not be thawed for a while: retried every 60 s, not every tick
            if kind == "done":
                self.engine.drop(e.id)
                self.engine.note("REAP", f"REAPED orphaned {e.what}: gone", False, e.project, entry=e.id)
                self.dirty = True
                continue
            if not self.reaper_cfg.enforce:
                if e.id not in self._would_reap:
                    self._would_reap.add(e.id)
                    self.engine.note("REAP", f"REAP orphaned {e.what}: its session is gone", True,
                                     e.project, entry=e.id)  # fmt: skip
                continue
            thaw_ok = True
            if kind == "term":
                thaw_ok = self._thaw(e)
                res = self._orphan_send(e, signal.SIGTERM)
                what, word = "thawed, SIGTERM", "term"
                if res.get("sent"):
                    e.term_at = now
            else:
                res = self._orphan_send(e, signal.SIGKILL)
                what, word = "still alive after 60 s, SIGKILL", "kill"
                e.killed = bool(res.get("sent"))
            if not res.get("sent"):
                e.reap_tries += 1
            thawed = self._tree_thawed(e, thaw_ok)
            if res and set(res) <= {"changed", "gone"} and thawed:
                why = "every target is gone or changed"
            elif not res.get("sent") and e.reap_tries >= reaper.MAX_TERM_TRIES and thawed:
                why = f"giving up after {e.reap_tries} rounds without a signal sent ({self._summary(res)})"
            else:
                why = None
            if not thawed and not res.get("sent") and e.reap_tries >= reaper.MAX_TERM_TRIES:
                # never forget work that may still be stopped; say so once and keep trying slowly
                self._retry_after[e.id] = now + 60.0
                if e.id not in self._stuck_alerted:
                    self._stuck_alerted.add(e.id)
                    self.engine.note("REAP", f"REAP orphaned {e.what}: cannot be thawed after "
                                     f"{e.reap_tries} rounds; still paused, retrying every 60 s", False,
                                     e.project, entry=e.id)  # fmt: skip
                    self.d.emit_event(
                        "stuck_paused",
                        {
                            "message": f"Orphaned paused work {e.what} "
                            "could not be thawed; retrying every 60 s"
                        },
                    )
            if why:  # nothing left to signal, or nothing will ever be signalled: do not retry forever
                self.engine.drop(e.id)
                self.engine.note("REAP", f"REAPED orphaned {e.what}: {why}", False, e.project, entry=e.id)
                self.dirty = True
                continue
            summary = self._summary(res)
            if self._reap_seen.get((e.id, word)) != summary:  # a refusal repeats every tick: say it once
                self._reap_seen[(e.id, word)] = summary
                self.engine.note("REAP", f"REAP orphaned {e.what}: {what} -> {summary}", False, e.project,
                                 entry=e.id)  # fmt: skip
            self.dirty = True

    def _foreign(self, s: dict) -> None:
        ours = {t["pid"] for e in self.engine.paused for t in e.targets if "pid" in t}
        self.foreign = [x for x in s.get("stopped", []) if x["pid"] not in ours]
        now = {x["pid"] for x in self.foreign}
        for x in self.foreign:
            if x["pid"] not in self._seen_foreign:
                self.engine.note("REPORT", f"REPORT stopped process {x['pid']} ({x['comm']}) in {x['cgroup']}"
                                 " is not ours; left alone", False)  # fmt: skip
        self._seen_foreign = now

    # ---- events, marker, state -------------------------------------------------------------------

    def flush(self) -> None:
        if self.dirty:
            self._sync_marker()
            self.d._save_state()
            self.dirty = False
        for ev in self.engine.drain_events():
            text = ("WOULD " if ev["would"] else "") + ev["text"]
            self.d.audit.append({
                "ts": iso(ev["at"]), "op": "memguard", "principal": "system", "source": "memguard",
                "project": ev.get("project"), "reason": text,
                "result": "would" if ev["would"] else RESULTS.get(ev["kind"], "applied"),
                "request": {"event": ev["kind"]},
            })  # fmt: skip
            if self.cfg.log_path:
                try:
                    with open(self.cfg.log_path, "a") as f:
                        f.write(f"{iso(ev['at'])} {text}\n")
                except OSError as err:
                    log.warning("cannot append to memguard log %s: %s", self.cfg.log_path, err.strerror)
            hook = HA_KINDS.get(ev["kind"])
            if hook and not ev["would"]:
                self.d.emit_event(hook, {"message": text, "avail_mb": self.engine.avail,
                                         "swap_mb": self.engine.swap})  # fmt: skip

    def _sync_marker(self) -> None:
        """[memguard.pause_marker]: the file exists while the marker's project has work paused (written only
        with enforce; removed only if this daemon wrote it, so a legacy guard's own marker is left alone)."""
        marker = self.cfg.pause_marker
        if marker is None:
            return
        path = self.d.fs.lock_path(marker.file)
        want = any(e.real and e.project == marker.project for e in self.engine.paused)
        try:
            if want and (self.cfg.enforce or self._marker_owned):
                with open(path, "w") as f:
                    f.write("paused\n")
                self._marker_owned = True
            elif not want and self._marker_owned:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(path)
                self._marker_owned = False
        except OSError as err:
            log.warning("cannot update %s: %s", path, err.strerror)

    def status(self) -> dict:
        return {
            **self.engine.status(self.cfg.enforce),
            "reaper_enforce": self.reaper_cfg.enforce,
            "foreign_stopped": [{"pid": x["pid"], "comm": x["comm"], "cgroup": x["cgroup"]}
                                for x in self.foreign],
        }  # fmt: skip

    # ---- human freeze ----------------------------------------------------------------------------

    def _expire_human(self) -> None:
        now = self.d.clock()
        for e in [e for e in self.engine.paused if e.kind == "human" and e.expires and e.expires <= now]:
            self._thaw(e)
            self.engine.drop(e.id)
            self.engine.note("UNFREEZE", f"UNFREEZE {e.what}: ttl expired", not e.real, e.project, entry=e.id)
            self.dirty = True
        h = self.engine.human
        if h and h.get("expires") and h["expires"] <= now:
            self.d.limits.set_flag("freeze", None)
            self.engine.human = None
            self.dirty = True
            self.d._after_change({})

    def human_freeze(self, project: str | None, reason: str, ttl_s: int | None) -> dict:
        now = self.d.clock()
        expires = now + ttl_s if ttl_s else None
        enforce, held = self.cfg.enforce, {e.key for e in self.engine.paused}
        frozen, failed = [], []
        new = [
            self.engine.add_human(t, reason, expires, enforce)
            for t in self.collect_trees()
            if t.key not in held and not (project and t.project != project)
        ]
        if enforce and new and not self.d._save_state():  # intent first; fail closed
            for e in new:
                self._refuse_pause(e)
            new = []
            failed.append("persist failed")
        for e in new:
            if self._pause(e):
                frozen.append(e.id)
                self.engine.note("FREEZE", f"FREEZE {e.what}: {reason}", not enforce, e.project, entry=e.id)
            else:
                self.engine.drop(e.id)
                failed.append(e.what)
        if project is None:  # a project freeze must not stop every other project's starts
            self.d.limits.set_flag("freeze", reason)
            self.engine.human = {"reason": reason, "since": now, "expires": expires}
        self.dirty = True
        return {"ok": True, "frozen": frozen, "failed": failed, "would": not enforce,
                "expires_at": iso(expires), "project": project}  # fmt: skip

    def human_unfreeze(self, project: str | None) -> dict:
        thawed, still = [], []
        for e in list(self.engine.paused):
            if project and e.project != project:
                continue
            if e.kind == "human" or self.engine.may_release(e):
                self._thaw(e)
                self.engine.drop(e.id)
                self.engine.note("UNFREEZE", f"UNFREEZE {e.what}", not e.real, e.project, entry=e.id)
                thawed.append(e.id)
            else:
                still.append(e)
        flag_cleared = False
        if project is None and (self.engine.human or self.d.limits.state.freeze):
            self.d.limits.set_flag("freeze", None)
            self.engine.human, flag_cleared = None, True
        self.dirty = True
        return {"thawed": thawed, "still": still, "flag_cleared": flag_cleared}

    def reap_now(self, enforce: bool, out_of_scope_only: bool = True) -> list[dict]:
        out = []
        for j in self.collect_jvm():
            if out_of_scope_only and j.in_scope:
                continue  # a daemon serving a running job is never an idle daemon
            res = self.signals.send(j.pid, j.start, signal.SIGTERM) if enforce else "would"
            out.append({"pid": j.pid, "start": j.start, "in_scope": j.in_scope, "result": res})
        return out


# ---- socket / REST ops ---------------------------------------------------------------------------


def handle(d, op: str, args: dict, peer_pid: int | None) -> dict:
    return _dispatch(d, op, args, peer_pid, None)


def rest(d, op: str, args: dict, peer: str) -> dict:
    return _dispatch(d, op, args, None, peer)


def _dispatch(d, op, args, peer_pid, rest_peer):
    allowed = {"freeze": {"project", "reason", "ttl", "owner"}, "unfreeze": {"project", "reason", "owner"},
               "reap": {"reason", "owner", "pre_gate"}}[op]  # fmt: skip
    unknown = sorted(set(args) - allowed)
    if unknown:
        return _err("bad_request", f"unknown argument(s): {', '.join(unknown)}")
    if "owner" in args and not isinstance(args["owner"], bool):
        return _err("bad_request", "owner must be a boolean")
    if "pre_gate" in args and not isinstance(args["pre_gate"], bool):
        return _err("bad_request", "pre_gate must be a boolean")
    project, bad = _text(args, "project", required=False)
    if bad:
        return bad
    ctx = {**caller_info(d.fs, peer_pid), "op": op, "request": args, "project": project}
    principal = "agent"
    if rest_peer is not None:
        ctx.update(source="rest", peer=rest_peer)
        principal = "owner"
    elif op != "reap" or args.get("owner") is True:  # reap frees memory: an agent may do it too
        why = owner_denial(d.fs, peer_pid, d.config.principal)
        if why or args.get("owner") is not True:
            why = why or "owner: true is required for this operation"
            d.audit.append({"ts": iso(d.clock()), **ctx, "principal": "agent", "result": "refused",
                            "reason": why})  # fmt: skip
            return _err("owner_denied", why)
        principal = "owner"
    reason, bad = _text(args, "reason", required=op == "freeze" and rest_peer is None)
    if bad:
        return bad
    if reason is None and rest_peer is not None:
        reason = f"REST {op} from {rest_peer}"
    d._expire()
    mg: MemGuardRuntime = d.memguard
    ctx.update(principal=principal, reason=reason)
    if op == "reap":
        return _reap(d, mg, args, ctx, reason or "reap-daemons")
    if op == "freeze":
        try:
            ttl = parse_ttl(args["ttl"]) if args.get("ttl") is not None else None
        except (ValueError, TypeError) as e:
            return _err("invalid_args", str(e))
        reply = mg.human_freeze(project, reason, ttl)
        d.audit.append({"ts": iso(d.clock()), **ctx, "result": "would" if reply["would"] else "applied",
                        "after": f"froze {len(reply['frozen'])} tree(s)"})  # fmt: skip
        d._after_change(ctx)
        if rest_peer is None:
            d.notify_owner_change(ctx, "freeze")
        return reply
    r = mg.human_unfreeze(project)
    still = r["still"]
    if still and not r["thawed"] and not r["flag_cleared"]:
        why = f"memory is still below the resume threshold ({mg.engine.thresholds_view()[1]} MB)"
        d.audit.append(
            {"ts": iso(d.clock()), **ctx, "result": "refused", "reason": f"restrictedBy system: {why}"}
        )
        return {"ok": False, "refused": True, "restrictedBy": "system", "reason": why,
                "still_paused": [e.id for e in still]}  # fmt: skip
    kept = f"thawed {len(r['thawed'])} tree(s), {len(still)} guard pause(s) kept"
    d.audit.append({"ts": iso(d.clock()), **ctx, "result": "applied", "after": kept})
    d._after_change(ctx)
    if rest_peer is None:
        d.notify_owner_change(ctx, "unfreeze")
    reply = {"ok": True, "thawed": r["thawed"], "flag_cleared": r["flag_cleared"],
             "still_paused": [e.id for e in still]}  # fmt: skip
    if still:
        reply["restrictedBy"] = "system"
    return reply


def _reap(d, mg: MemGuardRuntime, args: dict, ctx: dict, reason: str) -> dict:
    enforce = mg.cfg.enforce
    if args.get("pre_gate"):  # `loadctl run --kind gate`: only with enforce and below pre_gate_reap_gb
        gb, avail = mg.cfg.pre_gate_reap_gb, (d.status or {}).get("memory", {}).get("avail_mb")
        if not enforce or gb <= 0 or avail is None or avail >= gb * 1024:
            why = "enforce is off" if not enforce else "disabled" if gb <= 0 else "enough memory"
            return {"ok": True, "reaped": [], "skipped": why, "would": False}
    reaped = mg.reap_now(enforce, ctx["principal"] != "owner" or bool(args.get("pre_gate")))
    if reaped or not args.get("pre_gate"):
        pids = [r["pid"] for r in reaped]
        d.audit.append({
            "ts": iso(d.clock()), **ctx, "result": "applied" if enforce else "would",
            "reason": ("" if enforce else "WOULD ") + f"REAP build daemons {pids}: {reason}",
        })  # fmt: skip
    return {"ok": True, "reaped": reaped, "would": not enforce}
