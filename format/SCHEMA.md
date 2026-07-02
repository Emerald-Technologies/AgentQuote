# Canonical BOM Schema — Quoting Module Input

This document defines the target format the quoting module expects.
The BOM Processor Agent normalizes all customer BOMs to this schema.

Reference file: `format/finalformat.xlsx`

## Columns

| # | Column Name | Type | Required | Description |
|---|---|---|---|---|
| 1 | `line_number` | int | Yes | Sequential line number starting at 1 |
| 2 | `item_number` | str | Yes | Customer's internal part/item identifier |
| 3 | `item_name` | str | Yes | Human-readable description of the item |
| 4 | `revision` | str | No | Revision letter/number (e.g. A, B, 01) |
| 5 | `item_category` | str | No | Classification: Part, Document, Assembly, PCB, etc. |
| 6 | `unit_of_measure` | str | No | EA (each), FT (feet), IN (inches), LB (pounds), etc. |
| 7 | `quantity` | float | Yes | Per-assembly quantity (can be fractional for bulk items like solder) |
| 8 | `reference_designator` | str | No | PCB reference designators (e.g. "R1,R2,R5" or "C1-C10") |
| 9 | `bom_notes` | str | No | Free-text notes: RoHS status, alternate sourcing, production flags |
| 10 | `manufacturer_1` | str | No | Primary manufacturer name |
| 11 | `manufacturer_item_number_1` | str | No | Primary manufacturer part number (MPN) |
| 12 | `manufacturer_2` | str | No | Alternate manufacturer 1 |
| 13 | `manufacturer_item_number_2` | str | No | Alternate MPN 1 |
| 14 | `manufacturer_3` | str | No | Alternate manufacturer 2 |
| 15 | `manufacturer_item_number_3` | str | No | Alternate MPN 2 |
| 16 | `manufacturer_4` | str | No | Alternate manufacturer 3 |
| 17 | `manufacturer_item_number_4` | str | No | Alternate MPN 3 |
| 18 | `manufacturer_5` | str | No | Alternate manufacturer 4 |
| 19 | `manufacturer_item_number_5` | str | No | Alternate MPN 4 |
| 20 | `manufacturer_6` | str | No | Alternate manufacturer 5 |
| 21 | `manufacturer_item_number_6` | str | No | Alternate MPN 5 |
| 22 | `manufacturer_7` | str | No | Alternate manufacturer 6 |
| 23 | `manufacturer_item_number_7` | str | No | Alternate MPN 6 |
| 24 | `manufacturer_8` | str | No | Alternate manufacturer 7 |
| 25 | `manufacturer_item_number_8` | str | No | Alternate MPN 7 |

## Data Type Notes

- `line_number`: Integer, must be sequential and unique within a BOM
- `quantity`: Float — supports fractional values (e.g. 0.04 for bulk solder by weight)
- All string fields: UTF-8, no leading/trailing whitespace after normalization
- Empty cells should be `NaN`/`None` in the output, not empty strings
- `manufacturer_N` and `manufacturer_item_number_N` come in pairs: if manufacturer_N is populated, its corresponding MPN should also be populated

## Example Row

| line_number | item_number | item_name | revision | item_category | unit_of_measure | quantity | reference_designator | bom_notes | manufacturer_1 | manufacturer_item_number_1 |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 29172-001 | CCA,WHT,389 PWM,5V,FULL,3-LINE | D | Part | EA | 0.00 | | Production | | |
| 2 | 27949-001 | PWB,389 PWM-IN,5-PIN,5V,FULL | B | Part | EA | 0.04 | | Production | | |