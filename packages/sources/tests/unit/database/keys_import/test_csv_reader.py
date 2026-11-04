# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Keys import: reading the CSV file (work/add-bulk-import.txt, section 2.3).

Expected results are tracked in work/add-bulk-import.txt, section 3 (R1-R17).
Line numbers are file line numbers: the header is line 1, so the first data
row is line 2 (what a steward sees in a spreadsheet).
"""

from __future__ import annotations

import pytest
from coa_sources.database.keys_import.csv_reader import (
    MAX_ROWS,
    CsvFormatError,
    KeyRow,
    parse_keys_csv,
)

pytestmark = pytest.mark.unit

HEADER = "type,source_table,column,target_table,target_column\n"


def _messages(result) -> dict[int, str]:
    return {e.line: e.message for e in result.errors}


class TestValidRows:
    def test_r1_pk_and_fk_rows_parsed_with_file_line_numbers(self) -> None:
        result = parse_keys_csv(
            HEADER + "PK,purchase_order,po_number,,\nFK,purchase_order,supplier_id,supplier,supplier_id\n"
        )
        assert result.errors == []
        assert result.rows == [
            KeyRow(line=2, type="PK", source_table="purchase_order", columns=("po_number",)),
            KeyRow(
                line=3,
                type="FK",
                source_table="purchase_order",
                columns=("supplier_id",),
                target_table="supplier",
                target_column="supplier_id",
            ),
        ]

    def test_r2_composite_primary_key_split_on_pipe_and_trimmed(self) -> None:
        result = parse_keys_csv(HEADER + "PK,purchase_order_line, po_number | line_number ,,\n")
        assert result.rows[0].columns == ("po_number", "line_number")

    def test_r3_type_is_case_insensitive(self) -> None:
        result = parse_keys_csv(HEADER + "pk,t,a,,\nFk,t,b,u,c\n")
        assert [r.type for r in result.rows] == ["PK", "FK"]

    def test_r4_header_any_case_any_order_extra_columns_ignored(self) -> None:
        text = " Target_Column ,NOTES,Type,COLUMN,source_table,target_table\nc,ignore me,FK,b,t,u\n"
        result = parse_keys_csv(text)
        assert result.errors == []
        assert result.rows == [
            KeyRow(line=2, type="FK", source_table="t", columns=("b",), target_table="u", target_column="c")
        ]

    def test_r5_utf8_bom_from_excel_is_ignored(self) -> None:
        result = parse_keys_csv("﻿" + HEADER + "PK,t,a,,\n")
        assert result.errors == []
        assert len(result.rows) == 1

    def test_r6_blank_lines_skipped_and_line_numbers_stay_true(self) -> None:
        result = parse_keys_csv(HEADER + "PK,t,a,,\n\n,,,,\nPK,u,b,,\n")
        assert [r.line for r in result.rows] == [2, 5]

    def test_r16_quoted_values_with_commas_are_read_correctly(self) -> None:
        result = parse_keys_csv(HEADER + 'FK,"t",b,"u",c\n')
        assert result.rows[0].target_table == "u"

    def test_r1_target_fields_empty_for_pk(self) -> None:
        result = parse_keys_csv(HEADER + "PK,t,a,,\n")
        assert result.rows[0].target_table == ""
        assert result.rows[0].target_column == ""


class TestFileErrors:
    def test_r7_missing_required_header_column_names_it(self) -> None:
        with pytest.raises(CsvFormatError, match="target_column"):
            parse_keys_csv("type,source_table,column,target_table\nPK,t,a,\n")

    @pytest.mark.parametrize("text", ["", "   \n", "﻿"])
    def test_r8_empty_file_is_rejected(self, text: str) -> None:
        with pytest.raises(CsvFormatError, match="empty"):
            parse_keys_csv(text)

    def test_r8_header_only_is_rejected(self) -> None:
        with pytest.raises(CsvFormatError, match="no key rows"):
            parse_keys_csv(HEADER)

    def test_r14_more_than_max_rows_is_rejected(self) -> None:
        body = "PK,t,a,,\n" * (MAX_ROWS + 1)
        with pytest.raises(CsvFormatError, match=str(MAX_ROWS)):
            parse_keys_csv(HEADER + body)

    def test_r14_exactly_max_rows_is_accepted(self) -> None:
        result = parse_keys_csv(HEADER + "PK,t,a,,\n" * MAX_ROWS)
        assert len(result.rows) == MAX_ROWS


class TestRowErrors:
    def test_r9_unknown_type(self) -> None:
        result = parse_keys_csv(HEADER + "UK,t,a,,\n")
        assert result.rows == []
        assert "PK or FK" in _messages(result)[2]

    @pytest.mark.parametrize("row", ["PK,,a,,", "PK,t,,,", "FK,,a,u,c", "FK,t,,u,c"])
    def test_r10_missing_table_or_column(self, row: str) -> None:
        result = parse_keys_csv(HEADER + row + "\n")
        assert result.rows == []
        assert 2 in _messages(result)

    @pytest.mark.parametrize("row", ["FK,t,a,,c", "FK,t,a,u,"])
    def test_r11_fk_needs_target_table_and_target_column(self, row: str) -> None:
        result = parse_keys_csv(HEADER + row + "\n")
        assert result.rows == []
        assert "target" in _messages(result)[2]

    def test_r12_fk_with_several_columns_is_rejected(self) -> None:
        result = parse_keys_csv(HEADER + "FK,t,a|b,u,c|d\n")
        assert result.rows == []
        assert "single-column" in _messages(result)[2]

    def test_r13_pk_with_target_fields_is_rejected(self) -> None:
        result = parse_keys_csv(HEADER + "PK,t,a,u,c\n")
        assert result.rows == []
        assert "PK" in _messages(result)[2]

    def test_r15_empty_part_in_composite_key_is_rejected(self) -> None:
        result = parse_keys_csv(HEADER + "PK,t,a||b,,\n")
        assert result.rows == []
        assert 2 in _messages(result)

    def test_r17_bad_rows_do_not_stop_good_rows(self) -> None:
        result = parse_keys_csv(HEADER + "XX,t,a,,\nPK,t,a,,\n")
        assert [r.line for r in result.rows] == [3]
        assert list(_messages(result)) == [2]
