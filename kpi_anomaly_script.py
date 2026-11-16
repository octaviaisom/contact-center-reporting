"""Build a weekly call center KPI report from a CSV or a pandas DataFrame."""

import argparse
import csv
import re
from html import unescape
from pathlib import Path
from tempfile import NamedTemporaryFile

import numpy as np
import pandas as pd


# Edit this filename in VS Code, then click Run Python File.
# A relative path starts in the script folder. A full path can point anywhere.
INPUT_CSV = Path("contact_center_data.csv")


DURATIONS = ["talk_time_seconds", "wait_time_seconds"]
METRICS = ["call_volume", "answered_calls", "avg_talk_time_seconds",
           "fcr", "csat", "avg_wait_time_seconds"]
SERIES_COLUMNS = ["grouping", "category", "metric_name"]


def standardize_headers(contact_center_data):
    """Decode HTML and normalize source headers before processing any values."""
    names = {
        "call_number": "call_id",
        "talk_time_s": "talk_time_seconds",
        "wait_time_s": "wait_time_seconds",
        "handle_time_s": "handle_time_seconds",
    }
    data = contact_center_data.copy()
    headers = []
    for column in data.columns:
        clean = unescape(str(column)).strip().lower().replace("#", " number")
        clean = re.sub(r"[^a-z0-9]+", "_", clean).strip("_")
        headers.append(names.get(clean, clean))

    if pd.Index(headers).has_duplicates:
        raise ValueError("Two or more source headers normalize to the same name.")
    data.columns = headers
    return data


def calculate_weekly_kpis(calls):
    """Aggregate each reporting cut independently and reshape the six KPIs."""
    # Each mask applies only to the measure that needs it.
    answered = calls["abandon_flag"].eq("N")
    resolved = calls["first_contact_resolution_flag"].eq("Y")
    calls["answered_call_id"] = calls["call_id"].where(answered)
    calls["resolved_call_id"] = calls["call_id"].where(resolved)
    calls["answered_wait_time"] = calls["wait_time_seconds"].where(answered)

    results = []
    for grouping, label_column in [
        ("overall", None), ("customer", "customer"), ("call_reason", "call_reason")
    ]:
        keys = ["week_start_date"]
        if label_column is not None:
            keys.append(label_column)

        weekly = calls.groupby(keys, dropna=False, observed=True).agg(
            call_volume=("call_id", "nunique"),
            answered_calls=("answered_call_id", "nunique"),
            resolved_calls=("resolved_call_id", "nunique"),
            avg_talk_time_seconds=("talk_time_seconds", "mean"),
            csat=("customer_satisfaction", "mean"),
            avg_wait_time_seconds=("answered_wait_time", "mean"),
        ).reset_index()

        answered_count = weekly["answered_calls"]
        weekly["fcr"] = weekly["resolved_calls"].div(answered_count.where(answered_count.ne(0)))
        weekly["grouping"] = grouping
        if label_column is None:
            weekly["category"] = "Overall"
        else:
            weekly = weekly.rename(columns={label_column: "category"})

        results.append(weekly.melt(
            id_vars=["week_start_date", "grouping", "category"],
            value_vars=METRICS, var_name="metric_name", value_name="metric_value",
        ))

    return pd.concat(results, ignore_index=True)


def classify_anomalies(zscore):
    """Classify how far the value is from its own historical baseline."""
    magnitude = zscore.abs()
    return np.select(
        [zscore.isna(), magnitude.gt(3), magnitude.gt(2), magnitude.gt(1)],
        ["Insufficient Data", "Critical", "Investigate", "Monitor"],
        default="Normal",
    )


def add_time_series_metrics(report):
    """Add week-over-week changes, rolling statistics, and flags per series."""
    report = report.sort_values(SERIES_COLUMNS + ["week_start_date"], ignore_index=True)
    series = report.groupby(SERIES_COLUMNS, sort=False, dropna=False, observed=True)
    series_id = series.ngroup()
    previous_date = series["week_start_date"].shift()
    consecutive = (report["week_start_date"] - previous_date).eq(pd.Timedelta(days=7))
    previous_value = series["metric_value"].shift()
    difference = report["metric_value"] - previous_value

    # Count and duration changes are percentages. FCR changes are percentage
    # points, and CSAT changes are points on the original score scale.
    pop_change = difference.div(previous_value.where(previous_value.ne(0))) * 100
    fcr_rows = report["metric_name"].eq("fcr")
    csat_rows = report["metric_name"].eq("csat")
    pop_change.loc[fcr_rows] = difference.loc[fcr_rows] * 100
    pop_change.loc[csat_rows] = difference.loc[csat_rows]
    report["pop_change"] = pop_change.where(consecutive)

    for source, prefix in [("metric_value", ""), ("pop_change", "pop_")]:
        values = report[source]
        history = values.groupby(series_id, sort=False).shift()
        # Exclude this week. Use up to six prior observations and require
        # at least three valid values to compute the baseline.
        rolling = history.groupby(series_id, sort=False).rolling(6, min_periods=3)
        mean = rolling.mean().droplevel(0)
        std = rolling.std().droplevel(0)
        zscore = (values - mean).div(std.where(std.ne(0)))
        report[f"{prefix}rolling_mean"] = mean
        report[f"{prefix}rolling_std"] = std
        report[f"{prefix}zscore"] = zscore
        report[f"{prefix}zscore_flag"] = classify_anomalies(zscore)

    # A missing week or a non-abnormal KPI value ends the streak.
    abnormal = report["zscore_flag"].isin(["Investigate", "Critical"])
    streak_id = (~abnormal | ~consecutive).cumsum()
    report["sustained_count"] = abnormal.groupby(streak_id, sort=False).cumsum()
    sustained = report["sustained_count"].ge(3)
    report["sustained_flag"] = np.where(sustained, "Sustained", "Normal")

    flags = report[["zscore_flag", "pop_zscore_flag"]]
    report["priority_flag"] = np.select(
        [
            flags.eq("Critical").any(axis=1),
            sustained | flags.eq("Investigate").any(axis=1),
            flags.eq("Monitor").any(axis=1),
            flags.eq("Insufficient Data").all(axis=1),
        ],
        ["Critical", "High", "Monitor", "Insufficient Data"],
        default="Normal",
    )
    return report


def build_weekly_call_kpi_report(contact_center_data):
    """Return one row per Sunday-start week, reporting cut, category, and KPI."""
    calls = standardize_headers(contact_center_data)
    required = [
        "call_id", "call_date_time", "abandon_flag",
        "first_contact_resolution_flag", "customer_satisfaction",
        "customer", "call_reason", *DURATIONS,
    ]
    missing = sorted(set(required) - set(calls.columns))
    if missing:
        raise ValueError(f"Missing required columns after header cleaning: {missing}")
    calls = calls[required].copy()
    if calls["call_id"].isna().any() or calls["call_id"].astype("string").str.strip().eq("").any():
        raise ValueError("The Call # column contains a blank call ID.")

    raw_dates = calls["call_date_time"].astype("string").str.strip().str.lstrip("*").str.strip()
    call_dates = pd.to_datetime(raw_dates)
    if call_dates.isna().any():
        raise ValueError("Every call needs a call_date_time for weekly aggregation.")
    # A week ending on Saturday starts on Sunday.
    calls["week_start_date"] = call_dates.dt.to_period("W-SAT").dt.start_time

    for column in [*DURATIONS, "customer_satisfaction"]:
        try:
            values = pd.to_numeric(calls[column], errors="raise").astype(float)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{column} must contain numbers or blank values.") from error
        if np.isinf(values).any():
            raise ValueError(f"{column} must contain finite numbers or blank values.")
        calls[column] = values
    for column in ["abandon_flag", "first_contact_resolution_flag"]:
        flags = calls[column].astype("string").str.strip().str.upper()
        flags = flags.mask(flags.eq("").fillna(False))
        if not flags.dropna().isin(["Y", "N"]).all():
            raise ValueError(f"{column} must contain Y, N, or a blank value.")
        calls[column] = flags

    report = add_time_series_metrics(calculate_weekly_kpis(calls))
    if report.duplicated(["week_start_date", *SERIES_COLUMNS]).any():
        raise ValueError("Duplicate weekly KPI rows found.")
    return report


def read_call_csv(input_csv):
    """Read a comma-delimited call export and check each row's field count."""
    with Path(input_csv).open(encoding="utf-8-sig", newline="") as source:
        reader = csv.reader(source, strict=True)
        headers = next((row for row in reader if row), None)
        if headers is None:
            raise ValueError("The CSV is empty.")

        rows = []
        for row in reader:
            if not row:
                continue
            if len(row) != len(headers):
                raise ValueError(
                    f"CSV line {reader.line_num}: expected {len(headers)} fields, "
                    f"found {len(row)}."
                )
            # Keep IDs as text, including leading zeros.
            rows.append([value if value.strip() else None for value in row])

    if not rows:
        raise ValueError("The CSV has headers but no call records.")

    return pd.DataFrame(rows, columns=headers)


def run_csv_report(input_csv):
    """Read a call CSV and write the report beside it. Return the output path."""
    input_path = Path(input_csv).expanduser().absolute()
    contact_center_data = read_call_csv(input_path)
    report = build_weekly_call_kpi_report(contact_center_data)
    output_path = input_path.with_name(f"{input_path.stem}_weekly_kpi_report.csv")

    # Export to a temporary file first so an interrupted write leaves the
    # previous report available.
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="", dir=output_path.parent,
        suffix=".tmp", delete=False,
    ) as output:
        temporary_path = Path(output.name)
        try:
            report.to_csv(output, index=False)
            output.close()
            temporary_path.replace(output_path)
        finally:
            temporary_path.unlink(missing_ok=True)

    return output_path


def main():
    parser = argparse.ArgumentParser(
        description="Validate a call CSV and write a weekly KPI report beside it."
    )
    parser.add_argument(
        "input_csv", nargs="?", type=Path,
        help="Path to the source CSV. Defaults to INPUT_CSV at the top of the script.",
    )
    args = parser.parse_args()
    input_csv = args.input_csv
    if input_csv is None:
        input_csv = Path(__file__).resolve().parent / INPUT_CSV.expanduser()
    try:
        output = run_csv_report(input_csv)
    except FileNotFoundError as error:
        parser.exit(1, f"Could not find the CSV or output directory: {error.filename}\n")
    except (OSError, ValueError, csv.Error) as error:
        parser.exit(1, f"Could not complete the report: {error}\n")
    print(f"CSV validated. Report saved to {output}")


if __name__ == "__main__":
    main()
