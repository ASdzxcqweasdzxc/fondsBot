"""Offline audit of the research fixture, not a production analysis bot."""
import argparse
import hashlib
import json
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def read_json(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Write reference results after auditing inputs")
    args = parser.parse_args()
    manifest = read_json("source-manifest.json")
    data = read_json("facts.json")
    sources = {source["id"]: source for source in manifest["sources"]}
    facts = {fact["fact_id"]: fact for fact in data["facts"]}
    if len(facts) != len(data["facts"]):
        raise ValueError("Duplicate fact_id")
    cutoff = datetime.fromisoformat(manifest["as_of"])
    archive_checks = 0
    for source in sources.values():
        path = source.get("archive_path")
        if path:
            archive = (ROOT / path).resolve()
            if not archive.is_relative_to(ROOT):
                raise ValueError("Archive outside fixture")
            if sha256(archive) != source["original_sha256"]:
                raise ValueError(f"Archive hash mismatch: {path}")
            archive_checks += 1
        published = source.get("published_at")
        if published and datetime.fromisoformat(published) > cutoff:
            raise ValueError(f"Publication after cutoff: {source['id']}")
    for fact in facts.values():
        source = sources[fact["source_id"]]
        if source.get("usable_for_reference") is False:
            raise ValueError(f"Candidate source used: {fact['fact_id']}")
        if not fact.get("locator") or not fact.get("unit"):
            raise ValueError(f"Missing provenance: {fact['fact_id']}")
        if fact.get("period_end") and fact["period_end"] > manifest["as_of"][:10]:
            raise ValueError(f"Future period: {fact['fact_id']}")
    price_payload = read_json("sources/moex-sber-2024-08-08.json")["history"]
    rows = [dict(zip(price_payload["columns"], row)) for row in price_payload["data"]]
    matching = [row for row in rows if row["SECID"] == "SBER" and row["BOARDID"] == "TQBR" and row["TRADEDATE"] == "2024-08-08"]
    if len(matching) != 1:
        raise ValueError("Expected exactly one SBER/TQBR historical row")
    row = matching[0]

    def value(fact_id):
        return Decimal(facts[fact_id]["value"])

    for name, column in [("moex_close", "CLOSE"), ("moex_legal_close", "LEGALCLOSEPRICE")]:
        if value(name) != Decimal(str(row[column])):
            raise ValueError(f"Manual price differs from original response: {name}")

    results = []

    def calculation(calc_id, rule, inputs, expression, result, unit, expected_fact=None, note=None):
        item = {"calculation_id": calc_id, "rule_id": rule, "input_fact_ids": inputs,
                "expression": expression, "value": format(result.quantize(Decimal("0.000001")), "f"),
                "unit": unit, "status": "REFERENCE_ONLY"}
        if expected_fact:
            if result != value(expected_fact):
                raise ValueError(f"Reference mismatch: {calc_id}")
            item["matches_reported_fact_id"] = expected_fact
        if note:
            item["limitation"] = note
        results.append(item)

    calculation("REF-01", "FA-003", ["profit_h1_2024", "profit_q1_2024"],
                "816.1 - 397.4", value("profit_h1_2024") - value("profit_q1_2024"),
                "billion_RUB", "profit_q2_2024")
    calculation("REF-02", "FA-003", ["net_interest_income_h1_2024", "net_interest_income_q1_2024"],
                "1413.1 - 699.9", value("net_interest_income_h1_2024") - value("net_interest_income_q1_2024"),
                "billion_RUB", "net_interest_income_q2_2024")
    calculation("REF-03", "FA-003", ["operating_expenses_h1_2024", "operating_expenses_q1_2024"],
                "-480.3 - (-227.6)", value("operating_expenses_h1_2024") - value("operating_expenses_q1_2024"),
                "billion_RUB", "operating_expenses_q2_2024")
    cir = abs(value("operating_expenses_h1_2024")) / value("operating_income_pre_provisions_h1_2024") * 100
    if cir.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP) != value("cir_h1_2024_reported"):
        raise ValueError("CIR does not match reported one-decimal rounding")
    calculation("REF-04", "FA-006", ["operating_expenses_h1_2024", "operating_income_pre_provisions_h1_2024"],
                "abs(-480.3) / 1657.6 * 100", cir, "percent",
                note="Reported 29.0% matches one-decimal rounding; not evidence of investment attractiveness.")
    calculation("REF-05", "FA-006", ["nim_q2_2024_reported", "nim_q1_2024_reported"],
                "5.84 - 5.88", value("nim_q2_2024_reported") - value("nim_q1_2024_reported"), "percentage_points",
                note="Difference of reported quarterly ratios, not recalculated NIM.")
    calculation("REF-06", "FA-007", ["impaired_including_poci_2024_06", "impaired_including_poci_2023_12_comparative"],
                "3.5 - 3.4", value("impaired_including_poci_2024_06") - value("impaired_including_poci_2023_12_comparative"), "percentage_points",
                note="Reported impaired loans including POCI, not pure Stage 3 or NPL.")
    calculation("REF-07", "FA-007", ["coverage_impaired_2024_06", "coverage_impaired_2023_12_comparative"],
                "127.2 - 142.2", value("coverage_impaired_2024_06") - value("coverage_impaired_2023_12_comparative"), "percentage_points",
                note="Reported coverage movement; numerator and denominator not independently recalculated.")
    diagnostics = [
        {"id": "D-EPS", "input_fact_ids": ["eps_q1_2024", "eps_q2_2024", "eps_h1_2024"],
         "quarter_sum": str(value("eps_q1_2024") + value("eps_q2_2024")), "reported_half_year": str(value("eps_h1_2024")),
         "status": "DO_NOT_SUM_EPS", "reason": "Quarter sum differs by 0.05 RUB. Weighted shares/holders need source reconciliation; no TTM EPS constructed."},
        {"id": "D-N20-COMPARATIVE", "input_fact_ids": ["n20_0_2023_12_original", "n20_0_2023_12_h1_comparative"],
         "difference_pp": str(value("n20_0_2023_12_h1_comparative") - value("n20_0_2023_12_original")),
         "status": "UNRESOLVED_COMPARATIVE_DIFFERENCE", "reason": "Same historical date, 13.7 vs 13.8 in different releases. No explanation of revision obtained; no trend or headroom derived."},
        {"id": "D-PRICE", "input_fact_ids": ["moex_close", "moex_legal_close"],
         "status": "DISTINCT_PRICE_FIELDS", "reason": "CLOSE=281.09 and LEGALCLOSEPRICE=282.45 are distinct fields; no policy silently chooses one."}
    ]
    output = {"schema_version": "0.1", "dataset_id": manifest["dataset_id"],
              "input_sha256": {"facts.json": sha256(ROOT / "facts.json"), "source-manifest.json": sha256(ROOT / "source-manifest.json")},
              "archive_hashes_verified": archive_checks, "facts_count": len(facts),
              "validation_scope": "Offline archive/provenance links and arithmetic only; not full rule/profile/strategy validation",
              "calculations": results, "diagnostics": diagnostics,
              "unavailable_calculations": ["TTM_profit", "profit_growth_yoy_calculated", "ROE_TTM_calculated", "normalized_profit", "CoR_calculated", "capital_headroom", "SBER_P_E_TTM", "SBER_P_B", "dividend_yield", "residual_income_value"]}
    target = ROOT / "reference-calculations.json"
    if args.write:
        target.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    elif not target.exists() or read_json(target.name) != output:
        raise ValueError("Reference results differ or are absent; inspect input changes before --write")
    print(f"Verified {archive_checks} archives, {len(facts)} facts, {len(results)} arithmetic references; fixture remains DRAFT_PARTIAL")


if __name__ == "__main__":
    main()
