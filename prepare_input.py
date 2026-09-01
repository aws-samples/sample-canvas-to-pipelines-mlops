"""
================================================================================
 prepare_input.py  —  Normalize input to match the flow schema
================================================================================

[Why needed]
  The training flow's type-casting node enforces the 'column count at training time
  (target included)'. But real inference data has no target (answer) column, so it is
  short by one column and the flow fails with:
      "Invalid schema column size. schema columns size: 19, dataframe column size: 18"

[Fix]
  'Before' the flow reads the data, if the target column is missing, insert a
  'dummy (placeholder) target column' to match the column count. This dummy column is
  later removed by the DropTargetColumn step, so it has no effect on predictions.

  input : /opt/ml/processing/input   (newly arrived inference data, target may be absent)
  output: /opt/ml/processing/output  (data normalized to the flow schema)

If the target already exists (training-format data), it passes through unchanged.
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
                    help="target column name the flow expects (config.TARGET_COLUMN)")
    ap.add_argument("--position", type=int, default=1,
                    help="index at which to insert the dummy target (its index in the original training data)")
    ap.add_argument("--placeholder", default="unknown",
                    help="dummy target value (removed before inference anyway)")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    csv_files = glob.glob(os.path.join(IN_DIR, "**", "*.csv"), recursive=True)
    if not csv_files:
        raise FileNotFoundError(f"No input CSV found: {IN_DIR}")

    for path in csv_files:
        df = pd.read_csv(path)
        if args.target_column not in df.columns:
            pos = min(args.position, df.shape[1])
            df.insert(pos, args.target_column, args.placeholder)
            print(f"added dummy '{args.target_column}' -> {df.shape[1]} columns "
                  f"({os.path.basename(path)})")
        else:
            print(f"'{args.target_column}' already present (pass-through): {os.path.basename(path)}")
        df.to_csv(os.path.join(OUT_DIR, os.path.basename(path)), index=False)


if __name__ == "__main__":
    main()
