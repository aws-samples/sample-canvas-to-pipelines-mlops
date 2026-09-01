"""
================================================================================
 config.py  —  Shared configuration (all scripts read values from here)
================================================================================

[Purpose]
  Centralizes values reused across the project (execution role, bucket, container
  image, model name). promote_model.py and inference_pipeline.py import them via
  `from config import ...`.

[Why]
  - Hardcoding values across many files makes later role/bucket changes error-prone.
  - Change here once and it applies to all scripts.

[How to change values - 2 ways]
  1) Edit the defaults in this file
  2) (No code change) Override via environment variables, e.g.:
        export SM_EXECUTION_ROLE="arn:aws:iam::111122223333:role/MyMLTeamRole"
        export SM_BUCKET="my-other-bucket"
     -> Precedence: env var > default below

[Public-repo note]
  The defaults below are PLACEHOLDERS, not real account info.
  For real runs, inject your own values via env vars
  (SM_ACCOUNT / SM_EXECUTION_ROLE / SM_BUCKET, etc.).
  (Do NOT commit real account/role/bucket values to git.)

[Notes]
  - REGION and BUCKET must be in the same region (avoids cross-region access failures).
  - ROLE must have the following to run the pipeline:
      * S3 read/write on BUCKET
      * iam:PassRole (to pass the role to processing/transform jobs)
      * SageMaker processing/transform/model-creation permissions
================================================================================
"""
import os

# --- Region / account --------------------------------------------------------
REGION = os.environ.get("SM_REGION", "us-east-1")
# Placeholder account ID. Inject the real value via env var SM_ACCOUNT.
ACCOUNT = os.environ.get("SM_ACCOUNT", "111122223333")

# --- Execution role ----------------------------------------------------------
# The IAM role that runs the jobs (processing/transform) and reads/writes S3.
# The value below is a placeholder; inject the real role via SM_EXECUTION_ROLE.
# (For production, prefer a dedicated ML-team role — business/ML permission split.)
_DEFAULT_ROLE = (
    f"arn:aws:iam::{ACCOUNT}:role/service-role/"
    "AmazonSageMaker-ExecutionRole-EXAMPLE"
)
ROLE = os.environ.get("SM_EXECUTION_ROLE", _DEFAULT_ROLE)

# --- Data / artifact bucket --------------------------------------------------
# Placeholder. Inject the real bucket via SM_BUCKET.
BUCKET = os.environ.get("SM_BUCKET", "your-mlops-bucket")

# --- Data Wrangler container image -------------------------------------------
# The container that runs the flow(.flow). Must match the original .flow's
# internal_metadata.dw_job.container_uri so preprocessing is reproduced.
# (Version 5.1.3 — matched to the Data Wrangler version that created the flow.)
DW_CONTAINER = os.environ.get(
    "SM_DW_CONTAINER",
    "663277389841.dkr.ecr.us-east-1.amazonaws.com/sagemaker-data-wrangler-container:5.1.3",
)

# --- Model object name referenced by the inference pipeline ------------------
# promote_model.py creates a Model object with this name, and
# inference_pipeline.py's Batch Transform references the model by this name.
MODEL_NAME = os.environ.get("SM_MODEL_NAME", "loans-default-model")

# --- Target column to drop at inference time ---------------------------------
# The training flow outputs the target (answer) column too, but inference input
# must not include the target (the model expects features only). We remove this
# column by name before batch inference.
TARGET_COLUMN = os.environ.get("SM_TARGET_COLUMN", "loan_status")
