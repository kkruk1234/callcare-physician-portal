from __future__ import annotations

import os
import json
import psycopg
from psycopg.rows import dict_row
from typing import Optional

import requests
from fastapi import FastAPI, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from app.portal.portal_common import (
    add_signed_addendum,
    addendum_block,
    encounter_topic,
    encounter_when,
    html_escape,
    load_json,
    load_meta,
    packet_bundle,
    packet_path,
    patient_groups,
    queue_or_send_new_note_email,
    render_list_items,
    render_pharmacy,
    save_json,
    save_meta,
    safe_str,
    save_note_signed,
    signed_note_text,
)

app = FastAPI(title="CallCare Physician Portal")

CALLCARE_PUBLIC_BASE_URL = os.getenv("CALLCARE_PUBLIC_BASE_URL", "https://callcare.healthcare").rstrip("/")


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
            s = safe_str(line)
            if s.startswith("1."):
                s = s[2:].strip()
                if s:
                    s = s[:1].lower() + s[1:]
                    return "Possible " + s

    f = safe_str(fallback).strip().rstrip(".")
    for prefix in ("i have ", "i'm having ", "im having ", "i am having ", "my "):
        if f.lower().startswith(prefix):
            f = f[len(prefix):].strip()
            break
    if not f:
        return "Encounter"
    return f[:1].upper() + f[1:]

def _shared_db_url() -> str:
    return os.getenv("CALLCARE_SHARED_DATABASE_URL", "").strip()


def _shared_lookup_patient_id(chart_number: str):
    url = _shared_db_url()
    if not url or not chart_number:
        return None
    with psycopg.connect(url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id::text AS patient_id FROM callcare.patients WHERE chart_number = %s LIMIT 1",
                (chart_number,),
            )
            row = cur.fetchone()
            return row["patient_id"] if row else None


def _lookup_shared_patient_by_call_sid(call_sid: str):
    url = _shared_db_url()
    if not url or not call_sid:
        return None
    with psycopg.connect(url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  p.id::text AS patient_id,
                  p.chart_number,
                  e.chief_complaint
                FROM callcare.encounters e
                JOIN callcare.patients p
                  ON p.id = e.patient_id
                WHERE e.call_sid = %s
                ORDER BY e.started_at DESC
                LIMIT 1
                """,
                (call_sid,),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def _sync_bundle_to_shared_db(bundle: dict) -> None:
    url = _shared_db_url()
    if not url or not bundle:
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
            stored_meta = load_meta(packet_id)
            call_sid = safe_str(stored_meta.get("call_sid"))
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

    spoken_summary = safe_str(bundle.get("spoken_summary"))

    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO callcare.portal_packets (
                    packet_id,
                    chart_number,
                    patient_id,
                    call_sid,
                    session_id,
                    pathway_id,
                    created_at,
                    chief_complaint,
                    note_text,
                    spoken_summary,
                    spoken_summary_comments,
                    status,
                    prescription_status,
                    note_sent,
                    signed,
                    signed_at,
                    signed_by,
                    addenda,
                    updated_at
                )
                VALUES (
                    %s,
                    %s,
                    %s::uuid,
                    NULLIF(%s, ''),
                    NULLIF(%s, ''),
                    NULLIF(%s, ''),
                    %s::timestamptz,
                    NULLIF(%s, ''),
                    NULLIF(%s, ''),
                    COALESCE(NULLIF(%s, ''), ''),
                    COALESCE(NULLIF(%s, ''), ''),
                    COALESCE(NULLIF(%s, ''), 'active'),
                    COALESCE(NULLIF(%s, ''), 'under review'),
                    COALESCE(NULLIF(%s, ''), 'to be mailed'),
                    %s,
                    NULLIF(%s, '')::timestamptz,
                    COALESCE(NULLIF(%s, ''), ''),
                    %s::jsonb,
                    now()
                )
                ON CONFLICT (packet_id) DO UPDATE
                SET
                    chart_number = EXCLUDED.chart_number,
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
                    spoken_summary,
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


def _send_email_resend(to_email: str, subject: str, body: str):
    api_key = os.getenv("CALLCARE_RESEND_API_KEY", "").strip()
    if not api_key:
        return False, "Missing RESEND API key"

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": "CallCare <onboarding@resend.dev>",
                "to": [to_email],
                "subject": subject,
                "text": body,
            },
            timeout=20,
        )
        if 200 <= resp.status_code < 300:
            return True, None
        return False, resp.text
    except Exception as e:
        return False, str(e)


def _queue_or_send_new_note_email_resend(packet_id: str, patient_ctx: dict, reason: str = "note_ready"):
    import json
    from pathlib import Path
    from datetime import datetime, timezone

    outbox_dir = Path("logs") / "email_outbox"
    outbox_dir.mkdir(parents=True, exist_ok=True)

    to_email = safe_str(patient_ctx.get("email"))
    patient_name = safe_str(patient_ctx.get("patient_name")) or "Patient"
    chart_number = safe_str(patient_ctx.get("chart_number"))
    portal_url = f"{CALLCARE_PUBLIC_BASE_URL}/portal/login"

    if reason == "addendum":
        subject = "A CallCare note was updated"
        plain = (
            f"Hello {patient_name},\n\n"
            f"A physician updated a note in your CallCare patient portal.\n\n"
            f"Please log in to review the updated note:\n{portal_url}\n"
        )
        headline = "A physician updated your note"
        body_line = "A physician made a change to a note in your CallCare patient portal."
    else:
        subject = "A new CallCare note is available"
        plain = (
            f"Hello {patient_name},\n\n"
            f"A new CallCare note is available in your CallCare patient portal.\n\n"
            f"Please log in to review it:\n{portal_url}\n"
        )
        headline = "A new CallCare note is available"
        body_line = "A new physician-reviewed note is ready in your CallCare patient portal."

    html_body = f"""
    <html>
      <body style="margin:0;padding:0;background:#eef7f5;font-family:Arial,sans-serif;color:#173430;">
        <div style="max-width:620px;margin:32px auto;padding:0 16px;">
          <div style="background:linear-gradient(135deg,#1f8f80,#67b9ae);color:white;border-radius:24px;padding:30px 32px;">
            <div style="font-size:38px;font-weight:800;letter-spacing:-0.03em;">CallCare</div>
            <div style="margin-top:10px;font-size:16px;line-height:1.55;">Telephone-first medical care for rural communities.</div>
          </div>

          <div style="background:white;border-radius:24px;padding:30px 32px;margin-top:18px;border:1px solid #d7e7e3;box-shadow:0 10px 30px rgba(18,60,55,0.08);">
            <div style="font-size:25px;font-weight:700;color:#163133;">{headline}</div>

            <p style="font-size:16px;line-height:1.65;margin-top:18px;">Hello {html_escape(patient_name)},</p>

            <p style="font-size:16px;line-height:1.65;">{body_line}</p>

            <div style="margin-top:24px;">
              <a href="{portal_url}" style="display:inline-block;background:linear-gradient(135deg,#1f8f80,#67b9ae);color:white;text-decoration:none;font-weight:700;padding:14px 18px;border-radius:12px;">
                Go to Patient Portal
              </a>
            </div>

            <p style="font-size:14px;line-height:1.65;color:#47655f;margin-top:22px;">
              Direct link: <a href="{portal_url}" style="color:#1f8f80;">{portal_url}</a>
            </p>
          </div>
        </div>
      <script>
        (function() {{
          const note = document.getElementById("note_text_editor");
          const summary = document.getElementById("spoken_summary_comments_editor");
          const packetMatch = window.location.search.match(/packet_id=([^&]+)/);
          const pathMatch = window.location.pathname.match(/\/packet\/([^/]+)/);
          const packetId = packetMatch ? decodeURIComponent(packetMatch[1]) : (pathMatch ? decodeURIComponent(pathMatch[1]) : "");
          if (!packetId) return;

          const noteKey = "callcare_note_draft_" + packetId;
          const summaryKey = "callcare_summary_draft_" + packetId;

          if (note) {{
            const savedNote = localStorage.getItem(noteKey);
            if (savedNote !== null) note.value = savedNote;
            note.addEventListener("input", function() {{
              localStorage.setItem(noteKey, note.value);
            }});
            const noteForm = note.closest("form");
            if (noteForm) {{
              noteForm.addEventListener("submit", function() {{
                localStorage.removeItem(noteKey);
              }});
            }}
          }}

          if (summary) {{
            const savedSummary = localStorage.getItem(summaryKey);
            if (savedSummary !== null) summary.value = savedSummary;
            summary.addEventListener("input", function() {{
              localStorage.setItem(summaryKey, summary.value);
            }});
            const summaryForm = summary.closest("form");
            if (summaryForm) {{
              summaryForm.addEventListener("submit", function() {{
                localStorage.removeItem(summaryKey);
              }});
            }}
          }}
        }})();
      </script>
      </body>
    </html>
    """

    payload = {
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "to_email": to_email,
        "patient_name": patient_name,
        "chart_number": chart_number,
        "packet_id": packet_id,
        "subject": subject,
        "body": plain,
        "html_body": html_body,
        "reason": reason,
        "sent": False,
        "send_method": "queued_only",
    }

    if not to_email:
        payload["error"] = "No patient email on file"
    else:
        provider = os.getenv("CALLCARE_EMAIL_PROVIDER", "").strip().lower()
        if provider == "resend":
            api_key = os.getenv("CALLCARE_RESEND_API_KEY", "").strip()
            if not api_key:
                payload["error"] = "Missing RESEND API key"
            else:
                try:
                    resp = requests.post(
                        "https://api.resend.com/emails",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "from": "CallCare <onboarding@resend.dev>",
                            "to": [to_email],
                            "subject": subject,
                            "text": plain,
                            "html": html_body,
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
    outbox_path = outbox_dir / outbox_name
    outbox_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload

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
          .list-card {{ overflow: hidden; }}
          table {{ width: 100%; border-collapse: collapse; }}
          th, td {{ padding: 14px 12px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
          th {{ color: var(--muted); font-weight: 600; font-size: 13px; text-transform: uppercase; letter-spacing: 0.04em; }}
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
          .pill {{
            display: inline-block;
            padding: 8px 12px;
            border-radius: 999px;
            background: #eef8f7;
            border: 1px solid var(--line);
            font-size: 13px;
          }}
          .metric {{
            background: #f8fcfc;
            border: 1px solid var(--line);
            border-radius: 18px;
            padding: 14px 16px;
          }}
          .metric .label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; }}
          .metric .value {{ margin-top: 6px; font-size: 16px; font-weight: 600; }}
          textarea {{
            width: 100%;
            min-height: 300px;
            border: 1px solid var(--line);
            border-radius: 18px;
            padding: 16px;
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
            font-size: 14px;
            background: #fbfdfd;
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
            font-weight: 600;
            cursor: pointer;
            box-shadow: 0 8px 18px rgba(29,143,138,0.18);
          }}
          .btn-soft {{
            background: #eef8f7;
            color: var(--ink);
            border: 1px solid var(--line);
            box-shadow: none;
          }}
          .detail-list {{ margin: 0; padding-left: 18px; }}
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


@app.get("/healthz")
async def healthz() -> PlainTextResponse:
    return PlainTextResponse("ok")


@app.get("/packet/{packet_id}", response_class=HTMLResponse)
async def legacy_packet_redirect(packet_id: str) -> RedirectResponse:
    path = packet_path(packet_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Packet not found")

    bundle = packet_bundle(path)
    if not bundle:
        raise HTTPException(status_code=404, detail="Packet bundle not found")

    chart_number = safe_str((bundle.get("patient_ctx") or {}).get("chart_number"))
    if not chart_number:
        raise HTTPException(status_code=404, detail="Patient chart not linked")

    return RedirectResponse(
        url=f"/patient/{chart_number}?packet_id={packet_id}&tab=encounters",
        status_code=303,
    )


@app.get("/", response_class=HTMLResponse)
async def home() -> str:
    groups = patient_groups()

    if not groups:
        return shell(
            "CallCare Physician Portal",
            """
            <div class="hero">
              <h1>CallCare Physician Portal</h1>
              <p>No routed review packets yet.</p>
            </div>
            """,
        )

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
            f"<td>{html_escape(safe_str((patient_ctx or {}).get('chief_complaint')))}</td>"
            f"<td>{html_escape(safe_str(meta.get('status')))}</td>"
            f"<td>{html_escape(safe_str(meta.get('prescription_status')))}</td>"
            f"</tr>"
        )

    return shell(
        "CallCare Physician Portal",
        f"""
        <div class="hero">
          <h1>CallCare Physician Portal</h1>
          <p>Physician review queue with linked patient charts, signed notes, addenda, and delivery tracking.</p>
        </div>

        <div class="card list-card">
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
            <tbody>
              {''.join(rows)}
            </tbody>
          </table>
        </div>
        """,
    )


@app.get("/patient/{chart_number}", response_class=HTMLResponse)
async def patient_chart(
    chart_number: str,
    packet_id: Optional[str] = Query(default=None),
    tab: str = Query(default="encounters"),
) -> str:
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
    for idx, enc in enumerate(encounters, 1):
        enc_ctx = enc.get("patient_ctx") or {}
        label = _extract_differential_title(safe_str((enc.get("packet") or {}).get("note_text")), safe_str(enc_ctx.get("chief_complaint")))
        started = encounter_when(safe_str(enc_ctx.get("encounter_started_at")), safe_str(enc.get("created_at")))
        active_class = "enc-link active" if enc["packet_id"] == selected_packet_id else "enc-link"
        encounter_tab_links.append(
            f"<li><a class='{active_class}' href='/patient/{html_escape(chart_number)}?packet_id={html_escape(enc['packet_id'])}&tab=encounters'>{html_escape(label)} — {html_escape(started)}</a></li>"
        )
    encounter_tab_html = "<ul>" + "".join(encounter_tab_links) + "</ul>"

    allergies_html = render_list_items(
        patient_ctx.get("allergies") or [],
        ["allergen", "reaction", "severity"],
        "No allergy data on file.",
    )
    conditions_html = render_list_items(
        patient_ctx.get("conditions") or [],
        ["condition_name", "status"],
        "No past medical history on file.",
    )
    social_html = render_list_items(
        patient_ctx.get("social_history") or [],
        ["domain", "value_text"],
        "No social history on file.",
    )
    pharmacy_html = render_pharmacy(patient_ctx.get("preferred_pharmacy") or {})

    demographics_panel = f"""
      <div class="card">
        <h2 class="section-title">Demographics</h2>
        <div class="meta-grid">
          <div class="metric"><div class="label">Patient</div><div class="value">{html_escape(patient_ctx.get('patient_name'))}</div></div>
          <div class="metric"><div class="label">Chart #</div><div class="value">{html_escape(patient_ctx.get('chart_number'))}</div></div>
          <div class="metric"><div class="label">Date of Birth</div><div class="value">{html_escape(patient_ctx.get('date_of_birth'))}</div></div>
          <div class="metric"><div class="label">Sex at Birth</div><div class="value">{html_escape(patient_ctx.get('sex_at_birth'))}</div></div>
          <div class="metric"><div class="label">Phone</div><div class="value">{html_escape(patient_ctx.get('phone_number'))}</div></div>
          <div class="metric"><div class="label">Email</div><div class="value">{html_escape(patient_ctx.get('email'))}</div></div>
        </div>
      </div>
      <div class="card">
        <h2 class="section-title">Preferred Pharmacy</h2>
        {pharmacy_html}
      </div>
    """

    pmh_panel = f"""
      <div class="card">
        <h2 class="section-title">Past Medical History</h2>
        {conditions_html}
      </div>
      <div class="card">
        <h2 class="section-title">Allergies</h2>
        {allergies_html}
      </div>
    """

    social_panel = f"""
      <div class="card">
        <h2 class="section-title">Past Social History</h2>
        {social_html}
      </div>
    """

    note_editor_html = (
        f"""
        <form method="post" action="/packet/{html_escape(selected_packet_id)}/update-note">
          <textarea id="note_text_editor" name="note_text">{html_escape(selected_note)}</textarea>
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
          <form method="post" action="/packet/{html_escape(selected_packet_id)}/prescribe">
            <button type="submit">Send Prescription</button>
          </form>
          <form method="post" action="/packet/{html_escape(selected_packet_id)}/note-sent/to-be-mailed">
            <button type="submit">Mark Note To Be Mailed</button>
          </form>
        </div>
      </div>
    """

    panel_html = {
        "demographics": demographics_panel,
        "pmh": pmh_panel,
        "social": social_panel,
        "encounters": encounter_panel,
    }.get(tab, encounter_panel)

    def tab_link(tab_name: str, label: str) -> str:
        active = "tab active" if tab == tab_name else "tab"
        return (
            f"<a class='{active}' href='/patient/{html_escape(chart_number)}?packet_id={html_escape(selected_packet_id)}&tab={html_escape(tab_name)}'>{html_escape(label)}</a>"
        )

    _sync_bundle_to_shared_db(selected_bundle)

    return shell(
        f"{safe_str(patient_ctx.get('patient_name'))} - CallCare Physician Portal",
        f"""
        <div class="hero">
          <h1>{html_escape(patient_ctx.get('patient_name'))}</h1>
          <p>Chart #{html_escape(patient_ctx.get('chart_number'))} · Physician review workspace</p>
        </div>

        <p><a href="/">← Back to patient list</a></p>

        <div class="tabs">
          {tab_link("demographics", "Demographics + Pharmacy")}
          {tab_link("pmh", "Past Medical History")}
          {tab_link("social", "Social History")}
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


@app.get("/packet/{packet_id}/full-text", response_class=HTMLResponse)
async def full_text(packet_id: str) -> str:
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
async def update_note(packet_id: str, note_text: str = Form(...)) -> RedirectResponse:
    path = packet_path(packet_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Packet not found")

    meta = load_meta(packet_id)
    if meta.get("signed"):
        raise HTTPException(status_code=400, detail="Signed notes are read-only")

    d = load_json(path)
    d["note_text"] = safe_str(note_text)
    save_json(path, d)

    bundle = packet_bundle(path)
    if bundle:
        _sync_bundle_to_shared_db(bundle)
        patient_ctx = bundle.get("patient_ctx") or {}
        _queue_or_send_new_note_email_resend(packet_id, patient_ctx)
    chart_number = safe_str((bundle.get("patient_ctx") or {}).get("chart_number")) if bundle else ""
    return RedirectResponse(url=f"/patient/{chart_number}?packet_id={packet_id}&tab=encounters", status_code=303)


@app.post("/packet/{packet_id}/update-spoken-summary-comments")
async def update_spoken_summary_comments(packet_id: str, spoken_summary_comments: str = Form(...)) -> RedirectResponse:
    path = packet_path(packet_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Packet not found")

    meta = load_meta(packet_id)
    meta["spoken_summary_comments"] = safe_str(spoken_summary_comments)
    save_meta(packet_id, meta)

    bundle = packet_bundle(path)
    if bundle:
        _sync_bundle_to_shared_db(bundle)
        patient_ctx = bundle.get("patient_ctx") or {}
        _queue_or_send_new_note_email_resend(packet_id, patient_ctx)
    chart_number = safe_str((bundle.get("patient_ctx") or {}).get("chart_number")) if bundle else ""
    return RedirectResponse(url=f"/patient/{chart_number}?packet_id={packet_id}&tab=encounters", status_code=303)


@app.post("/packet/{packet_id}/sign")
async def sign_note(packet_id: str) -> RedirectResponse:
    path = packet_path(packet_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Packet not found")

    save_note_signed(packet_id)

    bundle = packet_bundle(path)
    if bundle:
        _sync_bundle_to_shared_db(bundle)
        patient_ctx = bundle.get("patient_ctx") or {}
        _queue_or_send_new_note_email_resend(packet_id, patient_ctx, reason="note_ready")
    chart_number = safe_str((bundle.get("patient_ctx") or {}).get("chart_number")) if bundle else ""
    return RedirectResponse(url=f"/patient/{chart_number}?packet_id={packet_id}&tab=encounters", status_code=303)


@app.post("/packet/{packet_id}/addendum")
async def sign_addendum(packet_id: str, addendum_text: str = Form(...)) -> RedirectResponse:
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
        patient_ctx = bundle.get("patient_ctx") or {}
        _queue_or_send_new_note_email_resend(packet_id, patient_ctx, reason="addendum")
    chart_number = safe_str((bundle.get("patient_ctx") or {}).get("chart_number")) if bundle else ""
    return RedirectResponse(url=f"/patient/{chart_number}?packet_id={packet_id}&tab=encounters", status_code=303)


@app.post("/packet/{packet_id}/prescribe")
async def send_prescription(packet_id: str) -> RedirectResponse:
    path = packet_path(packet_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Packet not found")

    meta = load_meta(packet_id)
    meta["prescription_status"] = "sent"
    save_meta(packet_id, meta)

    bundle = packet_bundle(path)
    if bundle:
        _sync_bundle_to_shared_db(bundle)
        patient_ctx = bundle.get("patient_ctx") or {}
        _queue_or_send_new_note_email_resend(packet_id, patient_ctx)
    chart_number = safe_str((bundle.get("patient_ctx") or {}).get("chart_number")) if bundle else ""
    return RedirectResponse(url=f"/patient/{chart_number}?packet_id={packet_id}&tab=encounters", status_code=303)


@app.post("/packet/{packet_id}/note-sent/{mode}")
async def note_sent(packet_id: str, mode: str) -> RedirectResponse:
    path = packet_path(packet_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Packet not found")

    normalized = safe_str(mode).lower()
    if normalized not in {"emailed", "to-be-mailed", "to_be_mailed", "to be mailed"}:
        raise HTTPException(status_code=400, detail="Invalid note-sent mode")

    meta = load_meta(packet_id)
    bundle = packet_bundle(path)

    if normalized == "emailed":
        patient_ctx = (bundle or {}).get("patient_ctx") or {}
        result = _queue_or_send_new_note_email_resend(packet_id, patient_ctx)
        meta["note_sent"] = "emailed"
        meta["email_last_queued_at"] = safe_str(result.get("queued_at"))
    else:
        meta["note_sent"] = "to be mailed"

    save_meta(packet_id, meta)

    chart_number = safe_str(((bundle or {}).get("patient_ctx") or {}).get("chart_number"))
    return RedirectResponse(url=f"/patient/{chart_number}?packet_id={packet_id}&tab=encounters", status_code=303)
