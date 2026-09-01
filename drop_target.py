"""
================================================================================
 drop_target.py  —  Prepare inference input: remove the target column
================================================================================

The preprocessing (flow) result includes the target (answer) column because the flow
is for training. The batch-inference model expects 'features only', so we remove the
target column 'by name' before inference.

  input : /opt/ml/processing/input   (preprocessing result CSV, target included)
  output: /opt/ml/processing/output  (feature-only CSV with the target removed)

Name-based removal is safe even if column order changes.
(Called by the DropTargetColumn step in inference_pipeline.py.)
================================================================================
"""
import argparse
import glob
import os

import pandas as pd

IN_DIR = "/opt/ml/processing/input"
OUT_DIR = "/opt/ml/processing/output"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-column", required=True,
                    help="target column to remove (config.TARGET_COLUMN)")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    csv_files = glob.glob(os.path.join(IN_DIR, "**", "*.csv"), recursive=True)
    if not csv_files:
        raise FileNotFoundError(f"No input CSV found: {IN_DIR}")

    for i, path in enumerate(csv_files):
        df = pd.read_csv(path)   # read with header to know column names -> drop target by name
        if args.target_column in df.columns:
            df = df.drop(columns=[args.target_column])
            print(f"dropped '{args.target_column}': {os.path.basename(path)} -> {df.shape[1]} columns")
        else:
            print(f"'{args.target_column}' not found (skip): {os.path.basename(path)}")
        # ★ The batch-inference model (AutoGluon) reads CSV WITHOUT a header, positionally.
        #   A header row would be treated as data and cause a type-cast error
        #   ("invalid literal for int() ... 'id_0'"). So write header=False.
        #   Also use a fixed filename so repeated runs don't accumulate old results.
        out_path = os.path.join(OUT_DIR, f"features-{i:05d}.csv")
        df.to_csv(out_path, index=False, header=False)


if __name__ == "__main__":
    main()
