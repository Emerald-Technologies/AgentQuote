#!/usr/bin/env python3
"""
Astronics BOM Parser for Emerald Technologies.

This parser handles Astronics Customer RFQ BOMs in .xls or .xlsx format.

Format Analysis:
- Files contain two sheets: "FOR UPLOAD" (primary data) and "q_BOM Export to Vendor Quote" (fuller data)
- Header is always at row 0 in both sheets
- Column mapping (0-indexed):
    [0] = Customer ID
    [1] = Assembly #
    [2] = Quote #
    [3] = Ascentron #        -> item_number
    [4] = Line #              -> line_number
    [5] = Description         -> item_name
    [6] = MFG                 -> manufacturer name
    [7] = MFG #               -> manufacturer item number
    [8] = U/M                 -> unit_of_measure
    [9] = Qty Per             -> quantity
    [24] = Comments           -> bom_notes
- Column count varies (21-25) across BOMs depending on which pricing columns are present
- Multiple rows with the same Ascentron # represent manufacturer cross-references (alternates)
- Document items (PCB, Labels, Adhesives, Conformal Coat, Filament) are identified by
  description keywords and set to item_category='Document'
- All other items are 'Part'

Strategy:
- Use "q_BOM Export to Vendor Quote" sheet as primary (more complete data)
- Fall back to "FOR UPLOAD" if the other sheet doesn't exist
- If neither sheet exists (e.g. format reference files), write empty output
- Auto-detect Excel engine based on file extension (.xls -> xlrd, .xlsx -> openpyxl)
- Group rows by Ascentron # (item_number) to collapse manufacturer alternates
"""

import argparse
import logging
import os
import sys
from collections import OrderedDict

import pandas as pd

# Document keywords in descriptions (case-insensitive matching)
DOCUMENT_KEYWORDS = [
    'PCB', 'DWG', 'SPEC', 'MANUAL', 'LABEL', 'LBL', 'ARTWORK',
    'STENCIL', 'FILAMENT', 'ADHS', 'RTV', 'CONFORMAL COAT',
    'HYSOL', 'EPOXY'
]

# Column name mapping (normalized to find columns by name)
COLUMN_MAP = {
    'Customer ID': None,   # skip
    'Assembly #': None,    # skip
    'Quote #': None,       # skip
    'Ascentron #': 'item_number',
    'Line #': 'line_number',
    'Description': 'item_name',
    'MFG': 'manufacturer',
    'MFG #': 'manufacturer_item_number',
    'U/M': 'unit_of_measure',
    'Qty Per': 'quantity',
    'Comments': 'bom_notes',
}

# Target column schema
TARGET_COLUMNS = [
    'line_number', 'item_number', 'item_name', 'revision', 'item_category',
    'unit_of_measure', 'quantity', 'reference_designator', 'bom_notes',
    'manufacturer_1', 'manufacturer_item_number_1',
    'manufacturer_2', 'manufacturer_item_number_2',
    'manufacturer_3', 'manufacturer_item_number_3',
    'manufacturer_4', 'manufacturer_item_number_4',
    'manufacturer_5', 'manufacturer_item_number_5',
    'manufacturer_6', 'manufacturer_item_number_6',
    'manufacturer_7', 'manufacturer_item_number_7',
    'manufacturer_8', 'manufacturer_item_number_8',
]


def get_engine(filepath):
    """Determine the Excel engine based on file extension."""
    ext = os.path.splitext(filepath)[1].lower()
    if ext == '.xlsx':
        return 'openpyxl'
    else:
        return 'xlrd'


def normalize_header(header_row):
    """Map header column names to indices."""
    col_indices = {}
    for idx, val in enumerate(header_row):
        if pd.notna(val):
            header_str = str(val).strip()
            if header_str in COLUMN_MAP:
                col_indices[header_str] = idx
    return col_indices


def is_document_item(description):
    """Determine if an item should be classified as Document."""
    if pd.isna(description):
        return False
    desc_upper = str(description).upper().strip()
    for kw in DOCUMENT_KEYWORDS:
        if kw in desc_upper:
            return True
    return False


def write_empty_output(output_path):
    """Write an empty output file with correct column headers."""
    output_df = pd.DataFrame(columns=TARGET_COLUMNS)
    output_df.to_excel(output_path, index=False)


def parse_astronics_bom(input_path, output_path):
    """
    Parse an Astronics BOM file and output standardized format.

    Returns 0 on success (including gracefully skipping non-Astronics files),
    1 on fatal errors.
    """
    logger = logging.getLogger('astronics_parser')
    logger.info(f"Reading BOM from: {input_path}")

    engine = get_engine(input_path)
    logger.info(f"Using engine: {engine}")

    # Read the Excel file
    try:
        xls = pd.ExcelFile(input_path, engine=engine)
    except Exception as e:
        logger.warning(f"Cannot open Excel file: {e}")
        write_empty_output(output_path)
        return 0

    # Try to find known Astronics sheet names
    sheet_name = None
    for preferred in ['q_BOM Export to Vendor Quote', 'FOR UPLOAD']:
        if preferred in xls.sheet_names:
            sheet_name = preferred
            break

    if sheet_name is None:
        logger.warning(f"No Astronics sheet found (sheets: {xls.sheet_names}), "
                       f"writing empty output")
        write_empty_output(output_path)
        return 0

    logger.info(f"Using sheet: {sheet_name}")

    # Read all rows as raw data (no header)
    raw_df = pd.read_excel(input_path, sheet_name=sheet_name, header=None, engine=engine)
    logger.info(f"Raw data shape: {raw_df.shape}")

    if len(raw_df) < 2:
        logger.warning("No data rows found (only header or empty)")
        write_empty_output(output_path)
        return 0

    # The first row is the header
    header_row = raw_df.iloc[0].tolist()
    col_indices = normalize_header(header_row)

    logger.info(f"Found columns: {list(col_indices.keys())}")

    # Check required columns
    required_cols = ['Ascentron #', 'Line #', 'Description', 'MFG', 'MFG #', 'Qty Per']
    missing = [c for c in required_cols if c not in col_indices]
    if missing:
        logger.warning(f"Missing required Astronics columns ({missing}), "
                       f"not an Astronics BOM. Writing empty output.")
        write_empty_output(output_path)
        return 0

    # Extract data rows (starting from row 1)
    data_rows = []
    for i in range(1, len(raw_df)):
        row = raw_df.iloc[i]

        # Skip completely empty rows
        if all(pd.isna(v) for v in row):
            continue

        # Skip rows with no Ascentron # (item number)
        ascentron = row[col_indices['Ascentron #']]
        if pd.isna(ascentron) or str(ascentron).strip() == '':
            continue

        # Skip footer rows that might have summary text
        ascentron_str = str(ascentron).strip()
        if ascentron_str.upper() in ['TOTAL:', 'TOTAL', 'NOTES:', 'NOTES']:
            continue

        line_val = row[col_indices['Line #']]
        desc = row[col_indices['Description']]
        mfg = row[col_indices['MFG']]
        mfg_num = row[col_indices['MFG #']]
        qty = row[col_indices['Qty Per']]

        # Get unit of measure
        uom_idx = col_indices.get('U/M')
        uom = str(row[uom_idx]).strip() if uom_idx is not None and pd.notna(row[uom_idx]) else ''

        # Get comments
        comments_idx = col_indices.get('Comments')
        comments = str(row[comments_idx]).strip() if comments_idx is not None and pd.notna(row[comments_idx]) else ''

        # Convert line number to int if possible
        try:
            line_num = int(float(str(line_val).strip()))
        except (ValueError, TypeError):
            line_num = line_val
            logger.warning(f"Could not parse line number: {line_val}, using as-is")

        # Convert quantity to float
        try:
            quantity = float(str(qty).strip())
        except (ValueError, TypeError):
            quantity = 0.0
            logger.warning(f"Could not parse quantity: {qty}, using 0.0")

        data_rows.append({
            'item_number': str(ascentron).strip(),
            'line_number': line_num,
            'item_name': str(desc).strip() if pd.notna(desc) else '',
            'unit_of_measure': uom,
            'quantity': quantity,
            'bom_notes': comments,
            'manufacturer': str(mfg).strip() if pd.notna(mfg) else '',
            'manufacturer_item_number': str(mfg_num).strip() if pd.notna(mfg_num) else '',
        })

    logger.info(f"Parsed {len(data_rows)} raw rows")

    if not data_rows:
        logger.warning("No data rows found in the BOM!")
        write_empty_output(output_path)
        return 0

    # Group by item_number to collapse manufacturer alternates
    grouped = OrderedDict()
    for row in data_rows:
        key = row['item_number']
        if key not in grouped:
            grouped[key] = {
                'line_number': row['line_number'],
                'item_name': row['item_name'],
                'unit_of_measure': row['unit_of_measure'],
                'quantity': row['quantity'],
                'bom_notes': row['bom_notes'],
                'manufacturers': [],
            }
        # Add manufacturer if present (avoid duplicates)
        if row['manufacturer'] and row['manufacturer_item_number']:
            mfr_entry = (row['manufacturer'], row['manufacturer_item_number'])
            if mfr_entry not in grouped[key]['manufacturers']:
                grouped[key]['manufacturers'].append(mfr_entry)

    logger.info(f"Collapsed into {len(grouped)} unique items")

    # Build output rows
    output_rows = []
    for item_number, item_data in grouped.items():
        # Determine item_category based on description keywords
        if is_document_item(item_data['item_name']):
            item_category = 'Document'
        else:
            item_category = 'Part'

        # Use 'EA' as default unit of measure if empty
        uom = item_data['unit_of_measure'] if item_data['unit_of_measure'] else 'EA'

        row_out = {
            'line_number': item_data['line_number'],
            'item_number': item_number,
            'item_name': item_data['item_name'],
            'revision': '',
            'item_category': item_category,
            'unit_of_measure': uom,
            'quantity': item_data['quantity'],
            'reference_designator': '',
            'bom_notes': item_data['bom_notes'],
        }

        # Add up to 8 manufacturer fields
        mfrs = item_data['manufacturers']
        for idx in range(8):
            if idx < len(mfrs):
                row_out[f'manufacturer_{idx + 1}'] = mfrs[idx][0]
                row_out[f'manufacturer_item_number_{idx + 1}'] = mfrs[idx][1]
            else:
                row_out[f'manufacturer_{idx + 1}'] = ''
                row_out[f'manufacturer_item_number_{idx + 1}'] = ''

        if len(mfrs) > 8:
            logger.warning(f"Item {item_number} has {len(mfrs)} manufacturer alternates, "
                           f"only first 8 included")

        output_rows.append(row_out)

    # Create DataFrame with canonical column order
    output_df = pd.DataFrame(output_rows)

    # Ensure all required columns exist
    for col in TARGET_COLUMNS:
        if col not in output_df.columns:
            output_df[col] = ''

    # Reorder columns to match canonical schema
    output_df = output_df[TARGET_COLUMNS]

    # Sort by line_number
    output_df = output_df.sort_values('line_number').reset_index(drop=True)

    # Write output
    output_df.to_excel(output_path, index=False)
    logger.info(f"Output written to: {output_path} ({len(output_rows)} rows)")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description='Parse Astronics BOM (.xls/.xlsx) and output standardized quoting format.'
    )
    parser.add_argument('--input', required=True,
                        help='Path to input Astronics BOM file (.xls or .xlsx)')
    parser.add_argument('--output', required=True,
                        help='Path to output standardized .xlsx file')

    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(levelname)s: %(message)s',
        stream=sys.stderr,
    )

    exit_code = parse_astronics_bom(args.input, args.output)
    sys.exit(exit_code)


if __name__ == '__main__':
    main()