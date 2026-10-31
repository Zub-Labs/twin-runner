# twin-runner

A GitHub Actions job (`twin-apptest`) that boots 1-4 TV twin containers on a GitHub `ubuntu-24.04-arm` runner and runs
an Android app test harness on a list of APKs. The runner's CPU (Neoverse N2) runs 32-bit ARM code natively.

Only the runner code is here. The twin images, runtime builds, APKs and reports are private and are reached with the
`DATA_TOKEN` secret, so this public log shows only step names, timings and counts.

Inputs: `apks` (file list), `apk_release` (private release holding them), `twins`, `tv_cpus`, `state`
(twin snapshot), `runtime_image` / `runtime_asset` (runtime under test), `runner`, `name`.
Optional harness overlay: files in `ci/harness/*.py` replace the twin's harness files at boot.
