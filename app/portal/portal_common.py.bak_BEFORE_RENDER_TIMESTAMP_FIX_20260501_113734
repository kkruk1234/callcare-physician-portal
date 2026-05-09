from __future__ import annotations

import json
import os
import re
import secrets
import smtplib
import subprocess
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Optional

PORTAL_INBOX_DIR = Path("logs") / "portal_inbox"
PORTAL_INBOX_DIR.mkdir(parents=True, exist_ok=True)

CALL_LOG_DIR = Path("logs") / "calls"
CALL_LOG_DIR.mkdir(parents=True, exist_ok=True)

EMAIL_OUTBOX_DIR = Path("logs") / "email_outbox"
EMAIL_OUTBOX_DIR.mkdir(parents=True, exist_ok=True)

DB_NAME = os.getenv("CALLCARE_DB_NAME", "callcare").strip() or "callcare"
PHYSICIAN_NAME = os.getenv("CALLCARE_PHYSICIAN_NAME", "Asynchronous Physician").strip() or "Asynchronous Physician"
PHYSICIAN_CREDENTIALS = os.getenv("CALLCARE_PHYSICIAN_CREDENTIALS", "").strip()

SMTP_HOST = os.getenv("CALLCARE_SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("CALLCARE_SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("CALLCARE_SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.getenv("CALLCARE_SMTP_PASSWORD", "").strip()
SMTP_FROM = os.getenv("CALLCARE_SMTP_FROM", "").strip()
SMTP_USE_TLS = os.getenv("CALLCARE_SMTP_USE_TLS", "true").strip().lower() == "true"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_str(x: Any) -> str:
    try:
        return str(x if x is not None else "").strip()
    except Exception:
        return ""


def html_escape(s: Any) -> str:
    text = safe_str(s)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: Dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def packet_files() -> List[Path]:
    return sorted(
        [p for p in PORTAL_INBOX_DIR.glob("*.json") if not p.name.endswith(".meta.json")],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def packet_path(packet_id: str) -> Path:
    return PORTAL_INBOX_DIR / f"{packet_id}.json"


def meta_path(packet_id: str) -> Path:
    return PORTAL_INBOX_DIR / f"{packet_id}.meta.json"


def default_meta(packet_id: str) -> Dict[str, Any]:
    return {
        "packet_id": packet_id,
        "status": "active",
        "prescription_status": "under review",
        "note_sent": "to be mailed",
        "call_sid": "",
        "signed": False,
        "signed_at": "",
        "signed_by": "",
        "addenda": [],
        "spoken_summary_comments": "",
        "email_last_queued_at": "",
    }


def load_meta(packet_id: str) -> Dict[str, Any]:
    mp = meta_path(packet_id)
    if mp.exists():
        try:
            d = load_json(mp)
            base = default_meta(packet_id)
            base.update(d)
            if not isinstance(base.get("addenda"), list):
                base["addenda"] = []
            if base.get("status") == "completed" and not base.get("signed"):
                base["signed"] = True
                if not safe_str(base.get("signed_at")):
                    base["signed_at"] = now_iso()
                if not safe_str(base.get("signed_by")):
                    base["signed_by"] = signature_line()
                save_meta(packet_id, base)
            return base
        except Exception:
            pass
    return default_meta(packet_id)


def save_meta(packet_id: str, meta: Dict[str, Any]) -> None:
    base = default_meta(packet_id)
    base.update(meta or {})
    if not isinstance(base.get("addenda"), list):
        base["addenda"] = []
    save_json(meta_path(packet_id), base)


def run_psql(sql: str, vars_map: Optional[Dict[str, str]] = None) -> str:
    cmd = ["psql", DB_NAME, "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1"]
    for k, v in (vars_map or {}).items():
        cmd.extend(["-v", f"{k}={v}"])
    proc = subprocess.run(
        cmd,
        input=sql,
        text=True,
        capture_output=True,
        check=True,
    )
    return proc.stdout.strip()


def load_call_log_by_sid(call_sid: str) -> Dict[str, Any]:
    if not call_sid:
        return {}
    path = CALL_LOG_DIR / f"{call_sid}.json"
    if not path.exists():
        return {}
    try:
        return load_json(path)
    except Exception:
        return {}


def extract_spoken_summary_from_call_log(call_log: Dict[str, Any]) -> str:
    transcript = call_log.get("transcript", []) if isinstance(call_log, dict) else []
    if not isinstance(transcript, list):
        return ""
    for turn in reversed(transcript):
        if safe_str(turn.get("role")).lower() == "assistant":
            return safe_str(turn.get("text"))
    return ""


def resolve_call_sid(packet_id: str, packet: Dict[str, Any], meta: Dict[str, Any]) -> str:
    candidates = [
        safe_str(meta.get("call_sid")),
        safe_str(packet.get("call_sid")),
        safe_str(packet.get("session_id")),
    ]
    for c in candidates:
        if c:
            return c
    return ""


def lookup_patient_context(call_sid: str) -> Dict[str, Any]:
    if not call_sid:
        return {}

    sql = r"""
    SELECT json_build_object(
      'encounter_id', e.id::text,
      'call_sid', e.call_sid,
      'patient_id', p.id::text,
      'chart_number', p.chart_number,
      'patient_name', trim(concat_ws(' ', p.legal_first_name, p.legal_last_name)),
      'legal_first_name', p.legal_first_name,
      'legal_last_name', p.legal_last_name,
      'date_of_birth', p.date_of_birth::text,
      'sex_at_birth', p.sex_at_birth,
      'phone_number', p.phone_number,
      'email', p.email,
      'chief_complaint', e.chief_complaint,
      'encounter_started_at', e.started_at::text,
      'conditions',
        COALESCE(
          (
            SELECT json_agg(
              json_build_object(
                'condition_name', pc.condition_name,
                'status', pc.status
              )
              ORDER BY pc.created_at
            )
            FROM callcare.patient_conditions pc
            WHERE pc.patient_id = p.id
          ),
          '[]'::json
        ),
      'social_history',
        COALESCE(
          (
            SELECT json_agg(
              json_build_object(
                'domain', sh.domain,
                'value_text', sh.value_text
              )
              ORDER BY sh.created_at
            )
            FROM callcare.patient_social_history sh
            WHERE sh.patient_id = p.id
          ),
          '[]'::json
        ),
      'allergies',
        COALESCE(
          (
            SELECT json_agg(
              json_build_object(
                'allergen', a.allergen,
                'reaction', a.reaction,
                'severity', a.severity
              )
              ORDER BY a.created_at
            )
            FROM callcare.patient_allergies a
            WHERE a.patient_id = p.id
              AND a.is_active = true
          ),
          '[]'::json
        ),
      'preferred_pharmacy',
        (
          SELECT json_build_object(
            'name', ph.name,
            'address_line_1', ph.address_line_1,
            'city', ph.city,
            'state', ph.state,
            'postal_code', ph.postal_code,
            'phone', ph.phone,
            'fax', ph.fax,
            'ncpdp_id', ph.ncpdp_id
          )
          FROM callcare.patient_pharmacies pp
          JOIN callcare.pharmacies ph
            ON ph.id = pp.pharmacy_id
          WHERE pp.patient_id = p.id
            AND pp.is_preferred = true
          ORDER BY ph.created_at DESC
          LIMIT 1
        )
    )
    FROM callcare.encounters e
    JOIN callcare.patients p
      ON p.id = e.patient_id
    WHERE e.call_sid = NULLIF(:'CALL_SID', '')
    ORDER BY e.started_at DESC
    LIMIT 1;
    """
    try:
        out = run_psql(sql, {"CALL_SID": call_sid})
        return json.loads(out) if out else {}
    except Exception:
        return {}


def packet_bundle(packet_path_obj: Path) -> Optional[Dict[str, Any]]:
    try:
        packet = load_json(packet_path_obj)
    except Exception:
        return None

    packet_id = safe_str(packet.get("packet_id") or packet_path_obj.stem)
    meta = load_meta(packet_id)
    call_sid = resolve_call_sid(packet_id, packet, meta)
    if call_sid and not safe_str(meta.get("call_sid")):
        meta["call_sid"] = call_sid
        save_meta(packet_id, meta)

    patient_ctx = lookup_patient_context(call_sid)
    call_log = load_call_log_by_sid(call_sid)
    spoken_summary = extract_spoken_summary_from_call_log(call_log)

    return {
        "packet_id": packet_id,
        "packet": packet,
        "meta": meta,
        "call_sid": call_sid,
        "patient_ctx": patient_ctx,
        "call_log": call_log,
        "spoken_summary": spoken_summary,
        "created_at": safe_str(packet.get("created_at")),
    }


def patient_groups() -> List[Dict[str, Any]]:
    groups: Dict[str, Dict[str, Any]] = {}

    for path in packet_files():
        bundle = packet_bundle(path)
        if not bundle:
            continue

        patient_ctx = bundle.get("patient_ctx") or {}
        chart_number = safe_str(patient_ctx.get("chart_number"))
        patient_name = safe_str(patient_ctx.get("patient_name"))

        if not chart_number:
            chart_number = f"UNLINKED::{bundle['packet_id']}"
        if not patient_name:
            patient_name = "Unknown patient"

        if chart_number not in groups:
            groups[chart_number] = {
                "chart_number": chart_number,
                "patient_name": patient_name,
                "patient_ctx": patient_ctx,
                "encounters": [],
            }

        groups[chart_number]["encounters"].append(bundle)

    for g in groups.values():
        g["encounters"].sort(
            key=lambda x: (
                safe_str((x.get("patient_ctx") or {}).get("encounter_started_at")),
                safe_str(x.get("created_at")),
            ),
            reverse=True,
        )

    return sorted(
        groups.values(),
        key=lambda g: safe_str((g["encounters"][0].get("patient_ctx") or {}).get("encounter_started_at"))
        or safe_str(g["encounters"][0].get("created_at")),
        reverse=True,
    )


def render_list_items(items: List[Dict[str, Any]], keys: List[str], empty_text: str) -> str:
    if not items:
        return f"<p>{html_escape(empty_text)}</p>"

    rendered: List[str] = []
    for item in items:
        parts = []
        for k in keys:
            val = safe_str(item.get(k))
            if val:
                parts.append(val)
        if parts:
            rendered.append(f"<li>{html_escape(' — '.join(parts))}</li>")

    if not rendered:
        return f"<p>{html_escape(empty_text)}</p>"

    return "<ul class='detail-list'>" + "".join(rendered) + "</ul>"


def render_pharmacy(ph: Dict[str, Any]) -> str:
    if not ph:
        return "<p>No preferred pharmacy on file.</p>"

    parts = [
        safe_str(ph.get("name")),
        safe_str(ph.get("address_line_1")),
        " ".join(
            x for x in [
                safe_str(ph.get("city")),
                safe_str(ph.get("state")),
                safe_str(ph.get("postal_code")),
            ] if x
        ).strip(),
        safe_str(ph.get("phone")),
        safe_str(ph.get("fax")),
        safe_str(ph.get("ncpdp_id")),
    ]
    parts = [p for p in parts if p]
    return "<ul class='detail-list'>" + "".join(f"<li>{html_escape(p)}</li>" for p in parts) + "</ul>"


def encounter_topic(chief_complaint: str) -> str:
    text = safe_str(chief_complaint).lower()
    if not text:
        return "encounter"

    patterns = [
        (r"abdominal pain|stomach pain|belly pain", "abdominal pain"),
        (r"rash|red swollen|crust", "rash"),
        (r"sore throat", "sore throat"),
        (r"cough", "cough"),
        (r"headache|migraine", "headache"),
        (r"ear pain|earache", "ear pain"),
        (r"back pain", "back pain"),
        (r"uti|urinary|burning with urination|dysuria", "urinary symptoms"),
        (r"sinus", "sinus symptoms"),
        (r"nausea|vomiting", "nausea/vomiting"),
        (r"diarrhea", "diarrhea"),
        (r"constipation", "constipation"),
        (r"tooth|dental", "tooth pain"),
        (r"anxiety|panic", "anxiety"),
        (r"insomnia|sleep", "insomnia"),
        (r"eye|red eye", "eye problem"),
        (r"asthma|wheeze|shortness of breath", "breathing problem"),
    ]

    for pattern, label in patterns:
        if re.search(pattern, text):
            return label

    words = re.findall(r"[a-zA-Z]+", text)
    if not words:
        return "encounter"
    return " ".join(words[:4])


def encounter_when(started: str, created: str) -> str:
    value = safe_str(started) or safe_str(created)
    value = value.replace("T", " ")
    return value[:16] if value else ""


def signature_line() -> str:
    creds = f", {PHYSICIAN_CREDENTIALS}" if PHYSICIAN_CREDENTIALS else ""
    return f"{PHYSICIAN_NAME}{creds}"


def signed_note_text(note_text: str, meta: Dict[str, Any]) -> str:
    text = safe_str(note_text)
    if not meta.get("signed"):
        return text

    signed_at = safe_str(meta.get("signed_at"))
    signed_by = safe_str(meta.get("signed_by")) or signature_line()
    stamp = f"\n\nSigned electronically by {signed_by} on {signed_at}"
    if stamp.strip() in text:
        return text
    return text + stamp


def addendum_block(addendum: Dict[str, Any]) -> str:
    text = safe_str(addendum.get("text"))
    signed_at = safe_str(addendum.get("signed_at"))
    signed_by = safe_str(addendum.get("signed_by")) or signature_line()
    return f"{text}\n\nSigned addendum by {signed_by} on {signed_at}"


def save_note_signed(packet_id: str) -> None:
    meta = load_meta(packet_id)
    if meta.get("signed"):
        return
    meta["signed"] = True
    meta["signed_at"] = now_iso()
    meta["signed_by"] = signature_line()
    meta["status"] = "completed"
    save_meta(packet_id, meta)


def add_signed_addendum(packet_id: str, text: str) -> None:
    meta = load_meta(packet_id)
    addenda = meta.get("addenda") or []
    addenda.append(
        {
            "text": safe_str(text),
            "signed_at": now_iso(),
            "signed_by": signature_line(),
        }
    )
    meta["addenda"] = addenda
    save_meta(packet_id, meta)


def verify_portal_login(first_name: str, last_name: str, dob: str, password: str) -> Optional[Dict[str, Any]]:
    sql = r"""
    SELECT json_build_object(
      'patient_id', p.id::text,
      'chart_number', p.chart_number,
      'patient_name', trim(concat_ws(' ', p.legal_first_name, p.legal_last_name)),
      'date_of_birth', p.date_of_birth::text
    )
    FROM callcare.patients p
    JOIN callcare.portal_accounts pa
      ON pa.patient_id = p.id
    WHERE lower(p.legal_first_name) = lower(NULLIF(:'FIRST_NAME', ''))
      AND lower(p.legal_last_name) = lower(NULLIF(:'LAST_NAME', ''))
      AND p.date_of_birth = NULLIF(:'DOB', '')::date
      AND pa.password_hash = crypt(:'PASSWORD', pa.password_hash)
      AND pa.is_active = true
      AND p.archived_at IS NULL
    LIMIT 1;
    """
    try:
        out = run_psql(
            sql,
            {
                "FIRST_NAME": first_name,
                "LAST_NAME": last_name,
                "DOB": dob,
                "PASSWORD": password,
            },
        )
        return json.loads(out) if out else None
    except Exception:
        return None


def signed_patient_group(chart_number: str) -> Optional[Dict[str, Any]]:
    groups = patient_groups()
    for g in groups:
        if g["chart_number"] == chart_number:
            signed_encounters = [e for e in g["encounters"] if (e.get("meta") or {}).get("signed")]
            out = dict(g)
            out["encounters"] = signed_encounters
            return out
    return None


def queue_or_send_new_note_email(patient_ctx: Dict[str, Any], chart_number: str, packet_id: str) -> Dict[str, Any]:
    to_email = safe_str(patient_ctx.get("email"))
    patient_name = safe_str(patient_ctx.get("patient_name")) or "Patient"

    payload = {
        "queued_at": now_iso(),
        "to_email": to_email,
        "patient_name": patient_name,
        "chart_number": chart_number,
        "packet_id": packet_id,
        "subject": "A new CallCare note is available",
        "body": (
            f"Hello {patient_name},\n\n"
            f"A new CallCare note is available in your patient portal.\n\n"
            f"Please log in to review your latest physician-reviewed note.\n"
        ),
        "sent": False,
        "send_method": "queued_only",
    }

    outbox_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{packet_id}.json"
    outbox_path = EMAIL_OUTBOX_DIR / outbox_name

    if not to_email:
        payload["error"] = "No patient email on file"
        save_json(outbox_path, payload)
        return payload

    if SMTP_HOST and SMTP_FROM:
        try:
            msg = EmailMessage()
            msg["Subject"] = payload["subject"]
            msg["From"] = SMTP_FROM
            msg["To"] = to_email
            msg.set_content(payload["body"])

            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
                if SMTP_USE_TLS:
                    smtp.starttls()
                if SMTP_USERNAME:
                    smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
                smtp.send_message(msg)

            payload["sent"] = True
            payload["send_method"] = "smtp"
        except Exception as e:
            payload["error"] = safe_str(e)

    save_json(outbox_path, payload)
    return payload


def make_session_token() -> str:
    return secrets.token_urlsafe(32)
