# Hybrid candidate recovery experiments

The hybrid policy is named `semantic` in run artifacts. See the [per-arm results](candidate-results.json) for first-query visibility, actual rewind targets, and official outcomes. The 20/40 matrix includes Bat, Ctags, and Age. The 80-cap follow-up includes Bat and Age. Ctags uses the archived model defaults. The preset target sets are Bat `rec_000055` / `rec_000041` for 20/40, Bat `rec_000041` for 80, Ctags `rec_000042` / `rec_000040`, and Age `rec_000031`.

## Run a paired comparison

Follow the [setup guide](setup.md) for MettleBench, the runtime, Docker, and the model environment. Use a fresh layout for the semantic runtime. A local failed endpoint archive needs its manifest, traces, snapshot store, and workspace and scratch archives. Checkpoint IDs refer to that archive. The result table ships with this repository. The large endpoint archives are local inputs to the runner.

```bash
export JWS_METTLE_ROOT="$PWD/local/mettle-hybrid"
# JWS_RUNTIME_SOURCE and JWS_MODEL_ENV_FILE are set during setup.
python tools/run_mettle_paper_recovery.py --install --semantic-candidates

python tools/run_mettle_candidate_windows.py \
  --source /path/to/archived/age/seed \
  --task programbench_age_crypto_fixture_handoff \
  --output "$JWS_METTLE_ROOT/results/age-comparison" \
  --limit 20 --limit 40 --target rec_000031
```

Each cap runs the recent and hybrid policies. The launcher checks the paired environment audit, then writes one JSON per arm with the verifier outcome and retrieval metrics. Use `--archived-model-defaults` for an endpoint that requires the archived model request settings. Use checkpoint IDs from your own archive when running a new endpoint.
