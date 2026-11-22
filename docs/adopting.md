# Adopting loadgov in a project

How a project hooks into the two lanes that need its cooperation: the **light lane** for small checks and the
**image train** for beta and release container image builds. The behaviour behind both is in
[protocol.md](protocol.md) (The light lane, The image train); this page is the project's side of it. The
examples use `web-app` as the project name; use your project's own name everywhere, and keep site-specific
values (addresses, paths, lock names) in the configuration, never in scripts you share.

## 1. The light lane

A light job is a short, read-only check that must never wait behind a build. `loadctl run --kind light` waits
for headroom, then runs the command in its own systemd scope with hard limits.

```bash
loadctl run --kind light --project web-app --label lint -- shellcheck scripts/*.sh
```

**Qualifies:** lint, `shellcheck`, `bash -n`, formatters in check mode, link checks, config validation, small
unit tests, read-only verifications. **Never qualifies:** compiles, image builds, full or integration suites,
browser or E2E runs, anything that starts containers, and agent sessions. Those are `gate` or `compile` jobs
(`loadctl run --kind gate|compile`); when in doubt, use `gate`.

**Use `run`, not a bare wait.** `loadctl wait --kind light` (or the MCP `load_wait_headroom`) only waits; the
command that follows is unlimited and not counted against the caps. Only `loadctl run --kind light` applies the
limits and registers the job, so only it shows in `loadctl status` and in the job statistics.

**Limits** (defaults; the site may tighten them in `[light]`): 1 core (`cpu_quota_pct = 100`), 1 GB of memory
(`memory_max_mb = 1024`, no swap) and 10 minutes (`max_runtime_s = 600`). At most `max_light` light jobs run on
the host (0 to 6 by profile; the drain profile is 0) and `max_light_per_project` (default 2) per project;
further jobs wait.

**Exit codes of `loadctl run`:** the job's own code; `1` the headroom wait timed out; `3` the daemon is not
reachable (the job did not start); `76` the job overran: it ran longer than `max_runtime_s` or hit the memory
limit. On 76 stderr says `LIGHT_OVERRUN <label>: runtime 600 s exceeded` (or `memory limit 1024 MB hit`). Treat
76 as a failed check, not a flake: either the check is not light (move it to `gate`) or it needs splitting. Three
overruns of a project within 24 hours raise the alert `light_overrun:<project>`.

**Splitting a slow check suite.** A suite that does not fit in 10 minutes or 1 GB is not one light job. Split it
by what each part needs:

1. Run the quick static parts (lint, format check, config validation) as separate light jobs, one `--label` each.
   They run side by side up to the per-project cap and each stays far below the limits.
2. Split a unit-test suite by directory or marker into slices that each finish in a few minutes
   (`pytest tests/unit/a`, `pytest tests/unit/b`), one light job per slice.
3. Keep what remains (integration, containers, browsers, anything over the limits) as one `gate` job:
   `loadctl run --kind gate --project web-app --label suite -- make test`.

Do not ask for higher limits to make a check fit: the ranges cap how loose a site may make them.

## 2. The image train

Beta and release image builds are heavy and not urgent, so they run in a night window (default `00:00-06:00` in
the site's time zone) instead of when someone asks. A project **submits** the build from its release script and
returns at once; the daemon only decides which job may start, and `loadctl image runner` (started by a systemd
timer every 10 minutes) runs it.

```bash
loadctl image submit --project web-app --label web-app-1.4.0 --channel beta --key PROJ-42 \
    --cwd /srv/web-app -- ./scripts/build-image.sh
```

- `--channel beta|release`. A **release** has only the window. A **beta** also has the interval: a project's
  beta starts at most once per `beta_interval_h` (default 48 h), measured from the start of its last beta. A beta
  submitted inside the interval is refused (exit 4) and the message names `next_allowed`, the first window moment
  after the interval; a second queued beta of the same project is refused too. Submit it again after
  `next_allowed`.
- `--key` (or `$LOADGOV_KEY`) is the issue key shown in the queue.
- Submitting prints the job id, its position and the planned start (`loadctl image list` also shows the
  finish, which stays unknown until the project has finished image jobs to take a median from).
- Jobs run one at a time, in submission order. At most `max_per_night` jobs (default 6) start per window.

**What happens after 06:00.** No new job starts after the window ends. A job that is already running runs to its
end; past `max_runtime_min` (default 90) it raises the warning `image_overrun:<project>`, but it is never
killed. A job that was released but did not get the heavy slot before the end goes back to the queue and counts
neither for the beta interval nor for the night. Everything still queued waits for the next window and shows as
`deferred` with its new planned start.

**See the queue.**

```bash
loadctl image list            # window, and each job's state and planned start and finish
loadctl image list --json
loadctl status                # one line: "image train: 3 queued, next 00:40 (web-app beta)"
```

`loadctl status` also prints a line for an empty train while its window is open or opens within 12 hours, for
example `image train: empty, window 00:00-06:00 UTC, opens in 7 h`. A running image job appears in the job list
with its expected finish (`eta`) once the project has finished image jobs.

**Cancel** a queued job with `loadctl image cancel ID --project web-app`. A running job cannot be cancelled.

**Now is owner-only.** Running a job outside the window or inside the beta interval is the human owner's
decision: `loadctl image now ID --owner --reason "..."`. An agent, or any process inside a governed cgroup, is
refused (`owner_denied`, exit 4). A project script never forces a build; it submits and lets the job wait.

**The environment and the directory of a build.** The runner starts the job in the `--cwd` you gave (default: the
directory `submit` ran in, stored as an absolute path; a missing directory fails the job) with **only** the
variables of `[image] env_allow`, taken from the submitting shell at submit time. The default allowlist is
`PATH`, `HOME`, `LANG`, `DOCKER_HOST`, `DOCKER_CONFIG` and `LOADGOV_KEY`; the status shows the site's list as
`image.env_allow`. Everything else is dropped on purpose: the build runs hours later, from a timer, and must not
depend on what the caller happened to export. So the build script has to

- set what it needs itself (`export VERSION=...`, registry and tag names) or read it from files in its cwd, not
  from the caller's shell;
- load its own credentials from the project's own secret store or file when it runs. Secrets are neither sent nor
  stored in the queue; the audit log records only the program name, the cwd and the variable names;
- use absolute paths or paths relative to its `--cwd`, and assume the minimal `PATH`.

If a build needs one more variable, ask the site owner to add it to `env_allow`. Do not pass it in the command
arguments: they are stored in the queue state.

A container build started by hand outside the window (`docker build`, `docker buildx build|bake`,
`docker compose build`, `buildctl build`) raises the warning `image_out_of_window`; builds the runner started are
exempt.

## 3. Adoption checks

Run these once per project after wiring the scripts. Each is harmless and finishes in seconds.

**Light lane.**

```bash
loadctl run --kind light --project web-app --label probe -- true; echo "exit $?"
```

Expect `exit 0` and a `HEADROOM_OK probe light lane: ...` line. While a longer probe runs (`-- sleep 30`),
`loadctl status` shows `light 1/4 (per project max 2)` and a `job   web-app light probe` line. An overrun probe
(`-- sleep 1000`, exit 76 after 10 minutes with the default limit) is only worth running when you changed the
limits.

**Image train.** The probe job only queues, outside the window as well as inside it, and is cancelled at once:

```bash
loadctl image submit --project web-app --label probe --channel release -- true
loadctl image list                                   # the probe is queued with a planned start
loadctl image cancel 17 --project web-app            # use the id that submit printed
```

Expect submit to print the id, the list to show the probe as `queued` (or `deferred`) and the cancel to succeed.
Use the `release` channel for the probe: a `beta` probe would use up the project's beta interval check.

**Checklist per project**

- [ ] Every small check calls `loadctl run --kind light --project <name> --label <check>`, never a bare wait.
- [ ] No light command compiles, builds an image, starts a container or runs a full suite.
- [ ] Slow suites are split (section 1) or run as `gate`.
- [ ] The light probe exits 0 and appears in `loadctl status`.
- [ ] The release script submits image builds with `loadctl image submit` and does not build them in place.
- [ ] The build script sets its own environment, loads its own credentials and works with only the allowlisted
      variables.
- [ ] The submit and cancel probes work, and the script handles exit 4 (a beta inside the interval) by reporting
      `next_allowed` instead of retrying in a loop.
