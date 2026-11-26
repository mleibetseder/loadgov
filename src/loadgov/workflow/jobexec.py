"""The job script: what `loadctl run --kind <lane> ... --` starts for one workflow job.

    python3 -m loadgov.workflow.jobexec SPEC.json

It runs the steps of the job in order inside the scope, lane and slot that `loadctl run` set up, and
records every step (status, exit code, times, log) in the job record the runner created. It never talks
to the daemon and signals only its own step process groups.

Exit code: 0 when the job succeeded, else the exit code of the first failing step (124 on a timeout,
143 when it was terminated).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

from loadgov.workflow import expr
from loadgov.workflow.records import LogWriter, RunStore, now_iso, read_json

SHELL_ARGV = {
    "bash": ["bash", "--noprofile", "--norc", "-eo", "pipefail", "{0}"],
    "sh": ["sh", "-e", "{0}"],
    "python": ["python3", "{0}"],
    None: ["bash", "-e", "{0}"],  # Actions: bash -e when `shell` is not set
}
TOOLS = {"setup-python": ("python3", "python-version"), "setup-node": ("node", "node-version")}
KILL_GRACE_S = 5.0
MAX_OUTPUTS = 200


def parse_env_file(text: str) -> dict[str, str]:
    """GITHUB_OUTPUT / GITHUB_ENV format: `name=value` lines and `name<<DELIM` ... `DELIM` blocks."""
    out: dict[str, str] = {}
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip():
            continue
        m = re.match(r"^([^=<]+)<<(.+)$", line)
        if m:
            name, delim = m.group(1), m.group(2)
            body = []
            while i < len(lines) and lines[i] != delim:
                body.append(lines[i])
                i += 1
            if i >= len(lines):
                raise ValueError(f"missing delimiter {delim!r} for {name!r}")
            i += 1
            out[name] = "\n".join(body)
        elif "=" in line:
            name, _, value = line.partition("=")
            out[name] = value
        else:
            raise ValueError(f"cannot parse line {line[:40]!r}")
    return out


class Executor:
    def __init__(self, spec: dict, which=shutil.which, clock=time.time, out=None):
        self.spec = spec
        self.which, self.clock = which, clock
        self.out = out or sys.stdout
        self.store = RunStore(spec["runs_dir"])
        self.run_id, self.key = spec["run_id"], spec["key"]
        self.job = read_json(self.store.job_path(self.run_id, self.key))
        self.cancelled = False
        self.proc: subprocess.Popen | None = None
        self.env_dyn: dict[str, str] = {}
        self.steps_ctx: dict[str, dict] = {}
        self.tmp = ""
        self.hit_job_timeout = False
        self.base_env: dict[str, str] = {}

    # ---- helpers ----------------------------------------------------------------------------------
    def say(self, text: str) -> None:
        with contextlib.suppress(OSError, ValueError):
            self.out.write(text + "\n")
            self.out.flush()

    def save(self) -> None:
        self.store.write_job(self.run_id, self.job)

    def github_ctx(self) -> dict:
        sp = self.spec
        return {
            "github": {
                "event_name": sp["event"],
                "ref": sp["ref"],
                "ref_name": sp["ref_name"],
                "sha": sp["sha"],
                "workspace": sp["workspace"],
            },
            "runner": {"os": "Linux"},
            "matrix": sp["matrix"],
            "steps": self.steps_ctx,
        }

    def terminate(self, *_args) -> None:
        self.cancelled = True
        p = self.proc
        if p is not None and p.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(p.pid, signal.SIGTERM)

    # ---- the job ----------------------------------------------------------------------------------
    def run(self) -> int:
        sp, job = self.spec, self.job
        start = self.clock()
        job["status"], job["started_at"] = "running", now_iso(start)
        self.save()
        self.tmp = tempfile.mkdtemp(prefix="loadgov-wf-")
        try:
            prev = signal.signal(signal.SIGTERM, self.terminate)
        except ValueError:  # not the main thread (in-process use): no handler
            prev = None
        deadline = start + sp["timeout_minutes"] * 60 if sp.get("timeout_minutes") else None
        failed = False
        exit_code = 0
        reason = None
        try:
            base = self.github_ctx()
            wf_env = self.layer({}, sp["env"]["workflow"], base)
            job_env = self.layer(wf_env, sp["env"]["job"], base)
            self.base_env = job_env
            for step, rec in zip(sp["steps"], job["steps"], strict=True):
                if self.cancelled or (deadline is not None and self.clock() >= deadline):
                    timed_out = not self.cancelled
                    rec["status"] = "cancelled"
                    rec["note"] = "job timeout-minutes exceeded" if timed_out else "terminated"
                    if timed_out and reason is None:
                        reason, exit_code, failed = "job timeout-minutes exceeded", 124, True
                    continue
                ok_so_far = not failed
                ctx = self.github_ctx() | {"env": self.merged_env(step)}
                status = {"success": ok_so_far, "failure": not ok_so_far, "cancelled": self.cancelled}
                with contextlib.suppress(expr.ExprError):
                    rec["name"] = expr.interpolate(step["label"], ctx)
                try:
                    run_it = expr.should_run(step["cond"], ctx, status)
                except expr.ExprError as e:  # validated at load; defensive
                    run_it = False
                    rec["note"] = f"if: {e}"
                if not run_it:
                    rec["status"] = "skipped"
                    if step["id"]:
                        self.steps_ctx[step["id"]] = {
                            "outputs": {},
                            "outcome": "skipped",
                            "conclusion": "skipped",
                        }
                    self.save()
                    continue
                code = self.run_step(step, rec, ctx, deadline)
                coe = self.eval_coe(step["continue_on_error"], ctx)
                if step["id"]:
                    self.steps_ctx[step["id"]] = {
                        "outputs": rec["outputs"],
                        "outcome": rec["status"],
                        "conclusion": "success" if (coe and rec["status"] == "failure") else rec["status"],
                    }
                rec["continue_on_error"] = coe
                self.save()
                if rec["status"] == "failure" and not coe:
                    if not failed:
                        exit_code = code
                    failed = True
                    if self.hit_job_timeout and reason is None:
                        reason = "job timeout-minutes exceeded"
                if rec["status"] == "cancelled":
                    failed = True
        finally:
            if prev is not None:
                signal.signal(signal.SIGTERM, prev)
            shutil.rmtree(self.tmp, ignore_errors=True)
        end = self.clock()
        job["finished_at"], job["duration_s"] = now_iso(end), round(end - start, 3)
        if self.cancelled:
            job["status"], job["exit_code"], job["reason"] = "cancelled", 143, "terminated (SIGTERM)"
            exit_code = 143
        else:
            job["status"] = "failure" if failed else "success"
            job["exit_code"] = exit_code if failed else 0
            if reason:
                job["reason"] = reason
        self.save()
        return job["exit_code"]

    def eval_coe(self, v, ctx) -> bool:
        if isinstance(v, bool):
            return v
        try:
            return expr.truthy(expr.evaluate(expr.parse_expr(v), ctx))
        except expr.ExprError:
            return False

    def layer(self, below: dict, raw: dict, ctx: dict) -> dict:
        out = dict(below)
        for k, v in raw.items():
            out[k] = expr.interpolate(v, ctx | {"env": out})
        return out

    def merged_env(self, step: dict) -> dict:
        env = {**self.base_env, **self.env_dyn}
        ctx = self.github_ctx()
        env = self.layer(env, step["env"], ctx | {"env": env})
        return env

    # ---- one step ---------------------------------------------------------------------------------
    def run_step(self, step: dict, rec: dict, ctx: dict, deadline: float | None) -> int:
        n = step["index"]
        t0 = self.clock()
        rec["status"], rec["started_at"] = "running", now_iso(t0)
        self.save()
        log = LogWriter(self.store.log_path(self.run_id, rec["log"]))
        self.hit_job_timeout = False
        total = len(self.spec["steps"])
        self.say(f"==> step {n}/{total}: {rec['name']}")
        code = 0
        timed_out = False
        try:
            if step["uses"]:
                code = self.run_uses(step, ctx, log)
            else:
                code, timed_out, self.hit_job_timeout = self.run_script(step, ctx, log, deadline)
        except Exception as e:  # a step must never crash the job script
            log.write(f"loadgov: step could not run: {e}\n".encode())
            code = 126
        finally:
            log.close()
        t1 = self.clock()
        rec["finished_at"], rec["duration_s"] = now_iso(t1), round(t1 - t0, 3)
        rec["exit_code"], rec["timed_out"], rec["log_truncated"] = code, timed_out, log.truncated
        if self.cancelled:
            rec["status"] = "cancelled"
        else:
            rec["status"] = "success" if code == 0 else "failure"
        return code

    def say_log(self, log: LogWriter, text: str) -> None:
        log.write((text + "\n").encode())
        self.say(text)

    def run_uses(self, step: dict, ctx: dict, log: LogWriter) -> int:
        kind = step["uses"]
        if kind == "checkout":
            self.say_log(
                log,
                f"host-only: actions/checkout is a no-op; using the local checkout {self.spec['workspace']}",
            )
            return 0
        tool, key = TOOLS[kind]
        path = self.merged_env(step).get("PATH") or os.environ.get("PATH", "")
        found = self.which(tool, path=path)
        if not found:
            self.say_log(
                log,
                f"host-only: {step['uses_ref']} needs {tool} on this host and it is not installed; "
                f"install it (or run the job on a host that has it). Nothing is downloaded.",
            )
            return 127
        wanted = step["with"].get(key)
        wanted = expr.interpolate(wanted, ctx) if wanted else None
        try:
            have = subprocess.run(
                [found, "--version"], capture_output=True, text=True, timeout=10
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            have = "unknown version"
        msg = f"host-only: {step['uses_ref']}: using {found} ({have})"
        if wanted and wanted.lstrip("v") not in have:
            msg += f"; requested {key} {wanted} is not checked against the installed version"
        self.say_log(log, msg)
        return 0

    def script_argv(self, step: dict, path: str) -> list[str]:
        shell = step["shell"] or self.spec["defaults"].get("shell")
        return [a.replace("{0}", path) for a in SHELL_ARGV[shell]]

    def run_script(self, step: dict, ctx: dict, log: LogWriter, deadline: float | None):
        env_map = ctx["env"]
        text = expr.interpolate(step["run"], ctx)
        suffix = ".py" if (step["shell"] or self.spec["defaults"].get("shell")) == "python" else ".sh"
        script = os.path.join(self.tmp, f"step-{step['index']}{suffix}")
        with open(script, "w", encoding="utf-8") as f:
            f.write(text if text.endswith("\n") else text + "\n")
        out_file = os.path.join(self.tmp, f"output-{step['index']}")
        env_file = os.path.join(self.tmp, f"env-{step['index']}")
        for p in (out_file, env_file):
            open(p, "w").close()  # noqa: SIM115
        sp = self.spec
        env = dict(os.environ)
        env.update(
            {
                "CI": "true",
                "RUNNER_OS": "Linux",
                "GITHUB_WORKSPACE": sp["workspace"],
                "GITHUB_SHA": sp["sha"],
                "GITHUB_REF": sp["ref"],
                "GITHUB_REF_NAME": sp["ref_name"],
                "GITHUB_EVENT_NAME": sp["event"],
                "GITHUB_JOB": sp["job_id"],
                "GITHUB_WORKFLOW": sp["workflow_name"],
                "LOADGOV_RUN_ID": self.run_id,
                "LOADGOV_JOB_KEY": self.key,
                "GITHUB_OUTPUT": out_file,
                "GITHUB_ENV": env_file,
            }
        )
        env.update(env_map)
        wd = step["working_directory"] or self.spec["defaults"].get("working_directory")
        cwd = os.path.join(sp["workspace"], expr.interpolate(wd, ctx)) if wd else sp["workspace"]
        if not os.path.isdir(cwd):
            self.say_log(log, f"loadgov: working directory {cwd} does not exist")
            return 1, False, False
        timeout = step["timeout_minutes"] * 60 if step["timeout_minutes"] else None
        hit_job = False
        if deadline is not None:
            left = max(deadline - self.clock(), 0.0)
            if timeout is None or left < timeout:
                timeout, hit_job = left, True
        self.proc = subprocess.Popen(
            self.script_argv(step, script),
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        pump = threading.Thread(target=self.pump, args=(self.proc, log), daemon=True)
        pump.start()
        timed_out = False
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self.say_log(log, f"loadgov: step timed out after {timeout:g} s; terminating")
            self.kill_group(self.proc)
        pump.join(10)
        rc = self.proc.returncode
        self.proc = None
        code = 124 if timed_out else (rc if rc >= 0 else 128 + -rc)
        self.collect(step, out_file, env_file, log)
        return code, timed_out, hit_job and timed_out

    def collect(self, step: dict, out_file: str, env_file: str, log: LogWriter) -> None:
        rec = self.job["steps"][step["index"] - 1]
        rec["outputs"] = {}
        try:
            with open(out_file, encoding="utf-8", errors="replace") as f:
                outs = parse_env_file(f.read())
            rec["outputs"] = dict(list(outs.items())[:MAX_OUTPUTS])
        except ValueError as e:
            self.say_log(log, f"loadgov: bad $GITHUB_OUTPUT: {e}")
        try:
            with open(env_file, encoding="utf-8", errors="replace") as f:
                self.env_dyn.update(parse_env_file(f.read()))
        except ValueError as e:
            self.say_log(log, f"loadgov: bad $GITHUB_ENV: {e}")

    def pump(self, proc: subprocess.Popen, log: LogWriter) -> None:
        assert proc.stdout is not None
        while True:
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:
                return
            log.write(chunk)
            with contextlib.suppress(OSError, ValueError, UnicodeDecodeError):
                self.out.write(chunk.decode("utf-8", "replace"))
                self.out.flush()

    @staticmethod
    def kill_group(p: subprocess.Popen) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(p.pid, signal.SIGTERM)
        try:
            p.wait(KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(p.pid, signal.SIGKILL)
            p.wait()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python3 -m loadgov.workflow.jobexec SPEC.json", file=sys.stderr)
        return 2
    with open(argv[0], encoding="utf-8") as f:
        spec = json.load(f)
    return Executor(spec).run()


if __name__ == "__main__":
    sys.exit(main())
