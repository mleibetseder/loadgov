"""Per-job resource use: own scope, host and attributed Docker CPU; the rolling summary; loadctl jobs --stats."""

import errno
import json
import shutil

import pytest

from loadgov.cli import loadctl
from loadgov.daemon.state import StateStore
from loadgov.engines.jobs import Job, JobTable
from loadgov.engines.jobstats import KEEP, MAX_PAIRS, JobStats
from loadgov.host.procfs import HostFS

CID_A, CID_B, CID_C = ("a" * 64, "b" * 64, "c" * 64)
T0 = 1_762_800_000.0


class Rig:
    def __init__(self, tmp_path):
        self.cg, self.docker = tmp_path / "cg", tmp_path / "docker"
        self.cg.mkdir()
        self.docker.mkdir()
        self.t = T0
        fs = HostFS(proc_root=str(tmp_path / "proc"), cgroup_root=str(self.cg))
        self.jt = JobTable(fs, lambda: self.t, docker_root=str(self.docker), pattern=self.pattern)
        self.patterns: dict = {}
        self.cpu("/", 1_000_000)

    def pattern(self, project):
        return self.patterns.get(project)

    def cpu(self, cgroup, usec):
        d = self.cg / cgroup.strip("/")
        d.mkdir(parents=True, exist_ok=True)
        (d / "cpu.stat").write_text(f"usage_usec {usec}\nuser_usec 1\n")

    def peak(self, cgroup, nbytes):
        (self.cg / cgroup.strip("/") / "memory.peak").write_text(f"{nbytes}\n")

    def container(self, cid, usec, name=None, project=None, config=True):
        self.cpu(f"system.slice/docker-{cid}.scope", usec)
        if config:
            labels = {"com.docker.compose.project": project} if project else {}
            d = self.docker / cid
            d.mkdir(exist_ok=True)
            (d / "config.v2.json").write_text(json.dumps({"Name": f"/{name}", "Config": {"Labels": labels}}))

    def drop_container(self, cid):
        shutil.rmtree(self.cg / "system.slice" / f"docker-{cid}.scope")

    def job(self, n=1, kind="gate", project="proj-a", slot="heavy", scope=True):
        unit = f"loadgov-{kind}-{n}.scope"
        cgroup = f"/system.slice/{unit}"
        if scope:
            self.cpu(cgroup, 0)
        j = Job(kind, project, "l", unit, cgroup, 100 + n, 1, 100 + n, slot, self.t)
        self.jt.add(j)
        return j

    def sample(self):
        self.t += 5
        self.jt.meter.sample()

    def end(self, j):
        self.t += 1
        self.jt.end(j.unit)
        return self.jt.drain_events()[-1]


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def test_usage_is_read_at_job_end_into_the_audit_event(rig):
    j = rig.job()
    rig.cpu(j.cgroup, 3_500_000)
    rig.peak(j.cgroup, 50 * 2**20)
    rig.cpu("/", 9_000_000)
    rig.t += 59
    ev = rig.end(j)
    assert (ev["cpu_s"], ev["mem_peak_mb"], ev["duration_s"]) == (3.5, 50.0, 60.0)
    assert ev["host_cpu_s"] == 8.0 and ev["docker_cpu_s"] == 0.0
    assert ev["op"] == "job_end"


def test_scope_gone_falls_back_to_the_last_tick_sample(rig):
    j = rig.job()
    rig.cpu(j.cgroup, 2_000_000)
    rig.peak(j.cgroup, 10 * 2**20)
    rig.sample()
    shutil.rmtree(rig.cg / "system.slice" / j.unit)
    ev = rig.end(j)
    assert (ev["cpu_s"], ev["mem_peak_mb"]) == (2.0, 10.0)


def test_enodev_and_enoent_mean_unknown(rig, monkeypatch):
    j = rig.job(scope=False)  # the scope never answered a read
    real = open

    def flaky(path, *a, **k):
        if str(path).startswith(str(rig.cg / "system.slice" / j.unit)):
            raise OSError(errno.ENODEV, "No such device")
        return real(path, *a, **k)

    monkeypatch.setattr("builtins.open", flaky)
    ev = rig.end(j)
    monkeypatch.undo()
    assert "cpu_s" in ev and ev["cpu_s"] is None and ev["mem_peak_mb"] is None
    assert ev["duration_s"] == 1.0
    rec = rig.jt.stats.summary()[0]
    assert rec["count"] == 1 and rec["cpu_p50_s"] is None and rec["mem_peak_p50_mb"] is None


def test_no_memory_peak_file_is_null(rig):
    j = rig.job()
    rig.cpu(j.cgroup, 1_000_000)
    assert rig.end(j)["mem_peak_mb"] is None


# ---- Docker attribution --------------------------------------------------------------------------


def test_heavy_slot_job_takes_all_docker_cpu_incl_buildkit_and_unreadable_config(rig):
    rig.container(CID_A, 500_000, "buildx_buildkit_builder0")  # exists before the job: baseline only
    rig.container(CID_B, 0, config=False)
    j = rig.job()
    rig.container(CID_A, 4_500_000, "buildx_buildkit_builder0")
    rig.container(CID_B, 1_000_000, config=False)
    rig.sample()
    ev = rig.end(j)
    assert ev["docker_cpu_s"] == 5.0
    assert ev["cpu_s"] == 0.0  # the scope itself only held the CLI


def test_container_appearing_and_disappearing_mid_job_is_counted(rig):
    j = rig.job()
    rig.container(CID_A, 2_000_000, "x")  # appears with 2 s already used
    rig.sample()
    rig.container(CID_A, 3_000_000, "x")
    rig.sample()
    rig.drop_container(CID_A)
    rig.container(CID_B, 1_000_000, "y")
    rig.sample()
    assert rig.end(j)["docker_cpu_s"] == 4.0


def test_compose_label_and_name_prefix_attribute_without_the_heavy_slot(rig):
    rig.container(CID_A, 0, "web", project="proj-a")
    rig.container(CID_B, 0, "proj-a-db-1")
    rig.container(CID_C, 0, "other-thing", project="other")
    j = rig.job(kind="impl", slot=None)
    rig.container(CID_A, 1_000_000, "web", project="proj-a")
    rig.container(CID_B, 2_000_000, "proj-a-db-1")
    rig.container(CID_C, 4_000_000, "other-thing", project="other")
    rig.sample()
    assert rig.end(j)["docker_cpu_s"] == 3.0


def test_memguard_containers_regex_attributes(rig):
    rig.patterns["proj-a"] = "^ci-run-"
    rig.container(CID_A, 0, "ci-run-7")
    j = rig.job(kind="impl", slot=None)
    rig.container(CID_A, 2_000_000, "ci-run-7")
    rig.sample()
    assert rig.end(j)["docker_cpu_s"] == 2.0


def test_project_match_wins_over_the_heavy_slot_and_nothing_is_double_counted(rig):
    rig.container(CID_A, 0, "proj-b-api-1")
    rig.container(CID_B, 0, "unrelated")
    gate = rig.job(1, kind="gate", project="proj-a", slot="heavy")
    impl = rig.job(2, kind="impl", project="proj-b", slot=None)
    rig.container(CID_A, 3_000_000, "proj-b-api-1")  # proj-b's container: not the gate's
    rig.container(CID_B, 5_000_000, "unrelated")  # no project: the heavy-slot job
    rig.sample()
    assert rig.end(gate)["docker_cpu_s"] == 5.0
    assert rig.end(impl)["docker_cpu_s"] == 3.0


def test_two_matching_jobs_split_the_container_equally(rig):
    rig.container(CID_A, 0, "proj-a-x")
    one, two = rig.job(1, kind="impl", slot=None), rig.job(2, kind="review", slot=None)
    rig.container(CID_A, 4_000_000, "proj-a-x")
    rig.sample()
    assert (rig.end(one)["docker_cpu_s"], rig.end(two)["docker_cpu_s"]) == (2.0, 2.0)


def test_non_heavy_job_without_a_match_gets_nothing(rig):
    rig.container(CID_A, 0, "elsewhere")
    j = rig.job(kind="impl", slot=None)
    rig.container(CID_A, 9_000_000, "elsewhere")
    rig.sample()
    assert rig.end(j)["docker_cpu_s"] == 0.0


def test_a_container_that_ran_before_the_job_is_not_charged_its_history(rig):
    rig.container(CID_A, 900_000_000, "old")
    j = rig.job()
    rig.sample()
    assert rig.end(j)["docker_cpu_s"] == 0.0


def test_no_docker_scopes_costs_nothing_and_idle_table_never_samples(rig):
    rig.jt.meter.sample()
    assert rig.jt.meter.primed is False


# ---- the summary ---------------------------------------------------------------------------------


def rec(i):
    return {"duration_s": float(i), "cpu_s": float(i), "docker_cpu_s": 10.0, "host_cpu_s": 2.0 * i,
            "mem_peak_mb": 100.0 + i}  # fmt: skip


def test_summary_maths():
    s = JobStats()
    for i in range(1, 6):
        s.record("p", "gate", rec(i))
    s.record("p", "impl", {"duration_s": 4.0, "cpu_s": None, "docker_cpu_s": None, "mem_peak_mb": None})
    gate = next(x for x in s.summary() if x["kind"] == "gate")
    assert gate["count"] == 5 and gate["duration_sum_s"] == 15.0 and gate["duration_p50_s"] == 3.0
    assert gate["cpu_total_s"] == 15.0 and gate["cpu_p50_s"] == 3.0 and gate["cpu_p95_s"] == 4.8
    assert gate["docker_cpu_p50_s"] == 10.0 and gate["total_cpu_p50_s"] == 13.0
    assert gate["total_cpu_p95_s"] == 14.8 and gate["host_cpu_p50_s"] == 6.0
    assert (gate["mem_peak_p50_mb"], gate["mem_peak_p95_mb"], gate["mem_peak_max_mb"]) == (
        103.0,
        104.8,
        105.0,
    )
    impl = next(x for x in s.summary() if x["kind"] == "impl")
    assert impl["count"] == 1 and impl["cpu_p50_s"] is None and impl["mem_peak_max_mb"] is None


def test_only_the_last_200_jobs_feed_percentiles_but_count_and_max_cover_all():
    s = JobStats()
    s.record("p", "gate", {**rec(1), "mem_peak_mb": 9999.0})
    for _ in range(KEEP):
        s.record("p", "gate", rec(7))
    g = s.summary()[0]
    assert g["count"] == KEEP + 1 and g["duration_p50_s"] == 7.0 and g["mem_peak_max_mb"] == 9999.0
    assert g["mem_peak_p95_mb"] == 107.0


def test_summary_survives_a_restart(tmp_path):
    s = JobStats()
    for i in range(1, 4):
        s.record("p", "gate", rec(i))
    store = StateStore(str(tmp_path))
    from loadgov.engines.limits import LimitState

    store.save(LimitState("balanced"), job_stats=s.to_state())
    back = JobStats()
    back.restore(StateStore(str(tmp_path)).load_job_stats())
    assert back.summary() == s.summary()
    back.restore([{"project": "x"}, "junk"])  # unreadable entries are skipped
    assert len(back.summary()) == 1


def test_running_job_keeps_its_usage_across_a_state_roundtrip(rig):
    j = rig.job()
    rig.container(CID_A, 1_000_000, "x")
    rig.sample()
    rig.container(CID_A, 3_000_000, "x")
    rig.sample()
    again = Job(**json.loads(json.dumps(rig.jt.to_state()[0])))
    assert again.docker_us == 3_000_000 and again.host_us0 == j.host_us0


# ---- loadctl jobs --stats ------------------------------------------------------------------------


def test_loadctl_jobs_stats_table_and_json(monkeypatch, capsys):
    s = JobStats()
    for i in range(1, 4):
        s.record("proj-a", "gate", rec(i))
    s.record("proj-a", "review", {"duration_s": 5.0})
    status = {"ok": True, "status": {"job_stats": s.summary(), "jobs": []}}
    monkeypatch.setattr(loadctl, "request", lambda sock, op, args: status)
    assert loadctl.main(["jobs", "--stats"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split()[:3] == ["project", "kind", "jobs"] and "docker cpu p50/p95 s" in lines[0]
    assert lines[1].split()[:4] == ["proj-a", "gate", "3", "2"]
    assert "10.0/10.0" in lines[1] and "12.0/12.9" in lines[1] and "102/103/103" in lines[1]
    assert lines[2].split()[:2] == ["proj-a", "review"] and "-" in lines[2]
    assert loadctl.main(["jobs", "--stats", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["count"] == 3
    monkeypatch.setattr(loadctl, "request", lambda sock, op, args: {"ok": True, "status": {"job_stats": []}})
    assert loadctl.main(["jobs", "--stats"]) == 0
    assert capsys.readouterr().out.strip() == "no finished jobs recorded"


def test_a_failed_docker_listing_keeps_the_counters(rig):
    rig.container(CID_A, 900_000_000, "old")
    j = rig.job()
    shutil.move(rig.cg / "system.slice", rig.cg / "hidden")  # the listing fails or comes back empty
    rig.sample()
    shutil.move(rig.cg / "hidden", rig.cg / "system.slice")
    rig.container(CID_A, 901_000_000, "old")
    rig.sample()
    assert rig.end(j)["docker_cpu_s"] == 1.0  # only the real delta, not the container's whole lifetime


def test_summaries_are_capped_at_64_pairs_least_recently_updated_goes_first():
    s = JobStats()
    for i in range(MAX_PAIRS):
        s.record(f"p{i}", "gate", rec(1))
    s.record("p0", "gate", rec(1))  # p0 is now the most recent, p1 the oldest
    s.record("new", "gate", rec(1))
    names = {x["project"] for x in s.summary()}
    assert len(names) == MAX_PAIRS and "p1" not in names and {"p0", "new"} <= names
    back = JobStats()
    back.restore(s.to_state())
    assert {x["project"] for x in back.summary()} == names
