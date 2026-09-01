**English** | [한국어](USAGE.ko.md)

# Run Guide (USAGE)

This document covers the **detailed run steps and caveats** for the `canvas-pipeline` code.
For background and strategy, see the [main README](../README.md).

---

## Workflow at a glance

```
[Business / Canvas]              [ML team / this code]
Preprocess(flow) + Train    →    ① promote_model.py    : approve + deploy (once per version)
Model Registry (Pending)         ② inference_pipeline.py : preprocess + batch inference (per new data)
```

Why **separate** promotion/deploy from inference: they have different triggers.
Approve/deploy happens once per new model version; inference repeats per new data.

---

## Files

| File | Role | Frequency |
|---|---|---|
| `config.py` | Shared config (role·bucket·container·model name). Swappable via env vars | - |
| `promote_model.py` | **[Flow 1]** model approve + deploy (create Model object) | Once per version |
| `inference_pipeline.py` | **[Flow 2]** preprocess + batch inference pipeline | Per new data |

---

## One-time setup — flow · bucket configuration (ML team)

When first setting up the pipeline, do the following **once** before running.
(After that, new data just re-runs the pipeline without this setup.)

### 1) Export the preprocessing flow from Canvas to S3
- After the business user builds the preprocessing in Data Wrangler, choose **Export to Amazon S3** on the Destination (S3) node.
- This executes the `.flow` **as-is** (no code "translation"), so it matches the business screen 100%.

**⚠️ Export job settings — important**
- **Turn OFF `Auto job configuration`.**
  If left on, Canvas decides job settings automatically and may run via EMR Serverless, which
  would not produce the **SageMaker Processing Job** form we want to reuse.
- Once disabled, an execution-engine choice appears — **select `SageMaker Processing`**
  (not `EMR Serverless`), so it reproduces the same way as this repo's `Processor`
  (Data Wrangler container) based pipeline.
- The **Instance type / count and IAM Role** you set here are the processing job config; they
  are recorded in the `.flow`'s `internal_metadata.dw_job` and should match the pipeline code
  (`config.DW_CONTAINER`, Processor instance settings).

> Summary: **Auto job configuration OFF → select SageMaker Processing.** Exporting this way lets
> the ML team re-run the `.flow` as a SageMaker Processing Job / Pipeline as-is.

### 2) Check processing job details (SageMaker AI console)
Exporting **creates one SageMaker Processing Job**. Check its settings in the console.

- SageMaker AI console → left panel **Data preparation → Processing jobs**
- Click the newly created job (e.g., `canvas-dw-export-s3-<timestamp>`) → collect the following:

| Info to collect | Console location (Processing job detail) | Used for |
|---|---|---|
| **Image URI** (Data Wrangler container) | App specification / Container image | `config.DW_CONTAINER` |
| **flow input S3 URI** | Processing input (name = `flow`) | `.flow` download location in 4) |
| **Instance type / count** | Resources configuration | `Processor` instance settings |
| **IAM Role ARN** | Role | `config.ROLE` reference |
| **Output S3 path** | Processing output configuration | (reference) |

- Some of these are also inside the `.flow`:
  `internal_metadata.dw_job.container_uri`, `dw_job.instance_type`.
- **`DEST_OUTPUT_NAME`** comes from the `.flow`'s DESTINATION node
  (`node_id` + `outputs[].name` → `{node_id}.{output}`). Quick check:
```bash
jq '.nodes[] | {type, node_id, name}' your.flow
jq '.internal_metadata.dw_job' your.flow
```

### 3) Set up the S3 bucket (version-friendly structure)
```
s3://<bucket>/pipeline_01/
├── raw/loans/
│   ├── incoming/part-1/       # where new inference data arrives (no target)
│   └── v1/part-1/, v1/part-2/ # where the flow SOURCE reads
├── flows/loans-join/          # keep the edited .flow (Git/versioned)
└── runs/<execution_id>/       # per-run preprocess·features·predictions·results (auto-created)
```
Enable bucket **Versioning** (guards against accidental delete/overwrite):
```bash
aws s3api put-bucket-versioning --bucket <bucket> \
  --versioning-configuration Status=Enabled
```

### 4) Download · understand · edit · re-upload the flow file

**(a) Download** — get the `.flow` from the flow input S3 path found in 2):
```bash
aws s3 cp s3://<original-path>/xxxxx.flow ./edited_version1.flow
```

**(b) Understand the structure** — `.flow` is JSON, roughly like this (comments are explanatory):
```jsonc
{
  "nodes": [
    { "node_id": "d8651ed4-...", "type": "SOURCE",
      "operator": "sagemaker.s3_source_0.1",
      "parameters": { "dataset_definition": {
        "datasetSourceType": "Canvas Dataset",              // ← change to "S3"
        "s3ExecutionContext": { "s3Uri": "s3://.../part-1.csv" },  // ← change to new input path
        "canvasDatasetMetadata": { "datasetId": "..." }     // ← change to null
      }}},
    { "type": "TRANSFORM", "operator": "...infer_and_cast_type..." },   // type casting
    { "type": "TRANSFORM", "operator": "...join_tables...",             // join
      "parameters": { "join_type": "fullouter", "left_column": "id", "right_column": "id" }},
    { "node_id": "a19ccb7b-...", "type": "DESTINATION",
      "parameters": { "output_config": { "output_path": "s3://.../" }}}  // ← change to new output path
  ],
  "internal_metadata": {
    "dw_job": { "container_uri": "....dkr.ecr...", "instance_type": "ml.m5.4xlarge" }
  }
}
```

**(c) Edit points**
| Target | Field | Change |
|---|---|---|
| SOURCE (part-1, part-2 **each**) | `s3ExecutionContext.s3Uri` | new input path (`raw/loans/v1/part-1/`, `part-2/`) |
| SOURCE (part-1, part-2 **each**) | `datasetSourceType` | `"Canvas Dataset"` → `"S3"` |
| SOURCE (part-1, part-2 **each**) | `canvasDatasetMetadata` | object → `null` (read raw S3 directly instead of a registered dataset) |
| DESTINATION | `output_config.output_path` | new output path |

**(d) Re-upload** — upload the edited `.flow` to the bucket's `flows/` location:
```bash
aws s3 cp edited_version1.flow \
  s3://<bucket>/pipeline_01/flows/loans-join/edited_version1.flow
```

> ⚠️ A wrong path makes the processing job fail with `PATH_NOT_FOUND`. After upload, verify the
> SOURCE paths actually contain data with `aws s3 ls`. There are **two SOURCEs (part-1/part-2)** —
> edit both.

---

## Prerequisites

```bash
# AWS credentials (as appropriate for your environment)
export AWS_PROFILE=<your-profile>
export AWS_DEFAULT_REGION=us-east-1

# Latest SageMaker SDK recommended (ModelStep/schedule etc.)
python3 -m pip install -U sagemaker
```

Swap values by editing `config.py` or overriding via env vars (no code change):
```bash
export SM_EXECUTION_ROLE="arn:aws:iam::...:role/MyMLTeamRole"
export SM_BUCKET="my-bucket"
export SM_MODEL_NAME="my-model"
```

---

## 1) Model approve + deploy — `promote_model.py`

Run **once** when a new model version is registered.

```bash
python promote_model.py <model_package_arn>
# example:
python promote_model.py \
  arn:aws:sagemaker:us-east-1:111122223333:model-package/my-model-group/1
```

- What it does: (1) set the package to `Approved` → (2) create a Model object named `config.MODEL_NAME`
- **Cost**: the Model object is metadata → **$0** (no idle billing unlike Canvas real-time Deploy)
- Idempotent: skips approval if already approved. On re-run, deletes and recreates the Model object.

---

## 2) Batch inference — `inference_pipeline.py`

Run when new data is ready.

```bash
python inference_pipeline.py
```

- `upsert()` registers the pipeline definition (free) → `start()` actually runs it (billed)
- Flow: `PrepareInput` → `PreprocessNewData`(re-run flow) → `DropTargetColumn` → `BatchPredict` → `AssembleResults`
- Result: header + id + predictions CSV under `runs/<execution_id>/results/`
- Instances spin up only while running and auto-terminate (no idle cost)

---

## Caveats you must know

### (1) The input path is defined inside the flow
The preprocessing step mounts only the `.flow` file; the **actual raw data is read by the Data
Wrangler container from the flow's SOURCE `s3Uri`**. Current flow input paths:
```
s3://<BUCKET>/pipeline_01/raw/loans/v1/part-1/
s3://<BUCKET>/pipeline_01/raw/loans/v1/part-2/
```
→ To infer on new data, **overwrite files at these paths**. To use a different path each time,
parameterize the flow's SOURCE `s3Uri`.

### (2) Two-file JOIN timing
This flow joins part-1 + part-2 on `id`. To avoid running when only one file has arrived,
upload both files then a `_SUCCESS` sentinel file, and **trigger only on that sentinel**.

### (3) DW output name format — `{node_id}.{output_name}`
The Data Wrangler job requires the processing output name to match the flow's DESTINATION node.
An arbitrary name fails with:
```
ProcessingOutputs have invalid names ... {node id}.{output name}
```
`DEST_OUTPUT_NAME` in `inference_pipeline.py` is that value. **Change it if you change the flow.**
Find it via the `.flow`'s `"type":"DESTINATION"` node — its `node_id` and `outputs` name.

### (4) Target column handling (why PrepareInput + DropTargetColumn)
The training flow expects the target column present, but real inference data has none.
`PrepareInput` adds a dummy target if missing (so the flow passes), and `DropTargetColumn`
removes it afterward (so the model gets features only). Both are conditional/idempotent, so
target-bearing data is also handled safely.

### (5) Cost summary
| Item | Cost |
|---|---|
| Model object (deploy) | Free (metadata) |
| Pipeline definition / Registry | Free |
| Batch Transform / Processing run | Billed **only while running**; instances auto-reclaimed on finish |
| S3 outputs | Storage only |

---

## Automation (optional)

- **Scheduled batch**: SageMaker Pipelines native `PipelineSchedule` (cron/rate)
  ```python
  from sagemaker.workflow.triggers import PipelineSchedule
  schedule = PipelineSchedule(name="daily", cron="0 2 * * ? *")
  pipeline.put_triggers(triggers=[schedule], role_arn=ROLE)
  ```
- **On arrival**: S3 event → EventBridge rule → pipeline `StartPipelineExecution`
  (EventBridge supports SageMaker pipelines as a native target → no Lambda needed)
- **Approve→deploy automation**: Registry approval event (EventBridge) → run `promote_model.py`'s
  `deploy()` logic in a Lambda

---

## Permission checklist (config.ROLE)

- S3 read/write on the target bucket (`SM_BUCKET`)
- `iam:PassRole` (pass the execution role to jobs)
- `sagemaker:CreateProcessingJob`, `CreateTransformJob`, `CreateModel`, `DeleteModel`
- `sagemaker:UpdateModelPackage`, `DescribeModelPackage` (approval)
