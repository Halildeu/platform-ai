# Issue #40 Provisional Hardware Decision Report

Issue: `#40 [Donanım kararı] RTX 4070 host upgrade vs cloud GPU vs k3d-prod node-pool`

Canonical source: live GitHub issue body read on 2026-06-08.

Status: **BLOCKED for final production decision; provisional development target selected.**

## Issue Decision Matrix

The live issue requires comparison of:

| Option | Description |
|---|---|
| A | Physical RTX 4070 host |
| B | Lambda Labs / Vast.ai hourly cloud GPU bridge |
| C | Dedicated production GPU node pool |

Required decision criteria:

- Turkish WER quality
- inference latency
- cost
- scalability
- KVKK and in-country data processing
- Cross-AI consensus
- explicit user approval for hardware investment

The issue explicitly places the final decision after pilot WER and cost
analysis.

## Available Evidence

An existing separate GPU PC is available with an NVIDIA GeForce RTX 4070 and
8 GB VRAM. It is not a new purchase made by this issue.

Issue #38 validation proved:

```text
model: large-v3
device: cuda
compute type: float16
audio duration: 12.0 seconds
inference latency: 767 ms
language: tr
```

The test used the real `FinalTranscriber` path and an existing locally cached
model. It proved that the current Windows GPU host can load and execute
faster-whisper with CUDA.

This evidence does not prove:

- production concurrency;
- sustained throughput;
- GPU container compatibility;
- complete Turkish WER;
- Workcube vocabulary accuracy;
- 24/7 operational cost;
- production high availability;
- KVKK production deployment compliance.

## Provisional Decision

Use the existing RTX 4070 GPU PC as the **development and technical validation
target** for issue #41 and subsequent GPU PoC work.

This choice is appropriate because:

1. the hardware already exists;
2. CUDA/float16 inference has been demonstrated;
3. it avoids a new hardware purchase before WER evidence;
4. it avoids sending meeting audio to an external cloud during early tests;
5. it provides a concrete target for Docker/CUDA compatibility work.

This is not the final production hardware decision and does not close the
decision gate in issue #40.

## Why the Final Decision Is Blocked

Issues #35 and #36 do not yet contain privacy-safe Workcube pilot WER evidence.
The synthetic/Common Voice smoke result cannot be used to lock production
hardware or model selection.

A defensible production decision also needs:

| Missing evidence | Required output |
|---|---|
| Pilot WER | Workcube vocabulary and meeting-condition accuracy |
| GPU concurrency | Parallel stream count, VRAM peak and queue growth |
| Cost comparison | Existing host electricity/operations vs cloud hourly cost |
| Availability model | Single-host failure and recovery expectations |
| KVKK review | Data location, processor boundary and retention controls |
| Production topology | Dedicated host or approved orchestrated GPU runtime |

## Allowed Next Step

Issue #41 may proceed against the existing RTX 4070 as a GPU Docker PoC:

```text
services/live-stt-service/Dockerfile.gpu
CUDA 12.2 runtime
cuDNN
faster-whisper float16 / int8_float16
optional NVENC investigation
image size baseline
```

The resulting image and measurements are evidence for #40, not proof that the
RTX 4070 is the permanent production platform.

## Prohibited Claims

Until the missing gates are complete, do not claim:

- issue #40 is Done;
- RTX 4070 is the final production hardware;
- cloud GPU is rejected;
- a production GPU node pool is unnecessary;
- `large-v3` or `large-v3-turbo` is the final production model;
- the single GPU PC satisfies production availability requirements.

## Risks

| Risk | Impact | Control |
|---|---|---|
| Provisional target mistaken for final decision | Premature hardware lock-in | Keep #40 open and label report provisional |
| 8 GB VRAM limits concurrency | Queue growth under multiple meetings | Measure in #42/#43 |
| Windows test differs from Linux container | CUDA/cuDNN mismatch | Validate Docker image in #41 |
| Single physical PC fails | No production high availability | Define production topology after measurements |
| Pilot WER remains unknown | Fast but inaccurate Turkish transcript | Return to #35/#36 |
| External cloud may violate data boundary | KVKK risk | No cloud audio test without approved boundary |

## Final Closure Criteria

Issue #40 can be completed only after:

1. pilot WER evidence exists;
2. #41 GPU container compatibility is measured;
3. #42 concurrency/VRAM behavior is measured;
4. #43 latency/memory/cost matrix is available;
5. KVKK deployment boundary is reviewed;
6. the user/operator approves the investment and topology;
7. required Cross-AI review is recorded.

## Next Action

Proceed to issue #41 using the existing RTX 4070 as the provisional GPU PoC
target. Keep issue #40 open until the final closure criteria are met.

AG-019 staging resource gate pending; GPU work is development validation only.
