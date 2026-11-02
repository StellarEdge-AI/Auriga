Title: [policy] Auriga: add eval-only RoboDojo agent adapter

## Policy
- Name / paper / upstream repo: Auriga / [Auriga Technical Report](https://github.com/StellarEdge-AI/Auriga/blob/robodojo-leaderboard/docs/Auriga-Technical-Report.pdf) (project page https://auriga.stelledge.com) / https://github.com/StellarEdge-AI/Auriga, branch `robodojo-leaderboard` (the commit is pinned in `deploy.yml`, `auriga_commit`)
- Supported: bench_name=RoboDojo, env_cfg_type=arx_x5, action_type=joint (`eval_batch: false`)
- Training support: eval-only. There is no trained checkpoint: actions come from a language model behind an OpenAI Responses API endpoint driving the Auriga harness, so there is no training code to release. `process_data.sh` and `train.sh` are omitted.

Auriga is an agent harness. A language model (`gpt-6-astra`, reasoning effort `high`) operates the two ARX X5 arms through typed tools: observe the three RGB cameras and the robot state, preview and execute planned single- or dual-arm motions, wait, and measure the scene with calibrated multi-view geometry and a local 3D perception service. Everything runs in the policy server: the agent session, the tools, RoboDojo's own cuRobo planner class (vendored under `policy/Auriga/vendor/robodojo_planner/`, MIT) and the perception service (`services/vggt_height` of the pinned Auriga checkout: VGGT-Omega and SAM 3.1). The same system prompt, tools, configuration and model are used for every task; the only task-dependent input is the task's instruction and documentation text, which is injected into the first prompt.

## Components
- [x] install.sh: conda env `Auriga` from the pinned Auriga checkout, XPolicyLab editable, cuRobo at RoboDojo's pinned commit (`install_planner.sh`), ARX X5 meshes (`download_robot_meshes.sh`), perception environments (`install_perception.sh`) and weights (`download_assets.sh`)
- [x] model.py (+ __init__.py)
- [x] images: model.py decodes nothing and swaps no channels; observations are passed to the harness as delivered. The adapter reads and writes no XPolicyLab trajectory bits
- [x] deploy.yml (standard key set incl. protocol: ws / host / port, policy_name: Auriga; extra keys documented in the README)
- [x] deploy.py: demo_policy loop with three divergences, explained in the README: a WAIT answer is asked again without stepping the simulator (physics stays paused while the model reasons); an empty answer ends the episode through RoboDojo's own final check; `additional_info["auriga"]` carries the native step count and limit and robot proprioception the stock observation lacks (measured gripper jaw position, arm joint velocities). No object, layout, seed or evaluator state is read or sent
- [x] eval.sh + setup_eval_policy_server.sh + setup_eval_env_client.sh (eval.sh and setup_eval_env_client.sh are demo_policy's, unchanged; the server script sources `config.env` and checks the endpoint variables; the policy server starts and stops the perception service itself)
- [x] process_data.sh / train.sh: omitted, eval-only (declared above)
- [x] policy README with install / data / train / eval commands

## Testing
- [x] bash -n + py_compile pass (all `.sh` and `.py` in `policy/Auriga`)
- [x] decode/encode grep: no `cv2.imdecode` / `np.frombuffer` / `Image.open`, no `cv2.imencode`, no channel swaps in `policy/Auriga`
- [x] EVAL_ENV_TYPE=debug closed loop passes, plain and with DEBUG_OBS_ENCODED=1 (10 episodes each, 0 tracebacks)

  ```text
  $ EVAL_ENV_TYPE=debug bash eval.sh RoboDojo stack_bowls no-checkpoint arx_x5 joint 0 0 0 Auriga base
  [TestEnv] Action Step: 19 / 20 (step_limit)
  [TestEnv] Check Episode End: True
  [TestEnv] Episode finished
  [MAIN] eval finished

  $ DEBUG_OBS_ENCODED=1 EVAL_ENV_TYPE=debug bash eval.sh RoboDojo stack_bowls no-checkpoint arx_x5 joint 0 0 0 Auriga base
  [TestEnv] Action Step: 19 / 20 (step_limit)
  [TestEnv] Check Episode End: True
  [TestEnv] Episode finished
  [MAIN] eval finished
  ```

- [x] Simulator eval: the adapter was run end to end with `eval.sh` (`EVAL_ENV_TYPE=sim`, real model endpoint) on an unmodified RoboDojo main checkout (`f57e163`) and XPolicyLab main (`0075cc9`) after a fresh `install.sh`.

  Reference result (our runs, three seeds, 6,300 episodes): 57.7% SR / 64.0 Score.

## Checkpoint (required for leaderboard evaluation)
No policy checkpoint. The only downloaded files are the robot meshes and the perception weights, fetched by `install.sh` and checked against pinned SHA-256 digests (no Hugging Face token needed):

```bash
cd policy/Auriga
bash download_assets.sh
# ARX X5 meshes (24 MB): Hugging Face dataset RoboDojo-Benchmark/RoboDojo at a fixed revision (download_robot_meshes.sh)
# Perception weights (8.1 GB): VGGT-Omega and SAM 3.1 from ModelScope
```

## Limitations / notes
- The model endpoint is provided by the Auriga team: we send the base URL and key privately with the evaluation application. They are read from `AURIGA_API_BASE_URL` and `AURIGA_API_KEY` (or `AURIGA_API_KEY_FILE`); the repository holds no key and `config.env.example` leaves both empty. The endpoint speaks the OpenAI Responses API.
- One environment per policy server (`eval_batch: false`). Wall-clock time per episode is dominated by model latency: typically 25–40 minutes, longer while the endpoint is retrying.
- GPU memory on the policy GPU: about 17.4 GB measured for the cuRobo planner (about 2.7 GB) and the perception service together, up to about 18.3 GB after long runs; a 24 GB GPU is enough. `VGGT_GPU` moves the perception service to another GPU.
- Disk: about 12 GB of perception environments and 8.1 GB of weights under `VGGT_HOME`, besides the conda env.
- Camera calibration comes from frozen constants of the stock `arx_x5` cell and forward kinematics of the measured joints; no depth is used.
- Each episode starts with an empty cross-episode memory; nothing carries over between episodes.
