"""
================================================================================
 inference_pipeline.py  —  [Flow 2] Batch inference pipeline
================================================================================

[What this pipeline does]
  When new input data is ready, it runs the following steps in sequence, automatically.

    STEP 0) PrepareInput       : normalize input format — if the target column is missing,
                                 fill a dummy column to match the flow schema (column count),
                                 done BEFORE the flow reads the data
    STEP 1) PreprocessNewData  : re-run the Data Wrangler flow to preprocess/join new data
    STEP 2) DropTargetColumn   : remove the target column (config.TARGET_COLUMN) by name (dummy/real)
    STEP 3) BatchPredict       : batch inference with the 'deployed model' on features-only data
    STEP 4) AssembleResults    : join original(id·features)+predictions, add header -> Excel-friendly CSV

  ┌────────────┐   ┌────────────┐   ┌──────────────┐   ┌────────────┐   ┌──────────────┐
  │PrepareInput│──►│Preprocess  │──►│DropTarget    │──►│BatchPredict│──►│AssembleResults│──► results/
  │(normalize) │   │(DW flow)   │   │Column        │   │(Transform) │   │(join+header) │
  └────────────┘   └────────────┘   └──────────────┘   └────────────┘   └──────────────┘

  ※ Why PrepareInput + DropTargetColumn both exist:
     The training flow expects the target (answer) column as 'one of the required columns'.
     - Real inference data has no target -> STEP 0 fills a 'dummy target' so the flow passes.
     - The inference model does not want the target -> STEP 2 removes that column again.
     Result: the (training) flow can be reused for inference without modification
     (reflects an issue we actually hit).

[Prerequisite]
  - Run promote_model.py first so the Model object (config.MODEL_NAME) already exists.
    This pipeline only 'references it by name' (it does not create the model).

[When it runs - triggers]
  - Manual:        python inference_pipeline.py
  - Scheduled:     SageMaker Pipelines schedule (PipelineSchedule) - built-in
  - On arrival:    S3 event -> EventBridge -> StartPipelineExecution on this pipeline

────────────────────────────────────────────────────────────────────────────
[Caveats you must know]

  (1) The input data path is defined 'inside the flow file', not 'in this code'.
      - STEP 1 mounts only the .flow file; the actual raw data is read directly by the
        Data Wrangler container from the s3Uri embedded in the flow's SOURCE nodes.
      - Current flow (edited_version1.flow) input paths:
            s3://.../pipeline_01/raw/loans/v1/part-1/
            s3://.../pipeline_01/raw/loans/v1/part-2/
      - So to 'infer on new data' you must overwrite files at those paths.
        (To use a different path each time, parameterize the flow's SOURCE s3Uri.)

  (2) This flow JOINs two files (part-1, part-2) on 'id'.
      - If an auto-trigger fires when only one file has arrived, the join is incomplete.
      - Safeguard: after uploading both files, upload a '_SUCCESS' sentinel last and
        trigger only on that sentinel.

  (3) ★ The DW processing output name MUST be in '{node_id}.{output_name}' format. ★
      - The Data Wrangler job requires the output name to match the flow's DESTINATION node.
      - An arbitrary name (e.g., 'processed') fails with:
            "ProcessingOutputs have invalid names ... {node id}.{output name}"
      - DEST_OUTPUT_NAME below is that exact value (reflects an issue we actually hit).

  (4) Cost: 'start'ing this pipeline briefly spins up processing/transform instances and is
      billed. Instances auto-terminate when jobs finish, so there is no idle cost.
      (upsert, which only registers the definition, is free.)
================================================================================
"""

import sagemaker
from sagemaker.workflow.pipeline import Pipeline
from sagemaker.workflow.parameters import ParameterString
from sagemaker.workflow.steps import ProcessingStep, TransformStep
from sagemaker.processing import Processor, ProcessingInput, ProcessingOutput
from sagemaker.sklearn.processing import SKLearnProcessor
from sagemaker.transformer import Transformer
from sagemaker.inputs import TransformInput
from sagemaker.workflow.execution_variables import ExecutionVariables
from sagemaker.workflow.functions import Join

from config import ROLE, BUCKET, DW_CONTAINER, TARGET_COLUMN

session = sagemaker.session.Session()

# ---------------------------------------------------------------------------
# Pipeline parameters
#   - 'Input variables' you can change at run time; swap paths/model without code edits.
#   - Also used to control issues like the 'wrong path' problem we hit earlier.
# ---------------------------------------------------------------------------
p_flow_uri = ParameterString(
    name="InferenceFlowS3Uri",
    default_value=f"s3://{BUCKET}/pipeline_01/flows/loans-join/edited_version1.flow",
)
# Where newly arrived inference data (part-1, target may be absent) is placed
p_incoming_uri = ParameterString(
    name="IncomingPart1S3Uri",
    default_value=f"s3://{BUCKET}/pipeline_01/raw/loans/incoming/part-1/",
)
# Where the flow's part-1 SOURCE reads (PrepareInput writes its normalized output here)
p_flow_part1_uri = ParameterString(
    name="FlowPart1S3Uri",
    default_value=f"s3://{BUCKET}/pipeline_01/raw/loans/v1/part-1/",
)

# ---------------------------------------------------------------------------
# Intermediate/result paths — separated into 'per-run folders' (runs/<execution_id>/...)
#   Why: with fixed paths, multiple runs' preprocessing outputs pile up under the same
#        prefix, so the next step reads 'previous runs' too and predictions get polluted
#        (an issue we actually hit).
#   ExecutionVariables.PIPELINE_EXECUTION_ID resolves to each run's unique ID at run time.
# ---------------------------------------------------------------------------
_run_base = Join(on="/", values=[
    f"s3://{BUCKET}/pipeline_01/runs", ExecutionVariables.PIPELINE_EXECUTION_ID,
])
prep_out = Join(on="/", values=[_run_base, "processed"])    # preprocessing result (target included)
feature_out = Join(on="/", values=[_run_base, "features"])  # features only (target removed)
pred_out = Join(on="/", values=[_run_base, "predictions"])  # final predictions
results_out = Join(on="/", values=[_run_base, "results"])   # original+prediction join (with header)
p_model_name = ParameterString(  # Model object name created by promote_model.py
    name="DeployedModelName",
    default_value="loans-default-model",
)

# ★ See caveat (3): output name matched to the flow's DESTINATION node.
#   edited_version1.flow's DESTINATION node_id = a19ccb7b-... , output = default
#   -> Change this value if you change the flow.
#   (How to find: node_id and outputs name of the "type":"DESTINATION" node in the .flow)
DEST_OUTPUT_NAME = "a19ccb7b-c715-4944-b27d-23f5467a4e0b.default"

# ---------------------------------------------------------------------------
# STEP 0 - Normalize input to the flow schema (add dummy column if target missing)
# ---------------------------------------------------------------------------
# Real inference data has no target, so the flow (which enforces the column count) fails.
# Fill a dummy target before the flow reads the data (DropTargetColumn removes it later).
prepare_processor = SKLearnProcessor(
    framework_version="1.2-1",
    role=ROLE,
    instance_count=1,
    instance_type="ml.m5.large",
    sagemaker_session=session,
)
step_prepare = ProcessingStep(
    name="PrepareInput",
    processor=prepare_processor,
    inputs=[
        ProcessingInput(source=p_incoming_uri, destination="/opt/ml/processing/input")
    ],
    outputs=[
        # Write the normalized output to the flow's part-1 SOURCE path.
        ProcessingOutput(output_name="prepared",
                         source="/opt/ml/processing/output",
                         destination=p_flow_part1_uri)
    ],
    code="prepare_input.py",
    job_arguments=["--target-column", TARGET_COLUMN, "--position", "1"],
)

# ---------------------------------------------------------------------------
# STEP 1 - Processing job: re-run the Data Wrangler flow (preprocess/join new data)
# ---------------------------------------------------------------------------
# Processor: defines 'which container, which instance' runs the preprocessing.
#   - image_uri must match the Data Wrangler version that created the flow, for reproducibility.
dw_processor = Processor(
    role=ROLE,
    image_uri=DW_CONTAINER,
    instance_count=1,
    instance_type="ml.m5.4xlarge",
    sagemaker_session=session,
)
step_preprocess = ProcessingStep(
    name="PreprocessNewData",
    processor=dw_processor,
    # Input: mount only the .flow file into the container.
    #   The actual raw data is read by the container from the s3Uri inside the flow (caveat 1).
    inputs=[
        ProcessingInput(
            source=p_flow_uri,
            destination="/opt/ml/processing/flow",
            input_name="flow",
        )
    ],
    # Output: send the preprocessing result to the per-run path (prep_out).
    #   output_name must match the flow's DESTINATION (caveat 3).
    outputs=[
        ProcessingOutput(
            output_name=DEST_OUTPUT_NAME,
            source="/opt/ml/processing/output",
            destination=prep_out,
        )
    ],
)
# The flow reads a fixed S3 path directly, so force PrepareInput to finish first.
step_preprocess.add_depends_on([step_prepare])

# ---------------------------------------------------------------------------
# STEP 2 - Remove the target column (prepare inference input)
# ---------------------------------------------------------------------------
# The preprocessing result includes the target column, so remove it by name before inference.
#   - Run drop_target.py in an sklearn container (drop the column with pandas)
#   - Column name to remove is config.TARGET_COLUMN (passed via job_arguments)
drop_processor = SKLearnProcessor(
    framework_version="1.2-1",
    role=ROLE,
    instance_count=1,
    instance_type="ml.m5.large",
    sagemaker_session=session,
)
step_drop = ProcessingStep(
    name="DropTargetColumn",
    processor=drop_processor,
    inputs=[
        ProcessingInput(
            # Take STEP 1's preprocessing result (target included) as input.
            source=step_preprocess.properties.ProcessingOutputConfig
                       .Outputs[DEST_OUTPUT_NAME].S3Output.S3Uri,
            destination="/opt/ml/processing/input",
        )
    ],
    outputs=[
        ProcessingOutput(
            output_name="features",
            source="/opt/ml/processing/output",
            destination=feature_out,
        )
    ],
    code="drop_target.py",
    job_arguments=["--target-column", TARGET_COLUMN],
)

# ---------------------------------------------------------------------------
# STEP 3 - Batch Transform: score the features-only data with the 'pre-deployed model'
# ---------------------------------------------------------------------------
# Transformer: defines 'which model, which instance' runs batch inference.
#   - model_name : the Model object name created by promote_model.py (reference only)
#   - Instances spin up only during the run and auto-terminate (no idle cost).
transformer = Transformer(
    model_name=p_model_name,
    instance_count=1,
    instance_type="ml.m5.xlarge",
    output_path=pred_out,
    accept="text/csv",
    assemble_with="Line",
    sagemaker_session=session,
)
step_transform = TransformStep(
    name="BatchPredict",
    transformer=transformer,
    inputs=TransformInput(
        # Reference STEP 2 (target-removed) output as input -> dependency auto-detected.
        data=step_drop.properties.ProcessingOutputConfig
                 .Outputs["features"].S3Output.S3Uri,
        content_type="text/csv",
        split_type="Line",
    ),
)

# ---------------------------------------------------------------------------
# STEP 4 - Assemble results: join original(id·features) + predictions, add header (Excel-friendly)
# ---------------------------------------------------------------------------
assemble_processor = SKLearnProcessor(
    framework_version="1.2-1",
    role=ROLE,
    instance_count=1,
    instance_type="ml.m5.large",
    sagemaker_session=session,
)
step_assemble = ProcessingStep(
    name="AssembleResults",
    processor=assemble_processor,
    inputs=[
        # Preprocessing original (has header, id included)
        ProcessingInput(source=prep_out, destination="/opt/ml/processing/original"),
        # Batch Transform output (.out, no header)
        ProcessingInput(source=step_transform.properties.TransformOutput.S3OutputPath,
                        destination="/opt/ml/processing/predictions"),
    ],
    outputs=[
        ProcessingOutput(output_name="results",
                         source="/opt/ml/processing/output",
                         destination=results_out)
    ],
    code="assemble_results.py",
    job_arguments=["--target-column", TARGET_COLUMN],
)

# ---------------------------------------------------------------------------
# Assemble the pipeline
#   Execution order is determined by 'property references', not by list order.
#   (PrepareInput -> Preprocess -> DropTargetColumn -> BatchPredict -> AssembleResults)
# ---------------------------------------------------------------------------
pipeline = Pipeline(
    name="loans-inference-pipeline",
    parameters=[p_flow_uri, p_incoming_uri, p_flow_part1_uri, p_model_name],
    steps=[step_prepare, step_preprocess, step_drop, step_transform, step_assemble],
    sagemaker_session=session,
)

if __name__ == "__main__":
    # upsert(): create/update the pipeline 'definition' (free)
    pipeline.upsert(role_arn=ROLE)
    # start(): actually run -> processing/transform jobs run and are billed (caveat 4)
    execution = pipeline.start()
    print("Inference pipeline started:", execution.arn)
