"""
================================================================================
 assemble_results.py  —  Make predictions Excel-friendly
================================================================================

Batch Transform output (.out) has no header and contains only predictions, which is
hard to read. This step:
  1) joins the preprocessing original (id·features, with header) with the predictions
     by 'row order'
  2) adds column headers and saves a single CSV  ->  easy to open in Excel

  input:
    /opt/ml/processing/original     preprocessing result (prep_out, has header, id included)
    /opt/ml/processing/predictions  Batch Transform output (.out, no header)
  output:
    /opt/ml/processing/output/predictions_with_input.csv  (with header)

Note: Batch Transform preserves input record order, so positional join is valid
      (single input file · SplitType=Line).
================================================================================
"""
import argparse
import glob
import os

import pandas as pd

ORIG_DIR = "/opt/ml/processing/original"
PRED_DIR = "/opt/ml/processing/predictions"
OUT_DIR = "/opt/ml/processing/output"

# AutoGluon batch output columns: predicted label, top probability, per-class probs, class order
PRED_COLS = ["predicted_label", "probability", "class_probabilities", "class_labels"]


def _read_concat(directory, **read_kwargs):
    files = sorted(f for f in glob.glob(os.path.join(directory, "**", "*"), recursive=True)
                   if os.path.isfile(f))
    frames = [pd.read_csv(f, **read_kwargs) for f in files]
    return pd.concat(frames, ignore_index=True) if frames else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-column", required=True,
                    help="target (real/dummy) column to drop from the original")
    args = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)

    original = _read_concat(ORIG_DIR)                  # has header (id·features)
    predictions = _read_concat(PRED_DIR, header=None)  # no header (.out)
    if original is None or predictions is None:
        raise FileNotFoundError("Could not find 'original' or 'predictions' input.")

    # The original's target column (real/dummy) is unneeded in the result -> drop it
    if args.target_column in original.columns:
        original = original.drop(columns=[args.target_column])

    predictions.columns = PRED_COLS[:predictions.shape[1]]

    if len(original) != len(predictions):
        print(f"WARN: row count mismatch (original={len(original)}, predictions={len(predictions)}) "
              f"-> joining on the shorter length")
    n = min(len(original), len(predictions))
    result = pd.concat(
        [original.iloc[:n].reset_index(drop=True),
         predictions.iloc[:n].reset_index(drop=True)],
        axis=1,
    )

    out_path = os.path.join(OUT_DIR, "predictions_with_input.csv")
    result.to_csv(out_path, index=False)   # ★ save WITH header
    print(f"Saved results: {out_path}, shape={result.shape}")


if __name__ == "__main__":
    main()
