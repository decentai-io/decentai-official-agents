"""Two lists, matched by the rules the user gave and nothing else.

Exact on every key: matched. Exact on every key but one: uncertain, shown side by side, never merged. Otherwise only
in A or only in B. A key with a blank part matches nothing: two rows with no email are not one customer. Matched
rows are diffed on the compare columns. Everything lands in a workbook with a change log; the inputs are untouched.

Matching is by hash: each row is looked up, never compared with every other row, so two lists of 100,000 rows take
seconds. A near match is looked up the same way, by the key with one part left out.
"""

import asyncio
from collections import defaultdict, deque

from standard import ToolBase

from .common import failed, job, opened
from .tables import Cell, blank_key, normalize
from .workbook import build, deliver, text_rows


class ReconcileTool(ToolBase):
    id = "reconcile"

    async def match(self, call):
        inputs = call.inputs
        try:
            book_a, sheet_a = await opened(call, inputs["file_ref_a"], inputs.get("sheet_a") or "",
                                           inputs.get("header_row_a"))
            book_b, sheet_b = await opened(call, inputs["file_ref_b"], inputs.get("sheet_b") or "",
                                           inputs.get("header_row_b"))
            keys = [(str(k["a"]), str(k["b"]), str(k.get("normalize") or "lowercase")) for k in inputs["keys"]]
            compare = [(str(c["a"]), str(c["b"])) for c in inputs.get("compare") or []]
            for a, b, _ in keys:
                sheet_a.column(a), sheet_b.column(b)
            for a, b in compare:
                sheet_a.column(a), sheet_b.column(b)
        except Exception as exc:
            return failed(exc)

        matched, uncertain, only_a, blank_a, only_b, blank_b = await asyncio.to_thread(
            self._paired, sheet_a, sheet_b, keys)
        differences = []
        for i, j in matched:
            for a, b in compare:
                left, right = sheet_a.cell(sheet_a.rows[i], a).text, sheet_b.cell(sheet_b.rows[j], b).text
                if normalize(left, "lowercase") != normalize(right, "lowercase"):
                    differences.append((i, j, a, left, right))

        log = []
        cols_a, rows_a = text_rows(sheet_a.columns, [sheet_a.rows[i] for i, _ in matched],
                                   {"Row in A": [sheet_a.row_number(i) for i, _ in matched],
                                    "Row in B": [sheet_b.row_number(j) for _, j in matched]})
        lone_a = only_a + blank_a
        cols_oa, rows_oa = text_rows(sheet_a.columns, [sheet_a.rows[i] for i in lone_a],
                                     {"Row in A": [sheet_a.row_number(i) for i in lone_a],
                                      "Why": ["no row in B has these keys" if i in set(only_a)
                                              else "a key is blank; never matched" for i in lone_a]})
        blank_b_set = set(blank_b)
        cols_ob, rows_ob = text_rows(sheet_b.columns, [sheet_b.rows[j] for j in only_b],
                                     {"Row in B": [sheet_b.row_number(j) for j in only_b],
                                      "Why": ["a key is blank; never matched" if j in blank_b_set
                                              else "no row in A has these keys" for j in only_b]})
        unc_rows, unc_pairs = [], []
        for i, j, differ in uncertain:
            agree = ", ".join(a for a, _, _ in keys if a != differ)
            unc_pairs.append({"row_a": sheet_a.row_number(i), "row_b": sheet_b.row_number(j),
                              "agree": agree, "differ": differ})
            unc_rows.append([Cell(sheet_a.row_number(i)), Cell(sheet_b.row_number(j)), Cell(agree), Cell(differ),
                             Cell(sheet_a.cell(sheet_a.rows[i], differ).text or None),
                             Cell(sheet_b.cell(sheet_b.rows[j], next(b for a, b, _ in keys if a == differ)).text
                                  or None)])
            log.append((sheet_a.row_number(i), "uncertain",
                        f"matches row {sheet_b.row_number(j)} of B on {agree} but not {differ}; not merged"))
        diff_rows = [[Cell(sheet_a.row_number(i)), Cell(sheet_b.row_number(j)), Cell(col), Cell(l or None),
                      Cell(r or None)] for i, j, col, l, r in differences]
        for i, j, col, l, r in differences:
            log.append((sheet_a.row_number(i), "differs", f"{col}: A '{l}' vs B '{r}' (row {sheet_b.row_number(j)})"))
        for i in only_a:
            log.append((sheet_a.row_number(i), "only in A", "no row in B matches on the keys"))
        for i in blank_a:
            log.append((sheet_a.row_number(i), "only in A", "a key is blank; a blank never matches"))
        for j in only_b:
            log.append((sheet_b.row_number(j), "only in B", "a key is blank; a blank never matches"
                        if j in blank_b_set else "no row in A matches on the keys"))
        log.insert(0, ("", "rules", "keys: " + "; ".join(f"A.{a} = B.{b} ({how})" for a, b, how in keys)
                       + (" | compare: " + ", ".join(f"A.{a} vs B.{b}" for a, b in compare) if compare else "")
                       + " | a blank key never matches"))
        try:
            raw, _ = await asyncio.to_thread(build, [("Matched", cols_a, rows_a), ("Only in A", cols_oa, rows_oa),
                            ("Only in B", cols_ob, rows_ob),
                            ("Uncertain", ["Row in A", "Row in B", "Agree on", "Differ on", "A value", "B value"],
                             unc_rows),
                            ("Differences", ["Row in A", "Row in B", "Column", "A value", "B value"], diff_rows)], log)
            file_ref, check = await asyncio.to_thread(deliver, call, str(inputs["filename"]), raw)
        except Exception as exc:
            return {"error": f"The workbook did not read back: {exc}"}, "error"
        blank_b_count = sum(1 for j in only_b if j in blank_b_set)
        summary = (f"{len(matched)} matched, {len(lone_a)} only in A, {len(only_b)} only in B, "
                   f"{len(uncertain)} uncertain, {len(differences)} differences; blank keys: "
                   f"{len(blank_a)} in A, {blank_b_count} in B")
        job_ref = await job(call, "reconcile", book_a.file_ref, summary, book_b.file_ref, check["filename"])
        return {"job_ref": job_ref, "file_ref": file_ref, "filename": check["filename"],
                "matched": len(matched), "only_in_a": len(lone_a), "only_in_b": len(only_b),
                "uncertain": len(uncertain), "differences": len(differences),
                "blank_keys_a": len(blank_a), "blank_keys_b": blank_b_count,
                "uncertain_pairs": unc_pairs[:100], "verified": True}, "success"

    @staticmethod
    def _paired(sheet_a, sheet_b, keys):
        """Every row of A looked up in B by its whole key, then what is left looked up by the key with one part left
        out. Hash lookups both times: near-linear in the rows, whatever their number."""

        def key(sheet, row, side):
            return tuple(normalize(sheet.cell(row, a if side == "a" else b).text, how) for a, b, how in keys)

        keys_b, exact_b, blank_b = {}, defaultdict(deque), []
        for j, row in enumerate(sheet_b.rows):
            if all(c.blank for c in row):
                continue
            k = key(sheet_b, row, "b")
            keys_b[j] = k
            if blank_key(k):
                blank_b.append(j)
            else:
                exact_b[k].append(j)

        used_b, matched, left = set(), [], []
        for i, row in enumerate(sheet_a.rows):
            if all(c.blank for c in row):
                continue
            k = key(sheet_a, row, "a")
            waiting = exact_b.get(k) if not blank_key(k) else None
            if waiting:
                j = waiting.popleft()
                used_b.add(j)
                matched.append((i, j))
            else:
                left.append((i, k))

        # Near matches among what is left: equal on every part but one, and that part filled on both sides.
        others_b = [defaultdict(list) for _ in keys] if len(keys) > 1 else []
        for j, k in keys_b.items():
            if j in used_b:
                continue
            for p, index in enumerate(others_b):
                rest = k[:p] + k[p + 1:]
                if not blank_key(rest) and k[p].strip():
                    index[rest].append(j)
        uncertain, only_a, blank_a = [], [], []
        for i, k in left:
            near = None
            if others_b:
                found = set()
                for p, index in enumerate(others_b):
                    rest = k[:p] + k[p + 1:]
                    if blank_key(rest) or not k[p].strip():
                        continue
                    for j in index.get(rest, ()):
                        if j not in used_b:
                            found.add((j, keys[p][0]))
                        if len(found) > 1:
                            break
                    if len(found) > 1:
                        break
                near = found.pop() if len(found) == 1 else None
            if near is not None:
                used_b.add(near[0])                       # held for this row alone: shown, never merged
                uncertain.append((i, near[0], near[1]))
            else:
                (blank_a if blank_key(k) else only_a).append(i)
        only_b = [j for j in keys_b if j not in used_b]
        return matched, uncertain, only_a, blank_a, only_b, blank_b
