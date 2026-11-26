"""Workflow runner: GitHub- and Forgejo-Actions-compatible workflow YAML run through loadgov's lanes.

yaml_subset  a restricted YAML parser (stdlib only) that refuses everything it does not support
expr         the small safe expression subset of `if:` and `${{ }}`
model        load, validate and expand a workflow file (jobs, needs, matrix, lane mapping)
records      run records: runs -> jobs -> steps under the client's state directory
jobexec      the job script `loadctl run` starts: runs the steps of one job and records each
runner       the orchestrator: needs ordering, parallel jobs, one `loadctl run` per job
cli          `loadctl workflow ...` and `loadctl runs ...`

The runner is a client like loadctl: the daemon never executes, signals or writes cgroups for it.
See docs/workflows.md.
"""
