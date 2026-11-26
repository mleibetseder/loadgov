# Workflows: GitHub- and Forgejo-Actions-compatible checks and pipelines

A project defines its checks and pipelines once, in workflow YAML that GitHub Actions and Forgejo Actions
understand (`.github/workflows/*.yml|yaml`, `.forgejo/workflows/*.yml|yaml`), and `loadctl workflow run` runs
them on this host **through loadgov's lanes and slots**: every job is one governed `loadctl run`, so the heavy
and compile slots, the headroom checks, the light limits and the image train apply as for any other job. Only
a documented subset is supported; everything outside it is refused when the file is loaded, with
`file:line:col` and the reason. Nothing is skipped silently. What differs on this host is named as
**host-only** below.

The runner is a client, like `loadctl` itself: the daemon never executes a step, sends a signal for it or
takes a lock for it.

The examples use `web-app` as the project name; use your project's own. Site values (paths, lock names,
addresses) never go into a workflow file: they are configuration (README, "Configure for your site").

## 1. Run a project's checks locally

```bash
loadctl workflow list                            # the workflows of the current project and whether they load
loadctl workflow validate .github/workflows/ci.yml
loadctl workflow run .github/workflows/ci.yml --dry-run          # the plan and the exact loadctl commands
loadctl workflow run .github/workflows/ci.yml --event push --key PROJ-42
loadctl workflow run .github/workflows/ci.yml --job lint         # one job, without its needs
loadctl runs                                     # recent runs
loadctl runs show latest --job lint --log        # what happened, with the step logs
```

The project defaults to `$LOADGOV_PROJECT`, else the name of the workspace directory (the folder that holds
`.github/` or `.forgejo/`; `--workspace DIR` overrides it). `--key` (or `$LOADGOV_KEY`) is the issue key shown
in `loadctl status`. A run without `--event` is `workflow_dispatch`: the local runner runs on demand, so
`schedule` is recorded and never triggered. An explicit `--event` that the workflow does not list in `on:` is
refused.

Working examples that the tests load are in [examples/workflows/](examples/workflows/): a light check
(`light-check.yml`), a pipeline over three lanes with `needs` and a matrix (`gate-with-needs.yml`), and a
workflow that is refused because of a `uses:` (`refused-uses.yml`).

## 2. What is supported

| Area | Supported | Refused at load | Host-only (runs, with a note) |
|---|---|---|---|
| File | one YAML document in the subset of section 3; `.github/workflows` and `.forgejo/workflows`, `.yml` and `.yaml`, up to 1 MB | anchors, aliases, tags, merge keys, complex keys, several documents, tab indentation; unknown keys at the top level, in jobs and in steps | |
| `name`, `on` | `name`; `on` as string, list or mapping (`push`, `pull_request`, `workflow_dispatch`, `schedule`) | | `schedule` and every other event are recorded, never triggered; branch and path filters are ignored; the local runner runs on demand |
| `env` | workflow, job and step `env`; `${{ }}` in values (section 4) | non-scalar values; bad variable names | |
| `defaults.run` | `shell` (`bash`, `sh`, `python`), `working-directory`, at workflow and job level | other `defaults` keys; other shells (`pwsh`, `cmd`, custom templates) | `python` runs `python3` |
| `permissions`, `concurrency`, `run-name`, `environment` | accepted | | ignored with a note: there is no token, the lanes serialise work, no deployment environments |
| `jobs.<id>` | `name`, `needs` (string or list; cycles and unknown jobs refused), `runs-on` (section 5), `env`, `if`, `timeout-minutes`, `continue-on-error`, `strategy`, `steps`, `defaults` | `container`, `services`, `uses` (reusable workflows), `secrets`, `outputs`, anything else | |
| `strategy` | `matrix` of `key: [scalar, ...]` with `include` and `exclude` (Actions' rules; at most 256 jobs per matrix and 1024 job instances per workflow; the product of the key lists is checked before anything is expanded and may not exceed 4096; at most 256 include and exclude entries), `max-parallel` | a matrix from an expression (`fromJSON`), nested values | `fail-fast` is accepted and ignored: every combination runs to its end |
| `steps[]` | `name`, `id`, `run`, `shell`, `working-directory`, `env`, `if`, `continue-on-error`, `timeout-minutes`, `uses`, `with` | anything else; both or neither of `run` and `uses` | |
| `uses` | `actions/checkout@<ref>`, `actions/setup-python@<ref>`, `actions/setup-node@<ref>` | every other action, local actions (`./x`), `docker://` | `checkout` is a no-op on the local checkout; `setup-python` and `setup-node` only assert that `python3` / `node` exist on this host (nothing is downloaded) and log the installed version; the other `with:` inputs are ignored |
| `if` | `success()`, `failure()`, `always()`, `cancelled()`, `!`, `&&`, `\|\|`, `==`, `!=`, parentheses, string / number / `true` / `false` / `null` literals, the contexts of section 4 | every other function (`contains`, `format`, `hashFiles`, `fromJSON` ...), `<`, `>`, `[]` indexing, other contexts (`secrets`, `vars`, `inputs`, `needs`, `job`, `github.event.*` ...) | |
| Secrets, artifacts, caches | | `secrets.*`; `actions/upload-artifact`, `actions/cache` and every other action (`uses` is the allow list) | |
| Runner OS | Linux | `macos-*`, `windows-*` labels | |

Behaviour that matches Actions: a job or step without `if` runs only while everything before it succeeded
(`success()`); an `if` without a status function means `success() && <if>`; `always()` and `failure()` run
after a failure; `continue-on-error` on a step keeps the job green, on a job keeps the run and its
dependents green (the record still says `failure`); `==` compares strings case-insensitively; a failed `needs`
job skips its dependents unless they say `always()` or `failure()`.

### The `on:` key and other YAML typing

`on:` is the key "on" (YAML 1.1 would read it as `true`; Actions does not). Mapping keys are always strings.
As values only `true`/`false` (any of the usual capitalisations) are booleans, so `on`, `yes` and `no` stay
strings; `3.10` is the number 3.1, exactly as in Actions: quote versions (`'3.10'`).

## 3. The YAML subset

The parser (`src/loadgov/workflow/yaml_subset.py`) is part of the standard-library-only runtime. It reads:
block mappings and sequences by indentation (a sequence may sit at the indentation of its key); plain,
`'single'` and `"double"` quoted scalars (the usual escapes in double quotes, multi-line quoted and plain
scalars folded as in YAML); `|` and `>` block scalars with chomping (`-`, `+`) and indentation indicators;
comments; `true`, `false`, `null`, `~`, integers (decimal, `0x`, `0o`) and floats; flow sequences `[a, b]`
and flow mappings `{a: b}` that fit on one line; a leading `---`.

It refuses, with `file:line:col: <what is not supported>`, and never guesses: anchors and aliases (`&`, `*`),
tags (`!`), merge keys (`<<`), complex keys (`?`), several documents (`---` / `...` again), directives,
tabs as indentation, flow collections over several lines, duplicate keys, and a `: ` inside an unquoted
scalar (`run: echo a: b` is an error in YAML too: quote it).

## 4. Expressions

`${{ ... }}` is supported in `run`, `env` values, `name`, `working-directory`, `with` values and `runs-on`
(matrix only); `if:` takes the same expressions, with or without `${{ }}`. The grammar is the one in section 2;
the contexts are:

| Context | Values |
|---|---|
| `github.event_name`, `github.ref`, `github.ref_name`, `github.sha`, `github.workspace` | the `--event`, the checkout's `HEAD` ref and commit (empty outside a git work tree), the workspace |
| `runner.os` | `Linux` |
| `env.NAME` | the variables the workflow, job, `$GITHUB_ENV` and the step defined so far (not the host environment) |
| `matrix.KEY` | the combination of the job instance (must be a key of the job's matrix) |
| `steps.ID.outputs.NAME`, `steps.ID.outcome`, `steps.ID.conclusion` | an earlier step of the same job (must exist) |

**`${{ }}` is substituted as text, exactly like hosted Actions.** The value is pasted into the script
before the shell reads it, so a branch name, a ref, a matrix value or a step output that contains `"`, `$(`,
a backtick or `;` becomes shell syntax. Pass such values through the environment instead: write
`$GITHUB_REF_NAME` or `$GITHUB_REF` (the runner sets them), or put the expression in a step `env:`
(`env: {MSG: "${{ steps.pick.outputs.msg }}"}`) and use `"$MSG"` in the script, as `examples/workflows/light-check.yml`
does. Nesting is limited to 64 levels (YAML and expressions), at most 256 expression tokens; deeper input is
refused at load.

Status functions are valid in `if:` only. An unsupported expression is refused when the file is loaded, not
when the step runs.

## 5. Lane mapping

A job's lane comes from its `runs-on` labels. One rule:

| `runs-on` | Lane | Notes |
|---|---|---|
| `loadgov-light` | `light` | 1 core, 1 GB, 10 minutes by default; exit 76 = overrun, a failed job |
| `loadgov-gate` | `gate` | takes the heavy slot |
| `loadgov-compile` | `compile` | takes the compile slot |
| `loadgov-image-beta`, `loadgov-image-release`, `loadgov-image` | `image` | see below |
| `ubuntu-latest`, `ubuntu-<version>`, `docker`, `self-hosted`, `linux`, `x64`, `arm64` alone | `gate` | host-only: no `loadgov-*` label, so the safe default |
| `macos-*`, `windows-*`, any other label | refused | no local equivalent |

Extra generic labels may accompany a lane label (`[self-hosted, linux, loadgov-light]`). Two different lane
labels are refused. `runs-on: ${{ matrix.lane }}` works: each matrix instance is mapped on its own. The
lanes `impl` and `review` are agent lanes and are not available to workflows.

Each job instance runs as **one** governed job:

```bash
loadctl run --kind <lane> --project <P> --label <workflow>-<job>[-<matrix>] [--key <KEY>] -- \
    python3 -m loadgov.workflow.jobexec <run dir>/specs/<job key>.json
```

The job script runs the steps in order inside the scope that `loadctl run` created and records every step.
The lane, slot, headroom wait, light limits, job statistics and `loadctl status` therefore see a workflow job
exactly like a hand-started one. Independent jobs are started in parallel (at most `--max-parallel`, default
8); the lanes decide who actually runs, the others wait in `loadctl run`. `strategy.max-parallel` limits one
matrix job. Jobs that need a failed or skipped job are skipped without ever reaching `loadctl`.

**The image lane.** A job in the image lane is not run directly. It is queued with
`loadctl image submit --project P --label L --channel beta|release --cwd <workspace> -- <job script>`
and runs later in the night window by `loadctl image runner`, like any image build; the channel comes from the
label (`loadgov-image-beta`) or `--channel` (a bare `loadgov-image` without `--channel` is refused). The run
then ends with that job in status `queued`; jobs that `needs` it are skipped ("queued in the image train"),
because their input does not exist yet. The job script updates the same job record when the runner starts it,
so `loadctl runs show` shows the result afterwards. A refused submit (beta interval, window) is a failed job
with loadctl's exit code. Running an image job directly (`loadctl image now`) stays an owner decision.

## 6. A step at run time

- The shell is `bash -e {0}` when `shell` is not set, `bash --noprofile --norc -eo pipefail {0}` for
  `shell: bash`, `sh -e {0}`, or `python3 {0}`. The step runs in its own process group, from the workspace (or
  `working-directory`, relative to it). Each step has its own `$GITHUB_OUTPUT` and `$GITHUB_ENV` file
  (`name=value` and `name<<DELIM` blocks); outputs are read by `steps.<id>.outputs.<name>` and
  `$GITHUB_ENV` entries are in the environment of the later steps.
- Environment: the job's own, plus `CI=true`, `RUNNER_OS=Linux`, `GITHUB_WORKSPACE`, `GITHUB_SHA`,
  `GITHUB_REF`, `GITHUB_REF_NAME`, `GITHUB_EVENT_NAME`, `GITHUB_JOB`, `GITHUB_WORKFLOW`, `GITHUB_OUTPUT`,
  `GITHUB_ENV`, `LOADGOV_RUN_ID` and `LOADGOV_JOB_KEY`. Then workflow `env`, job `env`, `$GITHUB_ENV`, step
  `env`, in that order. (`GITHUB_ACTIONS` is deliberately not set: this is not GitHub.)
- `timeout-minutes` on a step ends its process group (SIGTERM, then SIGKILL after 5 s; step exit code 124,
  `timed_out: true`). On a job it bounds the whole job: the running step is ended and the rest is `cancelled`
  (job reason `job timeout-minutes exceeded`). The light lane's own runtime limit applies on top.
- When `loadctl` terminates the job script (light overrun, `loadctl run` interrupted), the running step's
  process group is ended, the steps left are `cancelled` and the job is `cancelled` (exit 143), or `failure`
  with the overrun reason for exit 76. The runner never signals anything else.
- Interrupting `loadctl workflow run` (Ctrl-C) ends the pending jobs as `cancelled`; running jobs get the
  interrupt through `loadctl run`. The exit code is 130.

## 7. Run records

A run is stored by the client, not by the daemon, as plain files:

| | Where |
|---|---|
| Directory | `$LOADGOV_RUNS_DIR`, else `/var/lib/loadgov/runs` when running as root, else `$XDG_STATE_HOME/loadgov/runs` (`~/.local/state/loadgov/runs`); `--runs-dir` overrides it per command |
| Layout | `<run id>/run.json`, `<run id>/jobs/<job key>.json`, `<run id>/logs/<job key>-<step>.log`, `<run id>/specs/<job key>.json` (the job script's input) |
| Run id | `YYYYMMDDTHHMMSSZ-xxxxxx` (UTC start, 6 hex characters); sorts by time; `latest` and any unique prefix work in `loadctl runs show` |
| Retention | the newest 200 finished runs; older run directories are removed when a run starts (only directories named like a run id that hold a `run.json`). A run that still has a job that is not final (a queued image job, running, waiting) is never removed and does not count toward the 200, until it is 30 days old |
| Logs | one per step, at most 5 MB; past that the rest is dropped and the log ends with `[loadgov: log truncated at 5242880 bytes]` (`log_truncated: true`) |
| Permissions | directories `0700`: logs may contain what the steps print |

`loadctl runs [--json] [--limit N]` lists the newest runs; `loadctl runs show RUN [--job J] [--step S] [--log]
[--json]` shows one (`--job` takes a job key or id, `--step` a number, id or name). `--json` prints the
records below, filtered by `--job` and `--step` but with the same shape. Statuses: `queued`, `waiting for
slot`, `running`, `success`, `failure`, `cancelled`, `skipped`.

**JSON format** (`schema: 1`; fields are only ever added, so the parity comparison (ILM-26) and the UI
(ILM-28) can rely on it). Times are UTC ISO-8601 (`2026-11-23T08:15:20Z`), durations are seconds.

```json
{
  "schema": 1,
  "id": "20261123T081520Z-b216aa",
  "workflow": {"name": "Checks", "file": ".github/workflows/ci.yml", "stem": "ci"},
  "event": "push", "project": "web-app", "key": "PROJ-42", "host_id": "build-host",
  "sha": "0123abc...", "ref": "refs/heads/main", "workspace": "/srv/web-app",
  "created_at": "...", "finished_at": "...", "notes": ["host-only: ..."],
  "status": "success",
  "jobs": [{
    "key": "lint", "id": "lint", "name": "Lint", "order": 0, "matrix": {}, "lane": "light", "channel": null,
    "runs_on": ["self-hosted", "loadgov-light"], "label": "ci-lint", "needs": [],
    "status": "success", "reason": null, "exit_code": 0, "loadctl_exit": 0,
    "started_at": "...", "finished_at": "...", "duration_s": 3.2,
    "host_id": "build-host", "continue_on_error": false, "notes": [],
    "steps": [{
      "index": 1, "id": "s", "name": "Install", "kind": "run", "status": "success", "exit_code": 0,
      "started_at": "...", "finished_at": "...", "duration_s": 1.1, "timed_out": false,
      "continue_on_error": false, "log": "logs/lint-1.log", "log_truncated": false,
      "outputs": {"name": "value"}, "note": null
    }]
  }]
}
```

`status` of a run is derived from its jobs: `running`, `waiting for slot` or `queued` while any job is;
then `failure` if a job failed (a failed job with `continue_on_error` does not count), `cancelled`,
`skipped` when every job was skipped, else `success`. `kind` is `run` or `uses`. `reason` is set for a
skipped job (why), a job that never started (what `loadctl run` reported: `light lane overrun`, `the loadgov
daemon is not reachable`, `no room or slot busy`, ...), and a timeout. `loadctl_exit` is the exit code of
`loadctl run` (or `loadctl image submit`) and `exit_code` the job's own.

## 8. Commands and exit codes

| Command | Does |
|---|---|
| `loadctl workflow list [--path DIR] [--json]` | the workflows of a project, valid or `INVALID` with the reason |
| `loadctl workflow validate FILE [--json]` | loads the file; exit 0, or 1 with `refused: file:line:col: ...` on stderr |
| `loadctl workflow run FILE [--job ID] [--event push\|workflow_dispatch\|pull_request] [--project P] [--key K] [--workspace DIR] [--channel beta\|release] [--max-parallel N] [--dry-run] [--runs-dir DIR] [--json]` | runs the jobs; `--dry-run` prints the plan and the `loadctl` commands and records nothing; `--json` prints the final run record on stdout (progress goes to stderr) |
| `loadctl workflow parity RUN --hosted JOBS.json [--outputs FILE] [--runs-dir DIR] [--json]` | compares a local run with a hosted run's jobs (section 10); exit 0 identical, 1 diverged, 2 bad input |
| `loadctl runs [--json] [--limit N]`, `loadctl runs show RUN [--job J] [--step S] [--log] [--json]` | the records |

`workflow run` exits 0 when every job succeeded or was skipped (a run with only queued image jobs is 0), 1
when a job failed or was cancelled, 2 for a refused workflow or bad option (nothing ran), 130 when
interrupted. `--job ID` runs that job alone: its `needs` are not run and count as satisfied.

The host id in a record is `$LOADGOV_HOST_ID`, else the daemon's `host.id`, else the id derived from the
hostname (the daemon's own default rule). For tests and wrappers `LOADGOV_LOADCTL` replaces the `loadctl`
command the runner calls (default: `python3 -m loadgov.cli.loadctl --socket ...`).

## 9. Parity with GitHub and Forgejo

The same file runs in Actions and here; the results can be compared because the record stores, per step,
the status, exit code, outputs and log under stable names. What can legitimately differ: the host-only
rows of section 2 (named in `notes`), the environment (`GITHUB_ACTIONS` is not set), timing (jobs may wait
for a slot), and tool versions (`setup-*` does not install). Section 10 compares
these records with the results of a hosted run.

## 10. The parity harness

`loadctl workflow parity` answers one question: did the same workflow do the same thing on a hosted runner
(GitHub Actions; Forgejo Actions answers the jobs call in the same shape) and through `loadctl workflow run`?
It reads **files only**: it never calls a network, so a hosted run is recorded first and compared later.

```bash
loadctl workflow run .github/workflows/parity-check.yml --event workflow_dispatch --json > /dev/null   # the local run
gh api repos/OWNER/REPO/actions/runs/RUN_ID/jobs > hosted-jobs.json                     # the hosted run, recorded
loadctl workflow parity latest --hosted hosted-jobs.json --outputs outputs.json
```

`RUN` is a local run id, a unique prefix, `latest` (looked up in the runs directory, `--runs-dir`), or the path
of a run record file (the JSON of `loadctl runs show RUN --json`). `--hosted` is the JSON of
`GET /repos/{owner}/{repo}/actions/runs/{run_id}/jobs`: `jobs[].name`, `status`, `conclusion`, `started_at`,
`completed_at`, `labels`, `runner_name` and `steps[].name`, `number`, `status`, `conclusion`. A step may also
carry an `exit_code` (not part of the API; add it to a recorded file when you know it) and it is then compared.
Jobs are matched by name (a local job without `name` is named by its id), exactly first, then ignoring case.
Steps are matched by name: a hosted step `Run X` matches the local `X`. An unnamed `run:` step is labelled
locally by its first line, cut at 60 characters, while a hosted runner shows the whole script: long or
multi-line unnamed scripts therefore show up as `step_missing_hosted` plus `step_missing_local`; name such
steps. A local job that failed with `continue-on-error` is compared as `success`; the hosted jobs API may still
report such a job as `failure`, which has not been checked against a real recorded run yet. `--json` prints the report as JSON (`schema: 1`: `result`, `compared`, `divergences`, `host_only`).

Exit codes: 0 no divergence, 1 at least one divergence, 2 bad input or usage (an unreadable, malformed or
oversized file, an unknown run). Inputs are untrusted: a file over 4 MB, over 1024 jobs or over 1024 steps per
job is refused, every field is type-checked, and the error names the field.

**Outputs.** Only outputs the workflow declares deterministic are compared; the declaration is the outputs
file, because the workflow file stays inside the supported subset:

```json
{"schema": 1,
 "deterministic": [{"job": "Check", "step": "Compute", "output": "sum"}],
 "hosted": {"Check": {"Compute": {"sum": "5"}}}}
```

`step` is the step's name or id. The local value comes from the run record, the hosted value from `hosted`.
An output that varies between runs (a timestamp, a sha) does not belong in `deterministic`.

**Divergences** (real, counted, exit 1):

| Kind | Meaning |
|---|---|
| `job_status`, `step_status` | the conclusions differ (`timed_out` counts as `failure`; a failure tolerated by `continue-on-error` is `success` on both sides) |
| `exit_code` | the hosted step records an exit code and it differs |
| `job_missing_hosted`, `job_missing_local`, `step_missing_hosted`, `step_missing_local` | a job or step ran on one side only |
| `step_order` | the common steps ran in a different order |
| `job_order` | a job started on the hosted side before a job it `needs` had ended |
| `output_value`, `output_missing`, `output_unresolved` | a declared deterministic output differs, is absent on a side, or names a job or step the local run lacks |
| `state` | a job or step was not final (local `queued`, `running`; hosted not `completed`): nothing to compare yet |

**Host-only differences** (listed in the report, never counted):

| Kind | Why it differs |
|---|---|
| `runner_labels`, `runner` | the local lane comes from `runs-on` (`loadgov-*`); a hosted runner has its own labels and name |
| `timing` | job durations (a local job may also wait for a slot); start and end times are never compared, except the `needs` order |
| `hosted_step` | `Set up job`, `Complete job`, `Post Run ...`, `Pre Run ...` (only these two prefixes: a hosted step that is merely named `Post comment` is a real step) and container steps that the hosted runner adds around the workflow's own steps |
| `environment` | the `GITHUB_*` and `RUNNER_*` variables of a hosted run (`GITHUB_ACTIONS`, run id and number, actor, repository ...) are not set locally; the local runner sets `LOADGOV_RUN_ID` and `LOADGOV_JOB_KEY` |
| `local_note` | the `host-only:` notes of the local run (`checkout` is a no-op, `setup-*` installs nothing, ignored keys) |
| `job_start_order` | jobs without `needs` start in parallel on both sides, so their start order is not compared |

The worked example is [examples/workflows/parity-check.yml](examples/workflows/parity-check.yml) (`parity-check.yml`:
two light jobs, one declared deterministic output, one tolerated failure). Its recorded local run, a matching
hosted jobs file and a deliberately divergent one (a step failed hosted, a different output) are under
`tests/unit/parity/`; `tests/unit/test_workflow_parity.py` runs the harness on them.
