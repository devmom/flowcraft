"""Data Analysis Skill Script — deterministic pandas-based analysis."""

import json
import sys
from datetime import datetime
from collections import Counter

def main():
    try:
        raw = sys.stdin.read()
        params = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        print(json.dumps({"status": "error", "message": f"Invalid input JSON: {e}"}))
        return

    file_path = params.get("file_path", "")
    operation = params.get("operation", "summarize")
    columns = params.get("columns", [])
    group_by = params.get("group_by", "")

    if not file_path:
        print(json.dumps({"status": "error", "message": "file_path is required"}))
        return

    try:
        # Try pandas first
        import pandas as pd
        if file_path.endswith('.csv'):
            df = pd.read_csv(file_path)
        elif file_path.endswith(('.xlsx', '.xls')):
            df = pd.read_excel(file_path)
        elif file_path.endswith('.json'):
            df = pd.read_json(file_path)
        else:
            df = pd.read_csv(file_path)

        result = {}

        if operation == "summarize":
            result["row_count"] = len(df)
            result["column_count"] = len(df.columns)
            result["columns"] = list(df.columns)
            result["dtypes"] = {c: str(t) for c, t in df.dtypes.items()}
            result["null_counts"] = df.isnull().sum().to_dict()
            num_cols = df.select_dtypes(include='number').columns.tolist()
            if num_cols:
                result["numeric_summary"] = df[num_cols].describe().to_dict()

        elif operation == "trend":
            if columns:
                col = columns[0]
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
                    series = df[col].dropna()
                    result["column"] = col
                    result["mean"] = float(series.mean())
                    result["median"] = float(series.median())
                    result["std"] = float(series.std())
                    result["min"] = float(series.min())
                    result["max"] = float(series.max())
                    result["count"] = len(series)

        elif operation == "correlation":
            num_df = df.select_dtypes(include='number')
            if len(num_df.columns) >= 2:
                corr = num_df.corr()
                result["correlation_matrix"] = corr.to_dict()

        elif operation == "group":
            if group_by and group_by in df.columns:
                if columns:
                    agg_col = columns[0]
                    if agg_col in df.columns:
                        grouped = df.groupby(group_by)[agg_col].agg(['count', 'mean', 'sum'])
                        result["grouped"] = grouped.to_dict()
                else:
                    result["group_counts"] = df[group_by].value_counts().to_dict()

        result["status"] = "success"
        result["summary"] = f"Analyzed {len(df)} rows, {len(df.columns)} columns"
        print(json.dumps(result, ensure_ascii=False, default=str))

    except ImportError:
        # Fallback: pure Python CSV analysis
        import csv
        rows = []
        with open(file_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)

        result = {"status": "success", "row_count": len(rows), "columns": list(rows[0].keys()) if rows else []}
        if columns:
            col = columns[0]
            values = [r.get(col, '') for r in rows]
            result["unique_values"] = len(set(values))
            result["most_common"] = Counter(values).most_common(5)
        result["summary"] = f"CSV analysis: {len(rows)} rows (pandas not available, used csv module)"
        print(json.dumps(result, ensure_ascii=False, default=str))
    except Exception as e:
        print(json.dumps({"status": "error", "message": str(e)}))

if __name__ == "__main__":
    main()
