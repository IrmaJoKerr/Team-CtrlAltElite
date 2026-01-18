#!/usr/bin/env python3
"""
Backfill script: link document chunks to SOP versions and create
`chunk_sop_mapping` records where missing.

Usage:
    python3 scripts/backfill_chunk_sop_mapping.py [--dry-run]

Prerequisites:
    - Database migrations applied (migrate_sop_version_status.sql)
    - Provide DB credentials via `DB_PASSWORD` env var (preferred).
"""
import os
import re
import sys
import hashlib
import logging
from typing import Optional, List, Tuple

import psycopg2
import json

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

DB_HOST = os.environ.get('DB_HOST')
DB_USER = os.environ.get('DB_USER', 'postgres')
DB_NAME = os.environ.get('DB_NAME', 'docintel_db')
PROJECT_ID = os.environ.get('PROJECT_ID')

from adapters.secrets_adapter import get_db_password


def get_connection(config=None):
    """Establish database connection.

    Accepts a `Config` object to determine cloud-mode and secret provider.
    """
    cloud_mode = getattr(config, 'cloud_mode', False) if config is not None else False
    password = get_db_password(cloud_mode=cloud_mode, config=config)
    return psycopg2.connect(
        host=DB_HOST,
        user=DB_USER,
        password=password,
        dbname=DB_NAME
    )


def extract_step_info(chunk_content: str) -> List[Tuple[int, str, bool]]:
    """
    Extract procedural steps from chunk text.
    Returns list of (step_index, step_text, is_explicit)
    """
    steps = []
    
    # Pattern 1: Numbered steps "1. Step text" or "1) Step text"
    numbered = re.findall(r'^(\d+)[.\)]\s+(.+?)(?=\n\d+[.\)]|\n\n|$)', chunk_content, re.MULTILINE | re.DOTALL)
    for num, text in numbered:
        steps.append((int(num), text.strip(), True))
    
    # Pattern 2: Lettered steps "a. Step" or "a) Step"
    lettered = re.findall(r'^([a-z])[.\)]\s+(.+?)(?=\n[a-z][.\)]|\n\n|$)', chunk_content, re.MULTILINE | re.DOTALL)
    for letter, text in lettered:
        idx = ord(letter) - ord('a') + 1
        steps.append((idx, text.strip(), True))
    
    # Pattern 3: Bullet points (implicit steps)
    if not steps:
        bullets = re.findall(r'^\s*[-•]\s+(.+?)(?=\n\s*[-•]|\n\n|$)', chunk_content, re.MULTILINE | re.DOTALL)
        for idx, text in enumerate(bullets, 1):
            steps.append((idx, text.strip(), False))  # is_explicit=False
    
    return steps


def detect_section_number(chunk_content: str) -> Optional[str]:
    """Extract section number like '3.1.2' from chunk."""
    # Common patterns: "3.1.2", "Section 3.1.2", "§3.1"
    match = re.search(r'(?:Section\s+|§)?(\d+(?:\.\d+)+)', chunk_content[:200])
    return match.group(1) if match else None


def backfill_mappings(conn, dry_run: bool = False):
    """
    Main backfill logic:
    1. Get all documents (chunks)
    2. Find or create SOP + version records
    3. Create chunk_sop_mapping entries
    """
    cursor = conn.cursor()
    
    # Get all document chunks
    cursor.execute("""
        SELECT id, original_gcs_filename, gcs_object_path, department_folder, 
               chunk_index, chunk_content, final_department, final_title
        FROM documents
        ORDER BY original_gcs_filename, chunk_index
    """)
    
    rows = cursor.fetchall()
    logging.info(f"Found {len(rows)} document chunks to process")
    
    # Group by original filename (each file = 1 SOP)
    sop_chunks = {}
    for row in rows:
        doc_id, orig_filename, gcs_path, dept_folder, chunk_idx, content, final_dept, final_title = row
        key = orig_filename or gcs_path
        if key not in sop_chunks:
            sop_chunks[key] = []
        sop_chunks[key].append({
            'chunk_id': doc_id,
            'chunk_index': chunk_idx,
            'content': content,
            'department': final_dept or dept_folder,
            'title': final_title,
            'gcs_path': gcs_path
        })
    
    logging.info(f"Grouped into {len(sop_chunks)} SOPs")
    
    created = 0
    skipped = 0
    
    for sop_filename, chunks in sop_chunks.items():
        # Find or create SOP record
        department = chunks[0]['department'] or 'General'
        title = chunks[0]['title'] or sop_filename
        
        cursor.execute("SELECT sop_id FROM sops WHERE department = %s LIMIT 1", (department,))
        sop_row = cursor.fetchone()

        if sop_row:
            sop_id = sop_row[0]
        else:
            if dry_run:
                logging.info(f"[DRY RUN] Would create SOP: {title} ({department})")
                continue
            # Insert a minimal SOP record. `sop_code` is optional and may be NULL.
            cursor.execute(
                "INSERT INTO sops (sop_code, title, department) VALUES (%s, %s, %s) RETURNING sop_id",
                (None, title, department)
            )
            sop_id = cursor.fetchone()[0]
            logging.info(f"Created SOP {sop_id}: {title}")
        
        # Find or create version
        # Attempt to find an existing version; fallback to creating a new basic version
        cursor.execute("SELECT version_id FROM sop_versions WHERE sop_id = %s ORDER BY version DESC LIMIT 1", (sop_id,))
        ver_row = cursor.fetchone()

        if ver_row and ver_row[0] is not None:
            version_id = ver_row[0]
        else:
            if dry_run:
                logging.info(f"[DRY RUN] Would create version for SOP {sop_id}")
                continue
            gcs_path = chunks[0]['gcs_path']
            # Insert a simple version record. `version` defaults to 1 when unknown.
            cursor.execute(
                "INSERT INTO sop_versions (sop_id, version, payload, created_by) VALUES (%s, %s, %s, %s) RETURNING id, version",
                (sop_id, 1, json.dumps({'gcs_path': gcs_path}), 'backfill-script')
            )
            rv = cursor.fetchone()
            # Try to set version_id compatibly: prefer returned id, else version
            version_id = rv[0] if rv else None
            logging.info(f"Created version {version_id} for SOP {sop_id}")

            # Activate this version if activate function exists
            try:
                if not dry_run:
                    cursor.execute("SELECT activate_sop_version(%s, %s, %s)", (sop_id, version_id, 'backfill-script'))
            except Exception:
                logging.debug('activate_sop_version not available or failed; continuing')
        
        # Create mappings for each chunk
        for chunk in chunks:
            # Check if mapping exists
            cursor.execute(
                "SELECT id FROM chunk_sop_mapping WHERE chunk_id = %s AND sop_version_id = %s",
                (chunk['chunk_id'], version_id)
            )
            if cursor.fetchone():
                skipped += 1
                continue
            
            # Extract step info
            steps = extract_step_info(chunk['content'] or '')
            section_num = detect_section_number(chunk['content'] or '')
            
            is_procedural = len(steps) > 0
            step_idx = steps[0][0] if steps else None
            step_text = steps[0][1] if steps else None
            
            if dry_run:
                logging.info(f"[DRY RUN] Would map chunk {chunk['chunk_id']} -> SOP {sop_id} v{version_id}")
            else:
                cursor.execute("""
                    INSERT INTO chunk_sop_mapping 
                    (chunk_id, sop_id, sop_version_id, section_number, step_index, 
                     is_procedural_step, step_text, chunk_order)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    chunk['chunk_id'], sop_id, version_id, section_num,
                    step_idx, is_procedural, step_text, chunk['chunk_index']
                ))
                created += 1
    
    if not dry_run:
        conn.commit()
    
    cursor.close()
    logging.info(f"Backfill complete: {created} mappings created, {skipped} skipped (already exist)")


def main():
    from utils.cli import build_parser, get_effective_config

    parser = build_parser()
    parser.add_argument('--dry-run', action='store_true')
    ns = parser.parse_args()

    cfg = get_effective_config()
    cloud_mode = getattr(cfg, 'cloud_mode', False)
    dry_run = ns.dry_run or getattr(cfg, 'simulate', True)

    if dry_run:
        logging.info("DRY RUN MODE - no changes will be made")

    try:
        conn = get_connection(cloud_mode=cloud_mode)
        backfill_mappings(conn, dry_run=dry_run)
        conn.close()
    except Exception as e:
        logging.error(f"Backfill failed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
