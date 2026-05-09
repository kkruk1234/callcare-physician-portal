from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import psycopg
import requests
from psycopg.rows import dict_row

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from app.portal.portal_common import (
    add_signed_addendum,
    addendum_block,
    encounter_when,
    html_escape,
    load_json,
    load_meta,
    packet_bundle,
    packet_path,
    patient_groups,
    queue_or_send_new_note_email,
    save_json,
    save_meta,
    safe_str,
    save_note_signed,
    signed_note_text,
)

app = FastAPI(title="CallCare Physician Portal")

CALLCARE_PUBLIC_BASE_URL = os.getenv("CALLCARE_PUBLIC_BASE_URL", "https://callcare.healthcare").rstrip("/")
CALLCARE_SHARED_DATABASE_URL = os.getenv("CALLCARE_SHARED_DATABASE_URL", "").strip()
CALLCARE_EMAIL_PROVIDER = os.getenv("CALLCARE_EMAIL_PROVIDER", "").strip().lower()
CALLCARE_RESEND_API_KEY = os.getenv("CALLCARE_RESEND_API_KEY", "").strip()
CALLCARE_PHYSICIAN_USERNAME = os.getenv("CALLCARE_PHYSICIAN_USERNAME", "").strip()
CALLCARE_PHYSICIAN_PASSWORD = os.getenv("CALLCARE_PHYSICIAN_PASSWORD", "").strip()

SESSIONS: Dict[str, Dict[str, str]] = {}

COMMON_HISTORY_CONDITIONS = [
    "Hypertension", "Diabetes", "High Cholesterol", "Coronary Artery Disease",
    "Heart Failure", "Atrial Fibrillation", "Stroke", "COPD", "Asthma",
    "Sleep Apnea", "GERD", "Peptic Ulcer Disease", "Irritable Bowel Syndrome",
    "Crohn Disease", "Ulcerative Colitis", "Chronic Kidney Disease",
    "Kidney Stones", "Migraines", "Seizure Disorder", "Depression", "Anxiety",
    "Bipolar Disorder", "PTSD", "ADHD", "Hypothyroidism", "Hyperthyroidism",
    "Obesity", "Osteoarthritis", "Rheumatoid Arthritis", "Fibromyalgia",
    "Osteoporosis", "Chronic Back Pain", "Anemia", "Cancer", "Breast Cancer",
    "Colon Cancer", "Prostate Cancer", "Skin Cancer", "Liver Disease",
    "Hepatitis", "HIV", "Peripheral Neuropathy", "Dementia", "Parkinson Disease",
    "Glaucoma", "Macular Degeneration", "Seasonal Allergies", "Eczema",
    "Psoriasis", "Gout",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def shared_db_url() -> str:
    return CALLCARE_SHARED_DATABASE_URL


def db_connect():
    url = shared_db_url()
    if not url:
        raise HTTPException(status_code=500, detail="Missing CALLCARE_SHARED_DATABASE_URL")
    return psycopg.connect(url, row_factory=dict_row)


def make_session_token() -> str:
    return secrets.token_urlsafe(32)


def current_session(request: Request) -> Optional[Dict[str, str]]:
    token = request.cookies.get("callcare_physician_session", "")
    if not token:
        return None
    return SESSIONS.get(token)


def require_session(request: Request) -> Dict[str, str]:
    sess = current_session(request)
    if not sess:
        raise HTTPException(status_code=401, detail="Not logged in")
    return sess


def shell(title: str, body: str) -> str:
    return f"""
    <html>
      <head>
        <title>{html_escape(title)}</title>
        <style>
          :root {{
            --bg: #f3f8f7;
            --card: #ffffff;
            --ink: #163133;
            --muted: #5d7476;
            --line: #dbe7e5;
            --accent: #1d8f8a;
            --accent2: #6cb5b0;
          }}
          * {{ box-sizing: border-box; }}
          body {{
            margin: 0;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
            color: var(--ink);
            background:
              radial-gradient(circle at top left, rgba(108,181,176,0.18), transparent 28%),
              linear-gradient(180deg, #f6fbfb 0%, #eef7f6 100%);
          }}
          .wrap {{ max-width: 1400px; margin: 0 auto; padding: 28px; }}
          .hero {{
            background: linear-gradient(135deg, rgba(29,143,138,0.95), rgba(108,181,176,0.92));
            color: white;
            border-radius: 28px;
            padding: 28px 32px;
            box-shadow: 0 18px 45px rgba(19, 56, 58, 0.12);
            margin-bottom: 22px;
          }}
          .hero h1 {{ margin: 0 0 8px 0; font-size: 34px; }}
          .hero p {{ margin: 0; opacity: 0.95; font-size: 16px; }}
          .grid {{ display: grid; gap: 22px; }}
          .card {{
            background: var(--card);
            border: 1px solid var(--line);
            border-radius: 24px;
            padding: 22px;
            box-shadow: 0 10px 28px rgba(18, 40, 42, 0.06);
          }}
          table {{ width: 100%; border-collapse: collapse; }}
          th, td {{ padding: 12px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
          th {{ color: var(--muted); font-weight: 700; font-size: 13px; text-transform: uppercase; letter-spacing: 0.04em; }}
          tr:last-child td {{ border-bottom: 0; }}
          a {{ color: var(--accent); text-decoration: none; }}
          .tabs {{ display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 18px; }}
          .tab {{
            display: inline-block;
            background: #eef8f7;
            border: 1px solid var(--line);
            border-radius: 999px;
            padding: 10px 16px;
            color: var(--ink);
            font-weight: 700;
          }}
          .tab.active {{
            background: linear-gradient(135deg, var(--accent), var(--accent2));
            color: white;
            border-color: transparent;
          }}
          .layout {{ display: grid; grid-template-columns: 320px 1fr; gap: 22px; }}
          .sidebar ul {{ list-style: none; margin: 0; padding: 0; }}
          .sidebar li {{ margin: 0 0 10px 0; }}
          .enc-link {{
            display: block;
            padding: 14px 16px;
            border-radius: 18px;
            background: #f7fbfb;
            border: 1px solid var(--line);
          }}
          .enc-link.active {{
            background: linear-gradient(135deg, rgba(29,143,138,0.12), rgba(108,181,176,0.12));
            border-color: rgba(29,143,138,0.3);
            font-weight: 700;
          }}
          .section-title {{ margin: 0 0 14px 0; font-size: 22px; }}
          .meta-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 14px;
            margin-bottom: 20px;
          }}
          .metric {{
            background: #f8fcfc;
            border: 1px solid var(--line);
            border-radius: 18px;
            padding: 14px 16px;
          }}
          .metric .label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; }}
          .metric .value {{ margin-top: 6px; font-size: 16px; font-weight: 600; }}
          label {{
            display: block;
            margin-bottom: 6px;
            font-weight: 700;
            color: var(--ink);
          }}
          input, select {{
            width: 100%;
            padding: 12px;
            border-radius: 12px;
            border: 1px solid var(--line);
            background: white;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
            font-size: 15px;
          }}
          input[type="checkbox"] {{
            width: 18px;
            height: 18px;
            accent-color: #000000;
          }}
          textarea {{
            width: 100%;
            border: 1px solid var(--line);
            border-radius: 18px;
            padding: 14px;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
            font-size: 15px;
            background: #fbfdfd;
          }}
          .note-textarea {{
            min-height: 300px;
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
            font-size: 14px;
          }}
          .readonly {{
            border: 1px solid var(--line);
            background: #fbfdfd;
            border-radius: 18px;
            padding: 16px;
            white-space: pre-wrap;
          }}
          .btnbar {{ display: flex; gap: 12px; flex-wrap: wrap; margin-top: 16px; }}
          button {{
            border: 0;
            background: linear-gradient(135deg, var(--accent), var(--accent2));
            color: white;
            padding: 12px 16px;
            border-radius: 14px;
            font-weight: 700;
            cursor: pointer;
            box-shadow: 0 8px 18px rgba(29,143,138,0.18);
          }}
          .btn-soft {{
            background: #eef8f7;
            color: var(--ink);
            border: 1px solid var(--line);
            box-shadow: none;
          }}
          .pill {{
            display: inline-block;
            padding: 8px 12px;
            border-radius: 999px;
            background: #eef8f7;
            border: 1px solid var(--line);
            font-size: 13px;
          }}
          @media (max-width: 980px) {{
            .layout {{ grid-template-columns: 1fr; }}
          }}
        </style>
      </head>
      <body>
        <div class="wrap">
          {body}
        </div>
      </body>
    </html>
    """


def selected_attr(value: str, selected: Any) -> str:
    return "selected" if safe_str(value).lower() == safe_str(selected).lower() else ""


def checkbox_attr(value: Any) -> str:
    return "checked" if bool(value) else ""


def audit_event(cur, patient_id: str, event_type: str, event_json: Dict[str, Any]) -> None:
    cur.execute(
        """
        INSERT INTO callcare.audit_events (
          id, actor_type, actor_id, patient_id, encounter_id, event_type, event_json, created_at
        )
        VALUES (
          gen_random_uuid(), 'physician', NULL, %s::uuid, NULL, %s, %s::jsonb, now()
        )
        """,
        (patient_id, event_type, json.dumps(event_json)),
    )


def patient_id_for_chart(cur, chart_number: str) -> str:
    cur.execute(
        """
        SELECT id::text AS patient_id
        FROM callcare.patients
        WHERE chart_number = %s
          AND archived_at IS NULL
        LIMIT 1
        """,
        (chart_number,),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Patient not found")
    return row["patient_id"]


def _extract_differential_title(note_text: str, fallback: str) -> str:
    text = safe_str(note_text)
    lower_text = text.lower()
    marker = "the working diagnosis is "
    likely_marker = " likely"
    start = lower_text.find(marker)
    if start != -1:
        start += len(marker)
        end = lower_text.find(likely_marker, start)
        if end != -1:
            diagnosis = text[start:end].strip(" .:-")
            if diagnosis:
                return diagnosis[:1].upper() + diagnosis[1:]

    diff_marker = "Differential:"
    if diff_marker in text:
        tail = text.split(diff_marker, 1)[1]
        for line in tail.splitlines():
            line = safe_str(line)
            if line.startswith("1."):
                line = line[2:].strip()
                if line:
                    return "Possible " + line[:1].lower() + line[1:]

    f = safe_str(fallback).strip().rstrip(".")
    for prefix in ("i have ", "i'm having ", "im having ", "i am having ", "my "):
        if f.lower().startswith(prefix):
            f = f[len(prefix):].strip()
            break
    return f[:1].upper() + f[1:] if f else "Encounter"


def _shared_lookup_patient_id(chart_number: str) -> Optional[str]:
    if not shared_db_url() or not chart_number:
        return None
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id::text AS patient_id FROM callcare.patients WHERE chart_number = %s LIMIT 1",
                (chart_number,),
            )
            row = cur.fetchone()
            return row["patient_id"] if row else None


def _lookup_shared_patient_by_call_sid(call_sid: str) -> Optional[Dict[str, Any]]:
    if not shared_db_url() or not call_sid:
        return None
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.id::text AS patient_id, p.chart_number, e.chief_complaint
                FROM callcare.encounters e
                JOIN callcare.patients p ON p.id = e.patient_id
                WHERE e.call_sid = %s
                ORDER BY e.started_at DESC
                LIMIT 1
                """,
                (call_sid,),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def _sync_bundle_to_shared_db(bundle: dict) -> None:
    if not shared_db_url() or not bundle:
        return

    packet = bundle.get("packet") or {}
    meta = bundle.get("meta") or {}
    patient_ctx = bundle.get("patient_ctx") or {}

    packet_id = safe_str(bundle.get("packet_id") or packet.get("packet_id"))
    if not packet_id:
        return

    chart_number = safe_str(patient_ctx.get("chart_number"))
    chief_complaint = safe_str(patient_ctx.get("chief_complaint"))
    patient_id = safe_str(patient_ctx.get("patient_id"))
    call_sid = safe_str(bundle.get("call_sid"))

    if not call_sid:
        try:
            call_sid = safe_str(load_meta(packet_id).get("call_sid"))
        except Exception:
            pass

    if chart_number and not patient_id:
        patient_id = _shared_lookup_patient_id(chart_number) or ""

    if (not chart_number or not patient_id or not chief_complaint) and call_sid:
        linked = _lookup_shared_patient_by_call_sid(call_sid)
        if linked:
            chart_number = chart_number or safe_str(linked.get("chart_number"))
            patient_id = patient_id or safe_str(linked.get("patient_id"))
            chief_complaint = chief_complaint or safe_str(linked.get("chief_complaint"))

    if not chart_number or not patient_id:
        return

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO callcare.portal_packets (
                    packet_id, chart_number, patient_id, call_sid, session_id, pathway_id,
                    created_at, chief_complaint, note_text, spoken_summary,
                    spoken_summary_comments, status, prescription_status, note_sent,
                    signed, signed_at, signed_by, addenda, updated_at
                )
                VALUES (
                    %s, %s, %s::uuid, NULLIF(%s, ''), NULLIF(%s, ''), NULLIF(%s, ''),
                    NULLIF(%s, '')::timestamptz, NULLIF(%s, ''), NULLIF(%s, ''),
                    COALESCE(NULLIF(%s, ''), ''), COALESCE(NULLIF(%s, ''), ''),
                    COALESCE(NULLIF(%s, ''), 'active'),
                    COALESCE(NULLIF(%s, ''), 'under review'),
                    COALESCE(NULLIF(%s, ''), 'to be mailed'),
                    %s, NULLIF(%s, '')::timestamptz, COALESCE(NULLIF(%s, ''), ''),
                    %s::jsonb, now()
                )
                ON CONFLICT (packet_id) DO UPDATE
                SET chart_number = EXCLUDED.chart_number,
                    patient_id = EXCLUDED.patient_id,
                    call_sid = EXCLUDED.call_sid,
                    session_id = EXCLUDED.session_id,
                    pathway_id = EXCLUDED.pathway_id,
                    created_at = EXCLUDED.created_at,
                    chief_complaint = EXCLUDED.chief_complaint,
                    note_text = EXCLUDED.note_text,
                    spoken_summary = EXCLUDED.spoken_summary,
                    spoken_summary_comments = EXCLUDED.spoken_summary_comments,
                    status = EXCLUDED.status,
                    prescription_status = EXCLUDED.prescription_status,
                    note_sent = EXCLUDED.note_sent,
                    signed = EXCLUDED.signed,
                    signed_at = EXCLUDED.signed_at,
                    signed_by = EXCLUDED.signed_by,
                    addenda = EXCLUDED.addenda,
                    updated_at = now()
                """,
                (
                    packet_id,
                    chart_number,
                    patient_id,
                    call_sid,
                    safe_str(packet.get("session_id")),
                    safe_str(packet.get("pathway_id")),
                    safe_str(packet.get("created_at")),
                    chief_complaint,
                    safe_str(packet.get("note_text")),
                    safe_str(bundle.get("spoken_summary")),
                    safe_str(meta.get("spoken_summary_comments")),
                    safe_str(meta.get("status")),
                    safe_str(meta.get("prescription_status")),
                    safe_str(meta.get("note_sent")),
                    bool(meta.get("signed")),
                    safe_str(meta.get("signed_at")),
                    safe_str(meta.get("signed_by")),
                    json.dumps(meta.get("addenda") or []),
                ),
            )
        conn.commit()


def _queue_or_send_new_note_email_resend(packet_id: str, patient_ctx: dict, reason: str = "note_ready") -> Dict[str, Any]:
    outbox_dir = Path("logs") / "email_outbox"
    outbox_dir.mkdir(parents=True, exist_ok=True)

    to_email = safe_str(patient_ctx.get("email"))
    patient_name = safe_str(patient_ctx.get("patient_name")) or "Patient"
    chart_number = safe_str(patient_ctx.get("chart_number"))
    portal_url = f"{CALLCARE_PUBLIC_BASE_URL}/portal/login"

    if reason == "addendum":
        subject = "A CallCare note was updated"
        plain = f"Hello {patient_name},\n\nA physician updated a note in your CallCare patient portal.\n\nPlease log in:\n{portal_url}\n"
    else:
        subject = "A new CallCare note is available"
        plain = f"Hello {patient_name},\n\nA new CallCare note is available in your CallCare patient portal.\n\nPlease log in:\n{portal_url}\n"

    payload = {
        "queued_at": now_iso(),
        "to_email": to_email,
        "patient_name": patient_name,
        "chart_number": chart_number,
        "packet_id": packet_id,
        "subject": subject,
        "body": plain,
        "reason": reason,
        "sent": False,
        "send_method": "queued_only",
    }

    if not to_email:
        payload["error"] = "No patient email on file"
    elif CALLCARE_EMAIL_PROVIDER == "resend" and CALLCARE_RESEND_API_KEY:
        try:
            resp = requests.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {CALLCARE_RESEND_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": "CallCare <onboarding@resend.dev>",
                    "to": [to_email],
                    "subject": subject,
                    "text": plain,
                },
                timeout=20,
            )
            if 200 <= resp.status_code < 300:
                payload["sent"] = True
                payload["send_method"] = "resend"
            else:
                payload["error"] = safe_str(resp.text)
        except Exception as e:
            payload["error"] = safe_str(e)
    else:
        result = queue_or_send_new_note_email(patient_ctx, chart_number, packet_id)
        payload.update(result)

    outbox_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{packet_id}.json"
    (outbox_dir / outbox_name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def fetch_chart_profile(chart_number: str) -> Dict[str, Any]:
    if not shared_db_url():
        return {}

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  p.id::text AS patient_id,
                  p.chart_number,
                  trim(concat_ws(' ', p.legal_first_name, p.legal_last_name)) AS patient_name,
                  p.legal_first_name,
                  p.legal_last_name,
                  p.preferred_name,
                  p.date_of_birth::text AS date_of_birth,
                  p.sex_at_birth,
                  p.gender_identity,
                  p.phone_number,
                  p.email
                FROM callcare.patients p
                WHERE p.chart_number = %s
                  AND p.archived_at IS NULL
                LIMIT 1
                """,
                (chart_number,),
            )
            patient = dict(cur.fetchone() or {})
            patient_id = patient.get("patient_id")
            if not patient_id:
                return {}

            cur.execute(
                """
                SELECT address_line_1, address_line_2, city, state, postal_code, county_name
                FROM callcare.patient_addresses
                WHERE patient_id = %s::uuid
                ORDER BY created_at DESC NULLS LAST
                LIMIT 1
                """,
                (patient_id,),
            )
            address = dict(cur.fetchone() or {})

            cur.execute(
                """
                SELECT height_feet, height_inches, weight_lbs
                FROM callcare.patient_vitals
                WHERE patient_id = %s::uuid
                ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
                LIMIT 1
                """,
                (patient_id,),
            )
            vitals = dict(cur.fetchone() or {})

            cur.execute(
                """
                SELECT ph.name, ph.address_line_1, ph.city, ph.state, ph.postal_code, ph.phone, ph.fax
                FROM callcare.patient_pharmacies pp
                JOIN callcare.pharmacies ph ON ph.id = pp.pharmacy_id
                WHERE pp.patient_id = %s::uuid
                  AND pp.is_preferred = true
                ORDER BY pp.created_at DESC
                LIMIT 1
                """,
                (patient_id,),
            )
            pharmacy = dict(cur.fetchone() or {})

            return {
                "patient": patient,
                "address": address,
                "vitals": vitals,
                "pharmacy": pharmacy,
            }


def fetch_history_allergies(chart_number: str) -> Dict[str, Any]:
    with db_connect() as conn:
        with conn.cursor() as cur:
            patient_id = patient_id_for_chart(cur, chart_number)

            cur.execute(
                """
                SELECT
                  condition_name,
                  bool_or(current_flag) AS current_flag,
                  bool_or(past_flag) AS past_flag,
                  bool_or(family_history_flag) AS family_history_flag,
                  string_agg(DISTINCT COALESCE(notes, ''), '; ') AS notes
                FROM callcare.patient_conditions
                WHERE patient_id = %s::uuid
                  AND archived_at IS NULL
                GROUP BY condition_name
                ORDER BY condition_name
                """,
                (patient_id,),
            )
            conditions = [dict(r) for r in cur.fetchall()]

            cur.execute(
                """
                SELECT allergen, reaction, severity, is_active
                FROM callcare.patient_allergies
                WHERE patient_id = %s::uuid
                ORDER BY is_active DESC, updated_at DESC, created_at DESC
                """,
                (patient_id,),
            )
            allergies = [dict(r) for r in cur.fetchall()]

            return {"patient_id": patient_id, "conditions": conditions, "allergies": allergies}


def fetch_medications(chart_number: str) -> List[Dict[str, Any]]:
    with db_connect() as conn:
        with conn.cursor() as cur:
            patient_id = patient_id_for_chart(cur, chart_number)
            cur.execute(
                """
                SELECT medication_name, strength, dose_instructions, route, frequency, is_current
                FROM callcare.patient_medications
                WHERE patient_id = %s::uuid
                ORDER BY is_current DESC, updated_at DESC, created_at DESC
                """,
                (patient_id,),
            )
            return [dict(r) for r in cur.fetchall()]


def fetch_social(chart_number: str) -> Dict[str, Any]:
    with db_connect() as conn:
        with conn.cursor() as cur:
            patient_id = patient_id_for_chart(cur, chart_number)
            cur.execute(
                """
                SELECT tobacco_status, alcohol_use, drug_use, exercise_level, occupation,
                       sexually_active, sexual_partners_count, uses_protection, protection_type,
                       previous_tobacco_user, tobacco_products, cigarette_packs_per_day,
                       recreational_drug_use
                FROM callcare.patient_social_history_structured
                WHERE patient_id = %s::uuid
                ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
                LIMIT 1
                """,
                (patient_id,),
            )
            return dict(cur.fetchone() or {})


def demographics_form(chart_number: str, packet_id: str) -> str:
    bundle = fetch_chart_profile(chart_number)
    patient = bundle.get("patient") or {}
    address = bundle.get("address") or {}
    vitals = bundle.get("vitals") or {}
    pharmacy = bundle.get("pharmacy") or {}

    return f"""
    <form method="post" action="/patient/{html_escape(chart_number)}/demographics?packet_id={html_escape(packet_id)}" autocomplete="off">
      <div class="card">
        <h2 class="section-title">Background</h2>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:18px;">
          <div><label>Preferred Name</label><input name="preferred_name" value="{html_escape(patient.get('preferred_name'))}" /></div>
          <div><label>Phone Number</label><input name="phone_number" value="{html_escape(patient.get('phone_number'))}" /></div>
          <div><label>Email</label><input name="email" value="{html_escape(patient.get('email'))}" /></div>
          <div>
            <label>Sex Assigned at Birth</label>
            <select name="sex_at_birth">
              <option value="">Select</option>
              <option value="female" {selected_attr("female", patient.get("sex_at_birth"))}>Female</option>
              <option value="male" {selected_attr("male", patient.get("sex_at_birth"))}>Male</option>
              <option value="intersex" {selected_attr("intersex", patient.get("sex_at_birth"))}>Intersex</option>
              <option value="prefer not to say" {selected_attr("prefer not to say", patient.get("sex_at_birth"))}>Prefer not to say</option>
            </select>
          </div>
          <div><label>Gender Identity</label><input name="gender_identity" value="{html_escape(patient.get('gender_identity'))}" /></div>
        </div>
      </div>

      <div class="card" style="margin-top:20px;">
        <h2 class="section-title">Address</h2>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:18px;">
          <div><label>Address</label><input name="address_line_1" value="{html_escape(address.get('address_line_1'))}" /></div>
          <div><label>Apartment / Unit</label><input name="address_line_2" value="{html_escape(address.get('address_line_2'))}" /></div>
          <div><label>City</label><input name="city" value="{html_escape(address.get('city'))}" /></div>
          <div><label>State</label><input name="state" value="{html_escape(address.get('state') or 'GA')}" /></div>
          <div><label>ZIP Code</label><input name="postal_code" value="{html_escape(address.get('postal_code'))}" /></div>
          <div><label>County</label><input name="county_name" value="{html_escape(address.get('county_name'))}" /></div>
        </div>
      </div>

      <div class="card" style="margin-top:20px;">
        <h2 class="section-title">Height & Weight</h2>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:18px;">
          <div><label>Height Feet</label><input name="height_feet" value="{html_escape(vitals.get('height_feet'))}" /></div>
          <div><label>Height Inches</label><input name="height_inches" value="{html_escape(vitals.get('height_inches'))}" /></div>
          <div><label>Weight Pounds</label><input name="weight_lbs" value="{html_escape(vitals.get('weight_lbs'))}" /></div>
        </div>
      </div>

      <div class="card" style="margin-top:20px;">
        <h2 class="section-title">Preferred Pharmacy</h2>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:18px;">
          <div><label>Pharmacy Name</label><input name="pharmacy_name" value="{html_escape(pharmacy.get('name'))}" /></div>
          <div><label>Pharmacy Address</label><input name="pharmacy_address_line_1" value="{html_escape(pharmacy.get('address_line_1'))}" /></div>
          <div><label>Pharmacy City</label><input name="pharmacy_city" value="{html_escape(pharmacy.get('city'))}" /></div>
          <div><label>Pharmacy State</label><input name="pharmacy_state" value="{html_escape(pharmacy.get('state') or 'GA')}" /></div>
          <div><label>Pharmacy ZIP Code</label><input name="pharmacy_postal_code" value="{html_escape(pharmacy.get('postal_code'))}" /></div>
          <div><label>Pharmacy Phone</label><input name="pharmacy_phone" value="{html_escape(pharmacy.get('phone'))}" /></div>
          <div><label>Pharmacy Fax</label><input name="pharmacy_fax" value="{html_escape(pharmacy.get('fax'))}" /></div>
        </div>
        <p class="btnbar"><button type="submit">Save Demographics & Pharmacy</button></p>
      </div>
    </form>
    """


def history_form(chart_number: str, packet_id: str) -> str:
    data = fetch_history_allergies(chart_number)
    conditions = data.get("conditions") or []
    allergies = data.get("allergies") or []

    existing = {safe_str(c.get("condition_name")).lower(): c for c in conditions}
    common_names = {c.lower() for c in COMMON_HISTORY_CONDITIONS}
    other_lines = []
    seen_other = set()

    for item in conditions:
        name = safe_str(item.get("condition_name"))
        key = name.lower()
        if not name or key in common_names or key in seen_other:
            continue
        seen_other.add(key)
        other_lines.append(name)

    rows = []
    for cond in COMMON_HISTORY_CONDITIONS:
        item = existing.get(cond.lower()) or {}
        key = cond.lower().replace(" ", "_")
        rows.append(
            f"""
            <tr style="background:{'rgba(47,158,143,0.10)' if len(rows) % 2 == 0 else 'rgba(255,255,255,0.95)'};">
              <td>{html_escape(cond)}</td>
              <td style="text-align:center;"><input type="checkbox" name="{html_escape(key)}_current" {checkbox_attr(item.get("current_flag"))}></td>
              <td style="text-align:center;"><input type="checkbox" name="{html_escape(key)}_past" {checkbox_attr(item.get("past_flag"))}></td>
              <td style="text-align:center;"><input type="checkbox" name="{html_escape(key)}_family" {checkbox_attr(item.get("family_history_flag"))}></td>
            </tr>
            """
        )

    allergy_rows = []
    total_allergy_rows = max(1, len(allergies))

    for i in range(total_allergy_rows):
        a = allergies[i] if i < len(allergies) else {}
        severity = safe_str(a.get("severity")).lower()
        allergy_rows.append(
            f"""
            <tr style="background:{'rgba(47,158,143,0.10)' if i % 2 == 0 else 'rgba(255,255,255,0.96)'};">
              <td><input name="allergy_{i}_allergen" value="{html_escape(a.get('allergen'))}" placeholder="Allergen" oninput="autoCheckAllergyRow(this)" /></td>
              <td><input name="allergy_{i}_reaction" value="{html_escape(a.get('reaction'))}" placeholder="Reaction" /></td>
              <td>
                <select name="allergy_{i}_severity">
                  <option value="">Select</option>
                  <option value="mild" {selected_attr("mild", severity)}>Mild</option>
                  <option value="moderate" {selected_attr("moderate", severity)}>Moderate</option>
                  <option value="severe" {selected_attr("severe", severity)}>Severe</option>
                  <option value="life-threatening" {selected_attr("life-threatening", severity)}>Life-threatening</option>
                </select>
              </td>
              <td style="text-align:center;"><input type="checkbox" name="allergy_{i}_active" {checkbox_attr(a.get("is_active") if a.get("allergen") else False)}></td>
            </tr>
            """
        )

    return f"""
    <form method="post" action="/patient/{html_escape(chart_number)}/history?packet_id={html_escape(packet_id)}">
      <div class="card">
        <h2 class="section-title">Medical History</h2>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:18px;align-items:start;">
          <table>
            <thead><tr><th>Condition</th><th>Current</th><th>Past</th><th>Family History</th></tr></thead>
            <tbody>{''.join(rows[:(len(rows)+1)//2])}</tbody>
          </table>
          <table>
            <thead><tr><th>Condition</th><th>Current</th><th>Past</th><th>Family History</th></tr></thead>
            <tbody>{''.join(rows[(len(rows)+1)//2:])}</tbody>
          </table>
        </div>
      </div>

      <div class="card" style="margin-top:20px;">
        <h2 class="section-title">Allergies</h2>
        <table>
          <thead><tr><th>Allergen</th><th>Reaction</th><th>Severity</th><th>Active</th></tr></thead>
          <tbody id="allergies-body">{''.join(allergy_rows)}</tbody>
        </table>
        <div style="margin-top:18px;display:flex;justify-content:flex-end;">
          <button type="button" onclick="addAllergyRow()">Add Another Row</button>
        </div>
      </div>

      <div class="card" style="margin-top:20px;">
        <h2 class="section-title">Other Conditions</h2>
        <textarea name="other_conditions" rows="8" style="height:150px;min-height:150px;max-height:150px;resize:vertical;" placeholder="Enter any additional diagnoses or medical conditions here.">{html_escape(chr(10).join(other_lines))}</textarea>
        <p class="btnbar"><button type="submit">Save Medical History</button></p>
      </div>

      <script>
        let nextAllergyIndex = {total_allergy_rows};

        function autoCheckAllergyRow(input) {{
          const row = input.closest("tr");
          if (!row) return;
          const checkbox = row.querySelector("input[type='checkbox']");
          if (!checkbox) return;
          checkbox.checked = input.value.trim().length > 0;
        }}

        function addAllergyRow() {{
          const body = document.getElementById("allergies-body");
          if (!body) return;
          const i = nextAllergyIndex++;
          const tr = document.createElement("tr");
          tr.style.background = i % 2 === 0 ? "rgba(47,158,143,0.10)" : "rgba(255,255,255,0.96)";
          tr.innerHTML = `
            <td><input name="allergy_${{i}}_allergen" placeholder="Allergen" oninput="autoCheckAllergyRow(this)" /></td>
            <td><input name="allergy_${{i}}_reaction" placeholder="Reaction" /></td>
            <td>
              <select name="allergy_${{i}}_severity">
                <option value="">Select</option>
                <option value="mild">Mild</option>
                <option value="moderate">Moderate</option>
                <option value="severe">Severe</option>
                <option value="life-threatening">Life-threatening</option>
              </select>
            </td>
            <td style="text-align:center;"><input type="checkbox" name="allergy_${{i}}_active" /></td>
          `;
          body.appendChild(tr);
        }}
      </script>
    </form>
    """


def medications_form(chart_number: str, packet_id: str) -> str:
    meds = fetch_medications(chart_number)

    deduped = []
    seen = set()
    for med in meds:
        key = (
            safe_str(med.get("medication_name")).lower(),
            safe_str(med.get("strength") or med.get("dose_instructions")).lower(),
            safe_str(med.get("route")).lower(),
            safe_str(med.get("frequency")).lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(med)

    total_rows = max(5, len(deduped))
    rows = []
    for i in range(total_rows):
        med = deduped[i] if i < len(deduped) else {}
        name = safe_str(med.get("medication_name"))
        dose = safe_str(med.get("strength") or med.get("dose_instructions"))
        route = safe_str(med.get("route"))
        frequency = safe_str(med.get("frequency"))
        active_checked = "checked" if name and med.get("is_current") is True else ""

        rows.append(
            f"""
            <tr style="background:{'rgba(47,158,143,0.10)' if i % 2 == 0 else 'rgba(255,255,255,0.96)'};">
              <td><input name="med_{i}_name" value="{html_escape(name)}" placeholder="Medication or supplement name" oninput="autoCheckMedicationRow(this)" /></td>
              <td><input name="med_{i}_dose" value="{html_escape(dose)}" placeholder="Dose / strength" /></td>
              <td>
                <select name="med_{i}_route">
                  <option value="">Select</option>
                  <option value="oral" {selected_attr("oral", route)}>Oral</option>
                  <option value="topical" {selected_attr("topical", route)}>Topical</option>
                  <option value="injection" {selected_attr("injection", route)}>Injection</option>
                </select>
              </td>
              <td><input name="med_{i}_frequency" value="{html_escape(frequency)}" placeholder="How often?" /></td>
              <td style="text-align:center;"><input type="checkbox" name="med_{i}_active" {active_checked} /></td>
            </tr>
            """
        )

    return f"""
    <form method="post" action="/patient/{html_escape(chart_number)}/medications?packet_id={html_escape(packet_id)}" autocomplete="off">
      <div class="card">
        <h2 class="section-title">Medications & Supplements</h2>
        <table>
          <thead><tr><th>Name</th><th>Dose</th><th>Route</th><th>Frequency</th><th>Active</th></tr></thead>
          <tbody id="medications-body">{''.join(rows)}</tbody>
        </table>
        <div style="margin-top:18px;display:flex;justify-content:flex-end;">
          <button type="button" onclick="addMedicationRow()">Add Additional Row</button>
        </div>
        <p class="btnbar"><button type="submit">Save Medications</button></p>
      </div>
      <script>
        let nextMedicationIndex = {total_rows};

        function autoCheckMedicationRow(input) {{
          const row = input.closest("tr");
          if (!row) return;
          const checkbox = row.querySelector("input[type='checkbox']");
          if (!checkbox) return;
          checkbox.checked = input.value.trim().length > 0;
        }}

        function addMedicationRow() {{
          const body = document.getElementById("medications-body");
          if (!body) return;
          const i = nextMedicationIndex++;
          const tr = document.createElement("tr");
          tr.style.background = i % 2 === 0 ? "rgba(47,158,143,0.10)" : "rgba(255,255,255,0.96)";
          tr.innerHTML = `
            <td><input name="med_${{i}}_name" placeholder="Medication or supplement name" oninput="autoCheckMedicationRow(this)" /></td>
            <td><input name="med_${{i}}_dose" placeholder="Dose / strength" /></td>
            <td>
              <select name="med_${{i}}_route">
                <option value="">Select</option>
                <option value="oral">Oral</option>
                <option value="topical">Topical</option>
                <option value="injection">Injection</option>
              </select>
            </td>
            <td><input name="med_${{i}}_frequency" placeholder="How often?" /></td>
            <td style="text-align:center;"><input type="checkbox" name="med_${{i}}_active" /></td>
          `;
          body.appendChild(tr);
        }}
      </script>
    </form>
    """


def social_form(chart_number: str, packet_id: str) -> str:
    social = fetch_social(chart_number)

    def opt(value: str, label: str, current: Any) -> str:
        return f'<option value="{html_escape(value)}" {selected_attr(value, current)}>{html_escape(label)}</option>'

    return f"""
    <form method="post" action="/patient/{html_escape(chart_number)}/social?packet_id={html_escape(packet_id)}" autocomplete="off">
      <div class="card">
        <h2 class="section-title">Social History</h2>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:18px;">
          <div>
            <label>Current Tobacco / Nicotine Use</label>
            <select name="tobacco_status">
              {opt("", "Select", social.get("tobacco_status"))}
              {opt("never", "Never", social.get("tobacco_status"))}
              {opt("current", "Current", social.get("tobacco_status"))}
              {opt("former", "Former", social.get("tobacco_status"))}
            </select>
          </div>
          <div>
            <label>Previous Tobacco User?</label>
            <select name="previous_tobacco_user">
              {opt("", "Select", social.get("previous_tobacco_user"))}
              {opt("yes", "Yes", social.get("previous_tobacco_user"))}
              {opt("no", "No", social.get("previous_tobacco_user"))}
            </select>
          </div>
          <div><label>Tobacco Products</label><input name="tobacco_products" value="{html_escape(social.get('tobacco_products'))}" placeholder="Cigarettes, vaping, cigars, chewing tobacco" /></div>
          <div><label>Cigarette Packs Per Day</label><input name="cigarette_packs_per_day" value="{html_escape(social.get('cigarette_packs_per_day'))}" /></div>
          <div>
            <label>Alcohol Use</label>
            <select name="alcohol_use">
              {opt("", "Select", social.get("alcohol_use"))}
              {opt("none", "None", social.get("alcohol_use"))}
              {opt("1 drink per day", "1 drink per day", social.get("alcohol_use"))}
              {opt("2 drinks per day", "2 drinks per day", social.get("alcohol_use"))}
              {opt("3+ drinks per day", "3+ drinks per day", social.get("alcohol_use"))}
            </select>
          </div>
          <div><label>Recreational Drug Use</label><input name="recreational_drug_use" value="{html_escape(social.get('recreational_drug_use') or social.get('drug_use'))}" /></div>
          <div>
            <label>Exercise</label>
            <select name="exercise_level">
              {opt("", "Select", social.get("exercise_level"))}
              {opt("0 days/week", "0 days/week", social.get("exercise_level"))}
              {opt("1-2 days/week", "1-2 days/week", social.get("exercise_level"))}
              {opt("3-5 days/week", "3-5 days/week", social.get("exercise_level"))}
              {opt("6-7 days/week", "6-7 days/week", social.get("exercise_level"))}
            </select>
          </div>
          <div><label>Occupation</label><input name="occupation" value="{html_escape(social.get('occupation'))}" /></div>
          <div>
            <label>Sexually Active?</label>
            <select name="sexually_active">
              {opt("", "Select", social.get("sexually_active"))}
              {opt("yes", "Yes", social.get("sexually_active"))}
              {opt("no", "No", social.get("sexually_active"))}
            </select>
          </div>
          <div><label>If sexually active, how many partners do you currently have?</label><input name="sexual_partners_count" value="{html_escape(social.get('sexual_partners_count'))}" /></div>
          <div>
            <label>Uses Protection?</label>
            <select name="uses_protection">
              {opt("", "Select", social.get("uses_protection"))}
              {opt("yes", "Yes", social.get("uses_protection"))}
              {opt("no", "No", social.get("uses_protection"))}
            </select>
          </div>
          <div><label>Protection Type</label><input name="protection_type" value="{html_escape(social.get('protection_type'))}" /></div>
        </div>
        <p class="btnbar"><button type="submit">Save Social History</button></p>
      </div>
    </form>
    """


@app.get("/healthz")
async def healthz() -> PlainTextResponse:
    return PlainTextResponse("ok")


@app.get("/login", response_class=HTMLResponse)
async def login_page() -> str:
    return shell(
        "CallCare Physician Login",
        """
        <div class="hero">
          <h1>CallCare Physician Portal</h1>
          <p>Physician review workspace.</p>
        </div>
        <div class="card" style="max-width:700px;margin:0 auto;">
          <h2 style="margin-top:0;">Log In</h2>
          <form method="post" action="/login">
            <label>Username</label>
            <input name="username" autocomplete="off" />
            <label>Password</label>
            <input name="password" type="password" autocomplete="current-password" />
            <button type="submit">Log In</button>
          </form>
        </div>
        """,
    )


@app.post("/login")
async def login(username: str = Form(...), password: str = Form(...)) -> RedirectResponse:
    if CALLCARE_PHYSICIAN_USERNAME and username != CALLCARE_PHYSICIAN_USERNAME:
        return RedirectResponse(url="/login", status_code=303)
    if CALLCARE_PHYSICIAN_PASSWORD and password != CALLCARE_PHYSICIAN_PASSWORD:
        return RedirectResponse(url="/login", status_code=303)

    token = make_session_token()
    SESSIONS[token] = {"username": username}
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie("callcare_physician_session", token, httponly=True, samesite="lax", path="/", secure=True)
    return response


@app.get("/logout")
async def logout(request: Request) -> RedirectResponse:
    token = request.cookies.get("callcare_physician_session", "")
    if token in SESSIONS:
        del SESSIONS[token]
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("callcare_physician_session", path="/")
    return response


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> str:
    require_session(request)
    groups = patient_groups()

    rows = []
    for g in groups:
        latest = g["encounters"][0]
        meta = latest["meta"]
        patient_ctx = latest["patient_ctx"] or {}
        rows.append(
            f"<tr>"
            f"<td><a href='/patient/{html_escape(g['chart_number'])}'>{html_escape(g['patient_name'])}</a></td>"
            f"<td>{html_escape(g['chart_number'])}</td>"
            f"<td>{len(g['encounters'])}</td>"
            f"<td>{html_escape(safe_str(patient_ctx.get('chief_complaint')))}</td>"
            f"<td>{html_escape(safe_str(meta.get('status')))}</td>"
            f"<td>{html_escape(safe_str(meta.get('prescription_status')))}</td>"
            f"</tr>"
        )

    if not rows:
        body = """
        <div class="hero">
          <h1>CallCare Physician Portal</h1>
          <p>No routed review packets yet.</p>
        </div>
        <p><a href="/logout">Log out</a></p>
        """
    else:
        body = f"""
        <div class="hero">
          <h1>CallCare Physician Portal</h1>
          <p>Physician review queue with linked patient charts, signed notes, addenda, and delivery tracking.</p>
        </div>
        <p><a href="/logout">Log out</a></p>
        <div class="card">
          <table>
            <thead>
              <tr>
                <th>Patient</th>
                <th>Chart #</th>
                <th>Encounters</th>
                <th>Latest Chief Complaint</th>
                <th>Status</th>
                <th>Prescription</th>
              </tr>
            </thead>
            <tbody>{''.join(rows)}</tbody>
          </table>
        </div>
        """

    return shell("CallCare Physician Portal", body)


@app.get("/packet/{packet_id}", response_class=HTMLResponse)
async def legacy_packet_redirect(packet_id: str, request: Request) -> RedirectResponse:
    require_session(request)
    path = packet_path(packet_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Packet not found")
    bundle = packet_bundle(path)
    if not bundle:
        raise HTTPException(status_code=404, detail="Packet bundle not found")
    chart_number = safe_str((bundle.get("patient_ctx") or {}).get("chart_number"))
    if not chart_number:
        raise HTTPException(status_code=404, detail="Patient chart not linked")
    return RedirectResponse(url=f"/patient/{chart_number}?packet_id={packet_id}&tab=encounters", status_code=303)


@app.get("/patient/{chart_number}", response_class=HTMLResponse)
async def patient_chart(
    chart_number: str,
    request: Request,
    packet_id: Optional[str] = Query(default=None),
    tab: str = Query(default="encounters"),
) -> str:
    require_session(request)

    groups = patient_groups()
    group = next((g for g in groups if g["chart_number"] == chart_number), None)
    if not group:
        raise HTTPException(status_code=404, detail="Patient chart not found")

    patient_ctx = group["patient_ctx"] or {}
    encounters = group["encounters"]

    selected_bundle = None
    if packet_id:
        selected_bundle = next((e for e in encounters if e["packet_id"] == packet_id), None)
    if not selected_bundle and encounters:
        selected_bundle = encounters[0]
    if not selected_bundle:
        raise HTTPException(status_code=404, detail="No encounters found")

    selected_packet_id = selected_bundle["packet_id"]
    selected_packet = selected_bundle["packet"]
    selected_meta = selected_bundle["meta"]
    selected_note = safe_str(selected_packet.get("note_text"))
    selected_signed_note = signed_note_text(selected_note, selected_meta)
    selected_spoken_summary = selected_bundle["spoken_summary"]

    if safe_str(selected_meta.get("status")) == "active":
        selected_meta["status"] = "under review"
        save_meta(selected_packet_id, selected_meta)

    encounter_tab_links = []
    for enc in encounters:
        enc_ctx = enc.get("patient_ctx") or {}
        label = _extract_differential_title(
            safe_str((enc.get("packet") or {}).get("note_text")),
            safe_str(enc_ctx.get("chief_complaint")),
        )
        started = encounter_when(safe_str(enc_ctx.get("encounter_started_at")), safe_str(enc.get("created_at")))
        active_class = "enc-link active" if enc["packet_id"] == selected_packet_id else "enc-link"
        encounter_tab_links.append(
            f"<li><a class='{active_class}' href='/patient/{html_escape(chart_number)}?packet_id={html_escape(enc['packet_id'])}&tab=encounters'>{html_escape(label)} — {html_escape(started)}</a></li>"
        )
    encounter_tab_html = "<ul>" + "".join(encounter_tab_links) + "</ul>"

    demographics_panel = demographics_form(chart_number, selected_packet_id)
    pmh_panel = history_form(chart_number, selected_packet_id)
    social_panel = social_form(chart_number, selected_packet_id)
    medications_panel = medications_form(chart_number, selected_packet_id)

    note_editor_html = (
        f"""
        <form method="post" action="/packet/{html_escape(selected_packet_id)}/update-note">
          <textarea class="note-textarea" id="note_text_editor" name="note_text">{html_escape(selected_note)}</textarea>
          <p class="btnbar"><button type="submit">Save Note Changes</button></p>
        </form>
        """
        if not selected_meta.get("signed")
        else f"""
        <div class="readonly">{html_escape(selected_signed_note)}</div>
        <p><em>Signed notes are read-only.</em></p>
        """
    )

    addenda_html = ""
    addenda = selected_meta.get("addenda") or []
    if addenda:
        addenda_html += "<div class='card'><h2 class='section-title'>Signed Addenda</h2>"
        for idx, add in enumerate(addenda, 1):
            addenda_html += f"<div class='readonly' style='margin-bottom:12px;'><strong>Addendum {idx}</strong>\n\n{html_escape(addendum_block(add))}</div>"
        addenda_html += "</div>"

    addendum_editor_html = (
        f"""
        <div class="card">
          <h2 class="section-title">Add Addendum</h2>
          <form method="post" action="/packet/{html_escape(selected_packet_id)}/addendum">
            <textarea name="addendum_text" style="min-height:180px;"></textarea>
            <p class="btnbar"><button type="submit">Sign Addendum</button></p>
          </form>
        </div>
        """
        if selected_meta.get("signed")
        else ""
    )

    encounter_panel = f"""
      <div class="card">
        <h2 class="section-title">{html_escape(patient_ctx.get('patient_name'))}</h2>
        <div class="meta-grid">
          <div class="metric"><div class="label">Chart #</div><div class="value">{html_escape(patient_ctx.get('chart_number'))}</div></div>
          <div class="metric"><div class="label">Date of Birth</div><div class="value">{html_escape(patient_ctx.get('date_of_birth'))}</div></div>
          <div class="metric"><div class="label">Sex at Birth</div><div class="value">{html_escape(patient_ctx.get('sex_at_birth'))}</div></div>
          <div class="metric"><div class="label">Chief Complaint</div><div class="value">{html_escape((selected_bundle.get('patient_ctx') or {}).get('chief_complaint'))}</div></div>
          <div class="metric"><div class="label">Encounter Started</div><div class="value">{html_escape((selected_bundle.get('patient_ctx') or {}).get('encounter_started_at') or selected_bundle.get('created_at'))}</div></div>
          <div class="metric"><div class="label">Status</div><div class="value">{html_escape(selected_meta.get('status'))}</div></div>
        </div>
        <p class="pill">Prescription: {html_escape(selected_meta.get('prescription_status'))}</p>
        <p class="pill">Delivery: {html_escape(selected_meta.get('note_sent'))}</p>
        <div class="btnbar">
          <form method="get" action="/packet/{html_escape(selected_packet_id)}/full-text">
            <button class="btn-soft" type="submit">Full Transcript</button>
          </form>
        </div>
      </div>

      <div class="card">
        <h2 class="section-title">Clinical Note</h2>
        {note_editor_html}
      </div>

      <div class="card">
        <h2 class="section-title">Spoken Summary to Patient</h2>
        <div class="readonly">{html_escape(selected_spoken_summary or 'No spoken summary available.')}</div>
        <h3 style="margin-top:18px;">Physician's Comments on Spoken Summary</h3>
        {(
          f'<div class="readonly">{html_escape(selected_meta.get("spoken_summary_comments") or "No physician comments on spoken summary.")}</div><p><em>Signed notes lock spoken-summary comments. Use an addendum for any later changes.</em></p>'
          if selected_meta.get("signed")
          else
          f'<form method="post" action="/packet/{html_escape(selected_packet_id)}/update-spoken-summary-comments"><textarea id="spoken_summary_comments_editor" name="spoken_summary_comments" style="min-height:180px;">{html_escape(selected_meta.get("spoken_summary_comments"))}</textarea><p class="btnbar"><button type="submit">Save Spoken Summary Comments</button></p></form>'
        )}
      </div>

      {addenda_html}
      {addendum_editor_html}

      <div class="card">
        <h2 class="section-title">Physician Actions</h2>
        <div class="btnbar">
          {'' if selected_meta.get("signed") else f'<form method="post" action="/packet/{html_escape(selected_packet_id)}/sign"><button type="submit">Sign Note</button></form>'}
          <form method="post" action="/packet/{html_escape(selected_packet_id)}/prescribe"><button type="submit">Send Prescription</button></form>
          <form method="post" action="/packet/{html_escape(selected_packet_id)}/note-sent/to-be-mailed"><button type="submit">Mark Note To Be Mailed</button></form>
        </div>
      </div>
    """

    panel_html = {
        "demographics": demographics_panel,
        "pmh": pmh_panel,
        "social": social_panel,
        "medications": medications_panel,
        "encounters": encounter_panel,
    }.get(tab, encounter_panel)

    def tab_link(tab_name: str, label: str) -> str:
        active = "tab active" if tab == tab_name else "tab"
        return f"<a class='{active}' href='/patient/{html_escape(chart_number)}?packet_id={html_escape(selected_packet_id)}&tab={html_escape(tab_name)}'>{html_escape(label)}</a>"

    _sync_bundle_to_shared_db(selected_bundle)

    return shell(
        f"{safe_str(patient_ctx.get('patient_name'))} - CallCare Physician Portal",
        f"""
        <div class="hero">
          <h1>{html_escape(patient_ctx.get('patient_name'))}</h1>
          <p>Chart #{html_escape(patient_ctx.get('chart_number'))} · Physician review workspace</p>
        </div>

        <p><a href="/">← Back to patient list</a> | <a href="/logout">Log out</a></p>

        <div class="tabs">
          {tab_link("demographics", "Demographics & Pharmacy")}
          {tab_link("pmh", "Medical History")}
          {tab_link("social", "Social History")}
          {tab_link("medications", "Medications")}
          {tab_link("encounters", "Encounters")}
        </div>

        <div class="layout">
          <div class="card sidebar">
            <h3 style="margin-top:0;">Encounters</h3>
            {encounter_tab_html}
          </div>
          <div class="grid">
            {panel_html}
          </div>
        </div>
        """,
    )


@app.post("/patient/{chart_number}/demographics")
async def save_demographics(chart_number: str, request: Request, packet_id: str = Query(default="")) -> RedirectResponse:
    require_session(request)
    form = await request.form()

    with db_connect() as conn:
        with conn.cursor() as cur:
            patient_id = patient_id_for_chart(cur, chart_number)

            cur.execute(
                """
                UPDATE callcare.patients
                SET preferred_name = NULLIF(%s, ''),
                    sex_at_birth = NULLIF(%s, ''),
                    gender_identity = NULLIF(%s, ''),
                    phone_number = NULLIF(%s, ''),
                    email = NULLIF(%s, '')
                WHERE id = %s::uuid
                """,
                (
                    safe_str(form.get("preferred_name")),
                    safe_str(form.get("sex_at_birth")),
                    safe_str(form.get("gender_identity")),
                    safe_str(form.get("phone_number")),
                    safe_str(form.get("email")),
                    patient_id,
                ),
            )

            cur.execute(
                """
                INSERT INTO callcare.patient_addresses (
                  id, patient_id, address_line_1, address_line_2, city, state, postal_code, county_name
                )
                VALUES (
                  gen_random_uuid(), %s::uuid, %s, NULLIF(%s, ''), %s, %s, %s, NULLIF(%s, '')
                )
                """,
                (
                    patient_id,
                    safe_str(form.get("address_line_1")) or "Not provided",
                    safe_str(form.get("address_line_2")),
                    safe_str(form.get("city")) or "Not provided",
                    safe_str(form.get("state")) or "GA",
                    safe_str(form.get("postal_code")) or "00000",
                    safe_str(form.get("county_name")),
                ),
            )

            cur.execute(
                """
                INSERT INTO callcare.patient_vitals (
                  id, patient_id, height_feet, height_inches, weight_lbs, source, created_at, updated_at
                )
                VALUES (
                  gen_random_uuid(), %s::uuid, NULLIF(%s, '')::integer, NULLIF(%s, '')::integer,
                  NULLIF(%s, '')::numeric, 'physician_portal', now(), now()
                )
                """,
                (
                    patient_id,
                    safe_str(form.get("height_feet")),
                    safe_str(form.get("height_inches")),
                    safe_str(form.get("weight_lbs")),
                ),
            )

            pharmacy_name = safe_str(form.get("pharmacy_name"))
            if pharmacy_name:
                cur.execute(
                    """
                    INSERT INTO callcare.pharmacies (
                      id, name, address_line_1, city, state, postal_code, phone, fax, created_at
                    )
                    VALUES (
                      gen_random_uuid(), %s, NULLIF(%s, ''), NULLIF(%s, ''), NULLIF(%s, ''),
                      NULLIF(%s, ''), NULLIF(%s, ''), NULLIF(%s, ''), now()
                    )
                    RETURNING id::text
                    """,
                    (
                        pharmacy_name,
                        safe_str(form.get("pharmacy_address_line_1")),
                        safe_str(form.get("pharmacy_city")),
                        safe_str(form.get("pharmacy_state")),
                        safe_str(form.get("pharmacy_postal_code")),
                        safe_str(form.get("pharmacy_phone")),
                        safe_str(form.get("pharmacy_fax")),
                    ),
                )
                pharmacy_id = cur.fetchone()["id"]

                cur.execute(
                    "UPDATE callcare.patient_pharmacies SET is_preferred = false WHERE patient_id = %s::uuid",
                    (patient_id,),
                )

                cur.execute(
                    """
                    INSERT INTO callcare.patient_pharmacies (
                      id, patient_id, pharmacy_id, is_preferred, created_at
                    )
                    VALUES (
                      gen_random_uuid(), %s::uuid, %s::uuid, true, now()
                    )
                    ON CONFLICT (patient_id, pharmacy_id)
                    DO UPDATE SET is_preferred = true
                    """,
                    (patient_id, pharmacy_id),
                )

            audit_event(cur, patient_id, "patient_demographics_pharmacy_updated_by_physician", {"source": "physician_portal"})

        conn.commit()

    return RedirectResponse(url=f"/patient/{chart_number}?packet_id={packet_id}&tab=demographics", status_code=303)


@app.post("/patient/{chart_number}/history")
async def save_history(chart_number: str, request: Request, packet_id: str = Query(default="")) -> RedirectResponse:
    require_session(request)
    form = await request.form()

    condition_rows = []
    for cond in COMMON_HISTORY_CONDITIONS:
        key = cond.lower().replace(" ", "_")
        current_flag = safe_str(form.get(f"{key}_current")).lower() == "on"
        past_flag = safe_str(form.get(f"{key}_past")).lower() == "on"
        family_flag = safe_str(form.get(f"{key}_family")).lower() == "on"

        if current_flag or past_flag or family_flag:
            condition_rows.append({
                "condition_name": cond,
                "current_flag": current_flag,
                "past_flag": past_flag,
                "family_history_flag": family_flag,
                "notes": "",
            })

    other_text = safe_str(form.get("other_conditions"))
    if other_text:
        for line in other_text.splitlines():
            line = safe_str(line)
            if line:
                condition_rows.append({
                    "condition_name": line,
                    "current_flag": True,
                    "past_flag": False,
                    "family_history_flag": False,
                    "notes": "other_condition_writein",
                })

    allergy_rows = []
    for i in range(50):
        allergen = safe_str(form.get(f"allergy_{i}_allergen")).strip()
        if not allergen:
            continue
        allergy_rows.append({
            "allergen": allergen,
            "reaction": safe_str(form.get(f"allergy_{i}_reaction")),
            "severity": safe_str(form.get(f"allergy_{i}_severity")),
            "active": safe_str(form.get(f"allergy_{i}_active")).lower() == "on",
        })

    with db_connect() as conn:
        with conn.cursor() as cur:
            patient_id = patient_id_for_chart(cur, chart_number)

            cur.execute(
                """
                UPDATE callcare.patient_conditions
                SET archived_at = now()
                WHERE patient_id = %s::uuid
                  AND archived_at IS NULL
                """,
                (patient_id,),
            )

            for row in condition_rows:
                cur.execute(
                    """
                    INSERT INTO callcare.patient_conditions (
                      id, patient_id, condition_name, current_flag, past_flag,
                      family_history_flag, notes, source, verification_status, created_at, updated_at
                    )
                    VALUES (
                      gen_random_uuid(), %s::uuid, %s, %s, %s, %s, NULLIF(%s, ''),
                      'physician_portal', 'physician_verified', now(), now()
                    )
                    """,
                    (
                        patient_id,
                        row["condition_name"],
                        row["current_flag"],
                        row["past_flag"],
                        row["family_history_flag"],
                        row["notes"],
                    ),
                )

            cur.execute("DELETE FROM callcare.patient_allergies WHERE patient_id = %s::uuid", (patient_id,))
            for row in allergy_rows:
                cur.execute(
                    """
                    INSERT INTO callcare.patient_allergies (
                      id, patient_id, allergen, reaction, severity, is_active,
                      source, verification_status, created_at, updated_at
                    )
                    VALUES (
                      gen_random_uuid(), %s::uuid, %s, NULLIF(%s, ''), NULLIF(%s, ''),
                      %s, 'physician_portal', 'physician_verified', now(), now()
                    )
                    """,
                    (
                        patient_id,
                        row["allergen"],
                        row["reaction"],
                        row["severity"],
                        row["active"],
                    ),
                )

            audit_event(
                cur,
                patient_id,
                "patient_history_allergies_updated_by_physician",
                {"source": "physician_portal", "condition_count": len(condition_rows), "allergy_count": len(allergy_rows)},
            )

        conn.commit()

    return RedirectResponse(url=f"/patient/{chart_number}?packet_id={packet_id}&tab=pmh", status_code=303)


@app.post("/patient/{chart_number}/medications")
async def save_medications(chart_number: str, request: Request, packet_id: str = Query(default="")) -> RedirectResponse:
    require_session(request)
    form = await request.form()

    rows = []
    for i in range(50):
        name = safe_str(form.get(f"med_{i}_name")).strip()
        if not name:
            continue
        rows.append({
            "name": name,
            "dose": safe_str(form.get(f"med_{i}_dose")),
            "route": safe_str(form.get(f"med_{i}_route")),
            "frequency": safe_str(form.get(f"med_{i}_frequency")),
            "active": safe_str(form.get(f"med_{i}_active")).lower() == "on",
        })

    with db_connect() as conn:
        with conn.cursor() as cur:
            patient_id = patient_id_for_chart(cur, chart_number)
            cur.execute("DELETE FROM callcare.patient_medications WHERE patient_id = %s::uuid", (patient_id,))

            for row in rows:
                cur.execute(
                    """
                    INSERT INTO callcare.patient_medications (
                      id, patient_id, medication_name, strength, dose_instructions, route,
                      frequency, is_current, start_date, end_date, source,
                      verification_status, created_at, updated_at
                    )
                    VALUES (
                      gen_random_uuid(), %s::uuid, %s, NULLIF(%s, ''), NULLIF(%s, ''),
                      NULLIF(%s, ''), NULLIF(%s, ''), %s, CURRENT_DATE,
                      CASE WHEN %s THEN NULL ELSE CURRENT_DATE END,
                      'physician_portal', 'physician_verified', now(), now()
                    )
                    """,
                    (
                        patient_id,
                        row["name"],
                        row["dose"],
                        row["dose"],
                        row["route"],
                        row["frequency"],
                        row["active"],
                        row["active"],
                    ),
                )

            audit_event(cur, patient_id, "patient_medications_updated_by_physician", {"source": "physician_portal", "medication_count": len(rows)})

        conn.commit()

    return RedirectResponse(url=f"/patient/{chart_number}?packet_id={packet_id}&tab=medications", status_code=303)


@app.post("/patient/{chart_number}/social")
async def save_social(chart_number: str, request: Request, packet_id: str = Query(default="")) -> RedirectResponse:
    require_session(request)
    form = await request.form()

    with db_connect() as conn:
        with conn.cursor() as cur:
            patient_id = patient_id_for_chart(cur, chart_number)
            cur.execute("DELETE FROM callcare.patient_social_history_structured WHERE patient_id = %s::uuid", (patient_id,))
            cur.execute(
                """
                INSERT INTO callcare.patient_social_history_structured (
                  patient_id, tobacco_status, alcohol_use, drug_use, exercise_level, occupation,
                  sexually_active, sexual_partners_count, uses_protection, protection_type,
                  previous_tobacco_user, tobacco_products, cigarette_packs_per_day,
                  recreational_drug_use, created_at, updated_at
                )
                VALUES (
                  %s::uuid, NULLIF(%s, ''), NULLIF(%s, ''), NULLIF(%s, ''), NULLIF(%s, ''),
                  NULLIF(%s, ''), NULLIF(%s, ''), NULLIF(%s, '')::integer, NULLIF(%s, ''),
                  NULLIF(%s, ''), NULLIF(%s, ''), NULLIF(%s, ''), NULLIF(%s, '')::numeric,
                  NULLIF(%s, ''), now(), now()
                )
                """,
                (
                    patient_id,
                    safe_str(form.get("tobacco_status")),
                    safe_str(form.get("alcohol_use")),
                    safe_str(form.get("recreational_drug_use")),
                    safe_str(form.get("exercise_level")),
                    safe_str(form.get("occupation")),
                    safe_str(form.get("sexually_active")),
                    safe_str(form.get("sexual_partners_count")),
                    safe_str(form.get("uses_protection")),
                    safe_str(form.get("protection_type")),
                    safe_str(form.get("previous_tobacco_user")),
                    safe_str(form.get("tobacco_products")),
                    safe_str(form.get("cigarette_packs_per_day")),
                    safe_str(form.get("recreational_drug_use")),
                ),
            )

            audit_event(cur, patient_id, "patient_social_history_updated_by_physician", {"source": "physician_portal"})

        conn.commit()

    return RedirectResponse(url=f"/patient/{chart_number}?packet_id={packet_id}&tab=social", status_code=303)


@app.get("/packet/{packet_id}/full-text", response_class=HTMLResponse)
async def full_text(packet_id: str, request: Request) -> str:
    require_session(request)
    path = packet_path(packet_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Packet not found")

    bundle = packet_bundle(path)
    if not bundle:
        raise HTTPException(status_code=404, detail="Packet bundle not found")

    call_log = bundle["call_log"]
    transcript = call_log.get("transcript", []) if isinstance(call_log, dict) else []

    transcript_html = ""
    for turn in transcript:
        role = html_escape(turn.get("role"))
        text = html_escape(turn.get("text"))
        transcript_html += f"<p><strong>{role}:</strong> {text}</p>"

    if not transcript_html:
        transcript_html = "<p>No call transcript available for this packet.</p>"

    patient_ctx = bundle.get("patient_ctx") or {}
    chart_number = safe_str(patient_ctx.get("chart_number"))
    back_url = f"/patient/{chart_number}?packet_id={html_escape(bundle['packet_id'])}&tab=encounters" if chart_number else "/"

    return shell(
        f"Full Transcript {packet_id}",
        f"""
        <div class="hero">
          <h1>Full Transcript</h1>
          <p>Packet {html_escape(packet_id)}</p>
        </div>
        <p><a href="{back_url}">← Back to encounter</a></p>
        <div class="card">{transcript_html}</div>
        """,
    )


@app.post("/packet/{packet_id}/update-note")
async def update_note(packet_id: str, request: Request, note_text: str = Form(...)) -> RedirectResponse:
    require_session(request)
    path = packet_path(packet_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Packet not found")

    meta = load_meta(packet_id)
    if meta.get("signed"):
        raise HTTPException(status_code=400, detail="Signed notes are read-only")

    data = load_json(path)
    data["note_text"] = safe_str(note_text)
    save_json(path, data)

    bundle = packet_bundle(path)
    if bundle:
        _sync_bundle_to_shared_db(bundle)
        _queue_or_send_new_note_email_resend(packet_id, bundle.get("patient_ctx") or {})

    chart_number = safe_str((bundle.get("patient_ctx") or {}).get("chart_number")) if bundle else ""
    return RedirectResponse(url=f"/patient/{chart_number}?packet_id={packet_id}&tab=encounters", status_code=303)


@app.post("/packet/{packet_id}/update-spoken-summary-comments")
async def update_spoken_summary_comments(
    packet_id: str,
    request: Request,
    spoken_summary_comments: str = Form(...),
) -> RedirectResponse:
    require_session(request)
    path = packet_path(packet_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Packet not found")

    meta = load_meta(packet_id)
    meta["spoken_summary_comments"] = safe_str(spoken_summary_comments)
    save_meta(packet_id, meta)

    bundle = packet_bundle(path)
    if bundle:
        _sync_bundle_to_shared_db(bundle)
        _queue_or_send_new_note_email_resend(packet_id, bundle.get("patient_ctx") or {})

    chart_number = safe_str((bundle.get("patient_ctx") or {}).get("chart_number")) if bundle else ""
    return RedirectResponse(url=f"/patient/{chart_number}?packet_id={packet_id}&tab=encounters", status_code=303)


@app.post("/packet/{packet_id}/sign")
async def sign_note(packet_id: str, request: Request) -> RedirectResponse:
    require_session(request)
    path = packet_path(packet_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Packet not found")

    save_note_signed(packet_id)

    bundle = packet_bundle(path)
    if bundle:
        _sync_bundle_to_shared_db(bundle)
        _queue_or_send_new_note_email_resend(packet_id, bundle.get("patient_ctx") or {}, reason="note_ready")

    chart_number = safe_str((bundle.get("patient_ctx") or {}).get("chart_number")) if bundle else ""
    return RedirectResponse(url=f"/patient/{chart_number}?packet_id={packet_id}&tab=encounters", status_code=303)


@app.post("/packet/{packet_id}/addendum")
async def sign_addendum(packet_id: str, request: Request, addendum_text: str = Form(...)) -> RedirectResponse:
    require_session(request)
    path = packet_path(packet_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Packet not found")

    meta = load_meta(packet_id)
    if not meta.get("signed"):
        raise HTTPException(status_code=400, detail="Note must be signed before addenda can be added")
    if not safe_str(addendum_text):
        raise HTTPException(status_code=400, detail="Addendum text is required")

    add_signed_addendum(packet_id, addendum_text)

    bundle = packet_bundle(path)
    if bundle:
        _sync_bundle_to_shared_db(bundle)
        _queue_or_send_new_note_email_resend(packet_id, bundle.get("patient_ctx") or {}, reason="addendum")

    chart_number = safe_str((bundle.get("patient_ctx") or {}).get("chart_number")) if bundle else ""
    return RedirectResponse(url=f"/patient/{chart_number}?packet_id={packet_id}&tab=encounters", status_code=303)


@app.post("/packet/{packet_id}/prescribe")
async def send_prescription(packet_id: str, request: Request) -> RedirectResponse:
    require_session(request)
    path = packet_path(packet_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Packet not found")

    meta = load_meta(packet_id)
    meta["prescription_status"] = "sent"
    save_meta(packet_id, meta)

    bundle = packet_bundle(path)
    if bundle:
        _sync_bundle_to_shared_db(bundle)
        _queue_or_send_new_note_email_resend(packet_id, bundle.get("patient_ctx") or {})

    chart_number = safe_str((bundle.get("patient_ctx") or {}).get("chart_number")) if bundle else ""
    return RedirectResponse(url=f"/patient/{chart_number}?packet_id={packet_id}&tab=encounters", status_code=303)


@app.post("/packet/{packet_id}/note-sent/{mode}")
async def note_sent(packet_id: str, mode: str, request: Request) -> RedirectResponse:
    require_session(request)
    path = packet_path(packet_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Packet not found")

    normalized = safe_str(mode).lower()
    if normalized not in {"emailed", "to-be-mailed", "to_be_mailed", "to be mailed"}:
        raise HTTPException(status_code=400, detail="Invalid note-sent mode")

    meta = load_meta(packet_id)
    bundle = packet_bundle(path)

    if normalized == "emailed":
        result = _queue_or_send_new_note_email_resend(packet_id, (bundle or {}).get("patient_ctx") or {})
        meta["note_sent"] = "emailed"
        meta["email_last_queued_at"] = safe_str(result.get("queued_at"))
    else:
        meta["note_sent"] = "to be mailed"

    save_meta(packet_id, meta)

    chart_number = safe_str(((bundle or {}).get("patient_ctx") or {}).get("chart_number"))
    return RedirectResponse(url=f"/patient/{chart_number}?packet_id={packet_id}&tab=encounters", status_code=303)
