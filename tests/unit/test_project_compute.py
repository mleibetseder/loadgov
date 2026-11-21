"""Per-project CPU and memory from the build-work slices (engines/projects.py) and `loadctl status`."""

from loadgov.cli import loadctl
from loadgov.engines.jobs import Job
from loadgov.engines.projects import ROOT, ProjectMeter, nested_slice
from loadgov.host.procfs import HostFS
from loadgov.protocol import validate_status

T0 = 1_762_800_000.0
MB = 2**20


class Rig:
    def __init__(self, tmp_path, configured=(), waiting=(), limit=64):
        self.cg = tmp_path / "cg"
        self.cg.mkdir()
        self.t = T0
        self.jobs: dict = {}
        self.waiting = list(waiting)
        fs = HostFS(proc_root=str(tmp_path / "proc"), cgroup_root=str(self.cg))
        self.m = ProjectMeter(fs, lambda: self.jobs, lambda: self.waiting, lambda: list(configured),
                              lambda: self.t, limit=limit)  # fmt: skip

    def write(self, path, usec=None, mem=None):
        d = self.cg / path.strip("/")
        d.mkdir(parents=True, exist_ok=True)
        if usec is not None:
            (d / "cpu.stat").write_text(f"usage_usec {usec}\nuser_usec 1\n")
        if mem is not None:
            (d / "memory.current").write_text(f"{mem}\n")

    def job(self, project, scope_parent, unit="u1.scope"):
        j = Job("impl", project, "l", unit, f"{scope_parent}/{unit}", 10, 1, 10, None, self.t)
        self.jobs[unit] = j
        return j

    def prime(self):
        assert self.m.sample()
        return self.m.summary()

    def step(self, dt=5.0):
        self.t += dt
        assert self.m.sample()
        return self.m.summary()


def test_nested_slice_follows_the_dash_prefix_rule():
    assert nested_slice("web-app") == f"{ROOT}/build-work-web.slice/build-work-web-app.slice"
    assert nested_slice("web") == f"{ROOT}/build-work-web.slice"
    assert nested_slice("my proj!") == f"{ROOT}/build-work-my.slice/build-work-my-proj.slice"


def test_first_sample_is_null_then_delta_and_cores(tmp_path):
    r = Rig(tmp_path)
    sl = "/build.slice/build-work.slice/build-work-web.slice"
    r.write(sl, 1_000_000, 3 * 1024 * MB)
    r.job("web", sl)
    first = r.prime()["web"]
    assert first["cpu_pct"] is None and first["cores"] is None
    assert first["mem_mb"] == 3072 and first["jobs"] == 1 and first["slice"] == sl
    r.write(sl, 1_000_000 + 12_500_000, 3 * 1024 * MB)  # 12.5 s of CPU in 5 s
    p = r.step()["web"]
    assert (p["cpu_pct"], p["cores"], p["docker_cpu_pct"]) == (250.0, 2.5, 0)


def test_nested_project_slice_is_read_without_a_job(tmp_path):
    r = Rig(tmp_path, configured=["web-app"])
    sl = nested_slice("web-app")
    r.write(sl, 0, MB)
    r.prime()
    r.write(sl, 2_500_000, MB)
    assert r.step()["web-app"]["cpu_pct"] == 50.0


def test_rate_limited_to_the_interval(tmp_path):
    r = Rig(tmp_path, configured=["web"])
    assert r.m.sample()
    r.t += 1
    assert not r.m.sample()


def test_counter_reset_gives_null_then_recovers(tmp_path):
    r = Rig(tmp_path, configured=["web"])
    sl = nested_slice("web")
    r.write(sl, 5_000_000)
    r.prime()
    r.write(sl, 100)  # the slice was recreated
    assert r.step()["web"]["cpu_pct"] is None
    r.write(sl, 5_000_100)
    assert r.step()["web"]["cpu_pct"] == 100.0


def test_docker_cpu_is_added_per_project(tmp_path):
    r = Rig(tmp_path)
    sa, sb = ROOT + "/build-work-a.slice", ROOT + "/build-work-b.slice"
    r.write(sa, 0)
    r.write(sb, 0)
    ja, jb = r.job("a", sa, "a.scope"), r.job("b", sb, "b.scope")
    r.prime()
    r.write(sa, 5_000_000)
    r.write(sb, 5_000_000)
    ja.docker_us += 10_000_000  # 2 cores of Docker CPU over 5 s
    out = r.step()
    assert (out["a"]["cpu_pct"], out["a"]["docker_cpu_pct"], out["a"]["cores"]) == (300.0, 200.0, 3.0)
    assert (out["b"]["cpu_pct"], out["b"]["docker_cpu_pct"]) == (100.0, 0.0)
    r.write(sa, 10_000_000)
    r.write(sb, 10_000_000)
    assert r.step()["a"]["docker_cpu_pct"] == 0  # only the new interval counts
    assert jb.docker_us == 0


def test_configured_idle_project_shows_zeros_and_missing_files_are_null(tmp_path):
    r = Rig(tmp_path, configured=["idle", "ghost"])
    r.write(nested_slice("idle"), 7, 2 * MB)
    r.prime()
    out = r.step()
    assert out["idle"]["cpu_pct"] == 0.0 and out["idle"]["jobs"] == 0 and out["idle"]["waiting"] == 0
    assert out["idle"]["mem_mb"] == 2
    g = out["ghost"]
    assert (g["cpu_pct"], g["cores"], g["mem_mb"], g["jobs"]) == (None, None, None, 0)
    assert g["slice"] == nested_slice("ghost")


def test_waiters_count_per_project_and_make_it_appear(tmp_path):
    r = Rig(tmp_path, waiting=["q", "q", "other"])
    out = r.prime()
    assert out["q"]["waiting"] == 2 and out["other"]["waiting"] == 1 and out["q"]["jobs"] == 0


def test_bound_on_projects_keeps_active_ones(tmp_path):
    r = Rig(tmp_path, configured=[f"c{i:03d}" for i in range(100)], waiting=["zzz"], limit=64)
    out = r.prime()
    assert len(out) == 64 and "zzz" in out


def test_status_with_projects_validates():
    s = {"projects": {"x": {"cpu_pct": None, "cores": None, "docker_cpu_pct": 0, "mem_mb": None,
                            "jobs": 0, "waiting": 0, "slice": "/a"}}}  # fmt: skip
    assert not [e for e in validate_status(s) if e.startswith("projects")]
    s["projects"]["x"]["jobs"] = "1"
    assert "projects.x.jobs: str" in validate_status(s)


def test_loadctl_status_projects_line_sorted_and_skips_idle():
    def p(cpu, mem, jobs=0, waiting=0):
        return {"cpu_pct": cpu, "cores": None if cpu is None else cpu / 100, "docker_cpu_pct": 0,
                "mem_mb": mem, "jobs": jobs, "waiting": waiting, "slice": "/x"}  # fmt: skip

    line = loadctl.format_projects(
        {"idle": p(0.0, 10), "low": p(80.0, 1024, 1), "web": p(240.0, 6246, 1, 2), "q": p(None, None, 0, 1)}
    )
    assert line == ("web 2.4 cores 6.1 GB (1 job, 2 waiting), low 0.8 cores 1.0 GB (1 job), "
                    "q ? cores (1 waiting)")  # fmt: skip
    assert loadctl.format_projects({"idle": p(0.0, 10)}) == ""


def test_non_dict_projects_reports_the_type_error():
    assert "projects: list" in validate_status({"projects": []})
