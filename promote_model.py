"""
================================================================================
 promote_model.py  —  [Flow 1] Model promotion: Approve + Deploy
================================================================================

[What this script does]
  1) approve: set a model package registered in the Model Registry to 'Approved'
  2) deploy : create a 'SageMaker Model object' from the approved package

[Where it fits in the overall workflow]
  Business (Canvas) trains -> registers to Model Registry (Pending)
        │
        ▼
  [this script] ML team approves + deploys          <-- here
        │
        ▼
  inference_pipeline.py runs batch inference on new data with the deployed model

[When to run]
  Run 'once' per new model version.
  (Not repeated per new data like the inference pipeline.)

────────────────────────────────────────────────────────────────────────────
[Key concept] What "deploy" means in the batch approach
  - Here, "deploy" does NOT create an always-on real-time endpoint.
  - It only creates a 'Model object' (a definition of which artifact to run in which container).
  - ★ A Model object is 'metadata', so it costs $0 just to exist.
    Real cost occurs only while inference_pipeline's Batch Transform 'runs'.
  - By contrast, Canvas's 'Deploy' button creates a real-time endpoint that bills
    'continuously from the moment of deploy'.
────────────────────────────────────────────────────────────────────────────

[Usage]
  (A) Manual, once:
        python promote_model.py <model_package_arn>
      e.g.
        python promote_model.py \\
          arn:aws:sagemaker:us-east-1:111122223333:model-package/my-model-group/1

  (B) Automation: wrap this logic in a Lambda triggered by an 'approval event
      (EventBridge)' so approval flows straight into deploy.

[Required permissions] (config.ROLE or the running principal)
  - sagemaker:DescribeModelPackage, sagemaker:UpdateModelPackage  (approve)
  - sagemaker:CreateModel, sagemaker:DeleteModel                  (deploy)
  - iam:PassRole (attach the execution role to the Model)
================================================================================
"""
import sys
import boto3

from config import REGION, ROLE, MODEL_NAME

sm = boto3.client("sagemaker", region_name=REGION)


def approve(model_package_arn: str):
    """
    Set the model package to 'Approved'.

    Idempotent:
      Skips if it is already Approved.
      -> Safe to run multiple times, and avoids unnecessary audit-log entries.
    """
    desc = sm.describe_model_package(ModelPackageName=model_package_arn)
    if desc.get("ModelApprovalStatus") != "Approved":
        sm.update_model_package(
            ModelPackageArn=model_package_arn,
            ModelApprovalStatus="Approved",
            # ApprovalDescription is an audit note of 'who/why approved'.
            # This record matters in regulated (e.g., finance) environments.
            ApprovalDescription="Promoted for batch inference",
        )
        print(f"Approved: {model_package_arn}")
    else:
        print(f"Already approved: {model_package_arn}")


def deploy(model_package_arn: str):
    """
    Create a SageMaker Model object from the approved package (the batch-inference 'deploy').

    Fixed-name reuse strategy (config.MODEL_NAME):
      - The inference pipeline always references the model by the same name (MODEL_NAME),
        so when deploying a new version we 'delete the existing same-name Model and recreate it'.
      - This means (1) no need to change the inference pipeline code, and
        (2) Model objects don't pile up.
    """
    # 1) Delete an existing same-name Model if present (silently skip if absent)
    try:
        sm.delete_model(ModelName=MODEL_NAME)
        print(f"Deleted existing model: {MODEL_NAME}")
    except sm.exceptions.ClientError:
        pass  # first run: no Model yet

    # 2) Create a Model object whose container is the approved model package
    sm.create_model(
        ModelName=MODEL_NAME,
        ExecutionRoleArn=ROLE,
        Containers=[{"ModelPackageName": model_package_arn}],
    )
    print(f"Created model: {MODEL_NAME}")


if __name__ == "__main__":
    # Take the 'model package ARN to approve/deploy' as a CLI argument.
    if len(sys.argv) < 2:
        print("usage: python promote_model.py <model_package_arn>")
        sys.exit(1)

    arn = sys.argv[1]
    approve(arn)   # step 1: approve
    deploy(arn)    # step 2: deploy (create Model object)
    print(f"Done. The inference pipeline references this model as model_name='{MODEL_NAME}'.")
