from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import psycopg
import requests
from psycopg.rows import dict_row
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

app = FastAPI(title="CallCare Physician Portal")

CALLCARE_SHARED_DATABASE_URL = os.getenv("CALLCARE_SHARED_DATABASE_URL", "").strip()
CALLCARE_PUBLIC_BASE_URL = os.getenv("CALLCARE_PUBLIC_BASE_URL", "https://callcare.healthcare").rstrip("/")
CALLCARE_EMAIL_PROVIDER = os.getenv("CALLCARE_EMAIL_PROVIDER", "").strip().lower()
CALLCARE_RESEND_API_KEY = os.getenv("CALLCARE_RESEND_API_KEY", "").strip()
CALLCARE_PHYSICIAN_USERNAME = os.getenv("CALLCARE_PHYSICIAN_USERNAME", "").strip()
CALLCARE_PHYSICIAN_PASSWORD = os.getenv("CALLCARE_PHYSICIAN_PASSWORD", "").strip()

SESSIONS: Dict[str, Dict[str, str]] = {}


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


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_conn():
    if not CALLCARE_SHARED_DATABASE_URL:
        raise RuntimeError("CALLCARE_SHARED_DATABASE_URL is not set")
    return psycopg.connect(CALLCARE_SHARED_DATABASE_URL, row_factory=dict_row)


def query_all(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            return [dict(r) for r in rows]


def query_one(sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
    rows = query_all(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple = ()) -> None:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()


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


PORTAL_TIMEZONE = ZoneInfo("America/New_York")


def format_portal_time(value: Any) -> str:
    text = safe_str(value)
    if not text:
        return ""

    normalized = text.replace("T", " ").replace("Z", "+00:00")

    try:
        dt = datetime.fromisoformat(normalized)
    except Exception:
        try:
            dt = datetime.strptime(normalized[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return text.split(".")[0]

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(PORTAL_TIMEZONE).strftime("%Y-%m-%d %I:%M:%S %p %Z")


def signed_note_text(note_text: str, meta: Dict[str, Any]) -> str:
    text = safe_str(note_text)
    if not meta.get("signed"):
        return text
    signed_at = format_portal_time(meta.get("signed_at"))
    signed_by = safe_str(meta.get("signed_by"))
    stamp = f"\n\nSigned electronically by {signed_by} on {signed_at}"
    existing_prefix = f"Signed electronically by {signed_by} on "
    if existing_prefix in text:
        return text
    return text + stamp


def addendum_block(addendum: Dict[str, Any]) -> str:
    text = safe_str(addendum.get("text"))
    signed_at = format_portal_time(addendum.get("signed_at"))
    signed_by = safe_str(addendum.get("signed_by"))
    return f"{text}\n\nSigned addendum by {signed_by} on {signed_at}"


def encounter_when(dt_text: str) -> str:
    return format_portal_time(dt_text)


def render_list_items(items: List[Dict[str, Any]], keys: List[str], empty_text: str) -> str:
    if not items:
        return f"<p>{html_escape(empty_text)}</p>"
    rendered = []
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


def render_pharmacy(ph: Optional[Dict[str, Any]]) -> str:
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


def extract_encounter_label(note_text: str, fallback: str) -> str:
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


def patient_groups() -> List[Dict[str, Any]]:
    sql = """
    WITH latest AS (
      SELECT DISTINCT ON (pp.chart_number)
        pp.chart_number,
        pp.packet_id,
        pp.created_at,
        pp.chief_complaint,
        pp.note_text,
        pp.status,
        pp.prescription_status,
        pp.note_sent,
        p.legal_first_name,
        p.legal_last_name
      FROM callcare.portal_packets pp
      JOIN callcare.patients p
        ON p.id = pp.patient_id
      ORDER BY pp.chart_number, pp.created_at DESC
    )
    SELECT
      l.chart_number,
      trim(concat_ws(' ', l.legal_first_name, l.legal_last_name)) AS patient_name,
      l.packet_id,
      l.created_at::text AS created_at,
      l.chief_complaint,
      l.note_text,
      l.status,
      l.prescription_status,
      l.note_sent
    FROM latest l
    ORDER BY l.created_at DESC;
    """
    rows = query_all(sql)
    groups: List[Dict[str, Any]] = []
    for row in rows:
        groups.append(
            {
                "chart_number": safe_str(row.get("chart_number")),
                "patient_name": safe_str(row.get("patient_name")),
                "encounters": [
                    {
                        "packet_id": safe_str(row.get("packet_id")),
                        "created_at": safe_str(row.get("created_at")),
                        "packet": {
                            "packet_id": safe_str(row.get("packet_id")),
                            "note_text": safe_str(row.get("note_text")),
                            "created_at": safe_str(row.get("created_at")),
                        },
                        "meta": {
                            "status": safe_str(row.get("status")),
                            "prescription_status": safe_str(row.get("prescription_status")),
                            "note_sent": safe_str(row.get("note_sent")),
                        },
                        "patient_ctx": {
                            "chart_number": safe_str(row.get("chart_number")),
                            "patient_name": safe_str(row.get("patient_name")),
                            "chief_complaint": safe_str(row.get("chief_complaint")),
                        },
                    }
                ],
            }
        )
    return groups


def get_patient_context(chart_number: str) -> Optional[Dict[str, Any]]:
    sql = """
    SELECT
      p.id::text AS patient_id,
      p.chart_number,
      trim(concat_ws(' ', p.legal_first_name, p.legal_last_name)) AS patient_name,
      p.date_of_birth::text AS date_of_birth,
      p.sex_at_birth,
      p.phone_number,
      p.email
    FROM callcare.patients p
    WHERE p.chart_number = %s
    LIMIT 1;
    """
    ctx = query_one(sql, (chart_number,))
    if not ctx:
        return None

    patient_id = safe_str(ctx.get("patient_id"))

    ph_sql = """
    SELECT
      ph.name,
      ph.address_line_1,
      ph.city,
      ph.state,
      ph.postal_code,
      ph.phone,
      ph.fax,
      ph.ncpdp_id
    FROM callcare.patient_pharmacies pp
    JOIN callcare.pharmacies ph
      ON ph.id = pp.pharmacy_id
    WHERE pp.patient_id = %s::uuid
      AND pp.is_preferred = true
    ORDER BY ph.created_at DESC
    LIMIT 1;
    """
    ctx["preferred_pharmacy"] = query_one(ph_sql, (patient_id,))

    allergies_sql = """
    SELECT allergen, reaction, severity
    FROM callcare.patient_allergies
    WHERE patient_id = %s::uuid
      AND is_active = true
    ORDER BY created_at;
    """
    ctx["allergies"] = query_all(allergies_sql, (patient_id,))

    conditions_sql = """
    SELECT condition_name, status
    FROM callcare.patient_conditions
    WHERE patient_id = %s::uuid
    ORDER BY created_at;
    """
    ctx["conditions"] = query_all(conditions_sql, (patient_id,))

    social_sql = """
    SELECT domain, value_text
    FROM callcare.patient_social_history
    WHERE patient_id = %s::uuid
    ORDER BY created_at;
    """
    ctx["social_history"] = query_all(social_sql, (patient_id,))

    return ctx


def get_encounters(chart_number: str) -> List[Dict[str, Any]]:
    patient_ctx = get_patient_context(chart_number)
    if not patient_ctx:
        return []

    sql = """
    SELECT
      packet_id,
      chart_number,
      created_at::text AS created_at,
      chief_complaint,
      note_text,
      spoken_summary,
      COALESCE(spoken_summary_comments, '') AS spoken_summary_comments,
      status,
      prescription_status,
      note_sent,
      signed,
      signed_at::text AS signed_at,
      signed_by,
      COALESCE(addenda, '[]'::jsonb) AS addenda
    FROM callcare.portal_packets
    WHERE chart_number = %s
    ORDER BY created_at DESC;
    """
    rows = query_all(sql, (chart_number,))
    out: List[Dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "packet_id": safe_str(row.get("packet_id")),
                "created_at": safe_str(row.get("created_at")),
                "spoken_summary": safe_str(row.get("spoken_summary")),
                "packet": {
                    "packet_id": safe_str(row.get("packet_id")),
                    "note_text": safe_str(row.get("note_text")),
                    "created_at": safe_str(row.get("created_at")),
                },
                "meta": {
                    "signed": bool(row.get("signed")),
                    "signed_at": safe_str(row.get("signed_at")),
                    "signed_by": safe_str(row.get("signed_by")),
                    "status": safe_str(row.get("status")),
                    "prescription_status": safe_str(row.get("prescription_status")),
                    "note_sent": safe_str(row.get("note_sent")),
                    "spoken_summary_comments": safe_str(row.get("spoken_summary_comments")),
                    "addenda": row.get("addenda") or [],
                },
                "patient_ctx": {
                    **patient_ctx,
                    "chief_complaint": safe_str(row.get("chief_complaint")),
                    "encounter_started_at": safe_str(row.get("created_at")),
                },
            }
        )
    return out


def queue_or_send_new_note_email(packet_id: str, patient_ctx: dict, reason: str = "note_ready") -> dict:
    to_email = safe_str(patient_ctx.get("email"))
    patient_name = safe_str(patient_ctx.get("patient_name")) or "Patient"
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
      </body>
    </html>
    """

    payload = {"sent": False}

    if not to_email:
        payload["error"] = "No patient email on file"
        return payload

    if CALLCARE_EMAIL_PROVIDER == "resend":
        if not CALLCARE_RESEND_API_KEY:
            payload["error"] = "Missing RESEND API key"
            return payload
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
                    "html": html_body,
                },
                timeout=20,
            )
            if 200 <= resp.status_code < 300:
                payload["sent"] = True
            else:
                payload["error"] = safe_str(resp.text)
        except Exception as e:
            payload["error"] = safe_str(e)
    else:
        payload["error"] = "Unsupported email provider"
    return payload


def shell(title: str, body: str) -> str:
    template = """
    <html>
      <head>
        <title>{title}</title>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
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
          .detail-list {{ margin: 0; padding-left: 18px; }}
          .login-card {{ max-width: 520px; margin: 80px auto 0 auto; }}
          input {{
            width: 100%;
            padding: 12px;
            border-radius: 12px;
            border: 1px solid var(--line);
            margin-top: 6px;
            background: rgba(255,255,255,0.97);
          }}
          label {{ display: block; margin-top: 12px; font-weight: 700; }}
          @media (max-width: 980px) {{
            .layout {{ grid-template-columns: 1fr; }}
          }}
        </style>
      </head>
      <body>
        <div class="wrap">
          {body}
        </div>
        <script>
          (function() {{
            const note = document.getElementById("note_text_editor");
            const summary = document.getElementById("spoken_summary_comments_editor");
            const packetInput = document.getElementById("current_packet_id");
            const packetId = packetInput ? packetInput.value : "";
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
    return template.format(title=html_escape(title), body=body)


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
          <p>Secure physician access</p>
        </div>

        <div class="card login-card">
          <h2 style="margin-top:0;">Log In</h2>
          <form method="post" action="/login" autocomplete="off">
            <label>Username</label>
            <input name="username" />
            <label>Password</label>
            <input name="password" type="password" />
            <div class="btnbar">
              <button type="submit">Log In</button>
            </div>
          </form>
        </div>
        """,
    )


@app.post("/login")
async def login(username: str = Form(...), password: str = Form(...)) -> RedirectResponse:
    if not CALLCARE_PHYSICIAN_USERNAME or not CALLCARE_PHYSICIAN_PASSWORD:
        raise HTTPException(status_code=500, detail="Physician credentials are not configured")
    if username != CALLCARE_PHYSICIAN_USERNAME or password != CALLCARE_PHYSICIAN_PASSWORD:
        return RedirectResponse(url="/login", status_code=303)

    token = make_session_token()
    SESSIONS[token] = {"username": username, "created_at": now_iso()}
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie("callcare_physician_session", token, httponly=True, samesite="lax", secure=True, path="/")
    return response


@app.get("/logout")
async def logout(request: Request) -> RedirectResponse:
    token = request.cookies.get("callcare_physician_session", "")
    if token and token in SESSIONS:
        del SESSIONS[token]
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("callcare_physician_session", path="/")
    return response


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> str:
    require_session(request)
    groups = patient_groups()

    if not groups:
        return shell(
            "CallCare Physician Portal",
            """
            <div class="hero">
              <h1>CallCare Physician Portal</h1>
              <p>No routed review packets yet.</p>
            </div>
            <p><a href="/logout">Log out</a></p>
            """,
        )

    rows = []
    for g in groups:
        latest = g["encounters"][0]
        patient_ctx = latest["patient_ctx"] or {}
        packet = latest["packet"] or {}
        meta = latest["meta"] or {}
        label = extract_encounter_label(
            safe_str(packet.get("note_text")),
            safe_str(patient_ctx.get("chief_complaint")),
        )
        rows.append(
            f"<tr>"
            f"<td><a href='/patient/{html_escape(g['chart_number'])}'>{html_escape(g['patient_name'])}</a></td>"
            f"<td>{html_escape(g['chart_number'])}</td>"
            f"<td>{html_escape(label)}</td>"
            f"<td>{html_escape(encounter_when(safe_str(latest.get('created_at'))))}</td>"
            f"<td>{html_escape(safe_str(meta.get('status')))}</td>"
            f"<td>{html_escape(safe_str(meta.get('prescription_status')))}</td>"
            f"</tr>"
        )

    return shell(
        "CallCare Physician Portal",
        f"""
        <div class="hero">
          <h1>CallCare Physician Portal</h1>
          <p>Physician review workspace</p>
        </div>

        <p><a href="/logout">Log out</a></p>

        <div class="card list-card">
          <table>
            <thead>
              <tr>
                <th>Patient</th>
                <th>Chart #</th>
                <th>Encounter</th>
                <th>Date / Time</th>
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
    request: Request,
    packet_id: Optional[str] = Query(default=None),
    tab: str = Query(default="encounters"),
) -> str:
    require_session(request)

    patient_ctx = get_patient_context(chart_number)
    if not patient_ctx:
        raise HTTPException(status_code=404, detail="Patient chart not found")

    encounters = get_encounters(chart_number)
    if not encounters:
        raise HTTPException(status_code=404, detail="No encounters found")

    selected_bundle = None
    if packet_id:
        for enc in encounters:
            if safe_str(enc.get("packet_id")) == safe_str(packet_id):
                selected_bundle = enc
                break
    if not selected_bundle:
        selected_bundle = encounters[0]

    selected_packet_id = safe_str(selected_bundle.get("packet_id"))
    selected_packet = selected_bundle.get("packet") or {}
    selected_meta = selected_bundle.get("meta") or {}
    selected_note = safe_str(selected_packet.get("note_text"))
    selected_signed_note = signed_note_text(selected_note, selected_meta)
    selected_spoken_summary = safe_str(selected_bundle.get("spoken_summary"))

    encounter_tab_links = []
    for enc in encounters:
        enc_ctx = enc.get("patient_ctx") or {}
        label = extract_encounter_label(
            safe_str((enc.get("packet") or {}).get("note_text")),
            safe_str(enc_ctx.get("chief_complaint")),
        )
        started = encounter_when(safe_str(enc_ctx.get("encounter_started_at")) or safe_str(enc.get("created_at")))
        active_class = "enc-link active" if safe_str(enc.get("packet_id")) == selected_packet_id else "enc-link"
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
    pharmacy_html = render_pharmacy(patient_ctx.get("preferred_pharmacy"))

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
          <input type="hidden" id="current_packet_id" value="{html_escape(selected_packet_id)}" />
          <textarea id="note_text_editor" name="note_text">{html_escape(selected_note)}</textarea>
          <p class="btnbar"><button type="submit">Save Note Changes</button></p>
        </form>
        """
        if not selected_meta.get("signed")
        else f"""
        <input type="hidden" id="current_packet_id" value="{html_escape(selected_packet_id)}" />
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

    summary_editor = (
        f"""
        <form method="post" action="/packet/{html_escape(selected_packet_id)}/update-spoken-summary-comments">
          <textarea id="spoken_summary_comments_editor" name="spoken_summary_comments" style="min-height:180px;">{html_escape(selected_meta.get("spoken_summary_comments"))}</textarea>
          <p class="btnbar"><button type="submit">Save Spoken Summary Comments</button></p>
        </form>
        """
        if not selected_meta.get("signed")
        else f"""
        <div class="readonly">{html_escape(selected_meta.get("spoken_summary_comments") or "No physician comments on spoken summary.")}</div>
        <p><em>Signed notes lock spoken-summary comments. Use an addendum for later changes.</em></p>
        """
    )

    physician_actions = f"""
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

    encounter_panel = f"""
      <div class="card">
        <h2 class="section-title">{html_escape(patient_ctx.get('patient_name'))}</h2>

        <div class="meta-grid">
          <div class="metric"><div class="label">Chart #</div><div class="value">{html_escape(patient_ctx.get('chart_number'))}</div></div>
          <div class="metric"><div class="label">Date of Birth</div><div class="value">{html_escape(patient_ctx.get('date_of_birth'))}</div></div>
          <div class="metric"><div class="label">Sex at Birth</div><div class="value">{html_escape(patient_ctx.get('sex_at_birth'))}</div></div>
          <div class="metric"><div class="label">Chief Complaint</div><div class="value">{html_escape((selected_bundle.get('patient_ctx') or {}).get('chief_complaint'))}</div></div>
          <div class="metric"><div class="label">Encounter Started</div><div class="value">{html_escape(format_portal_time((selected_bundle.get('patient_ctx') or {}).get('encounter_started_at') or selected_bundle.get('created_at')))}</div></div>
          <div class="metric"><div class="label">Status</div><div class="value">{html_escape(selected_meta.get('status'))}</div></div>
        </div>

        <p class="pill">Prescription: {html_escape(selected_meta.get('prescription_status'))}</p>
        <p class="pill">Delivery: {html_escape(selected_meta.get('note_sent'))}</p>
      </div>

      <div class="card">
        <h2 class="section-title">Clinical Note</h2>
        {note_editor_html}
      </div>

      <div class="card">
        <h2 class="section-title">Spoken Summary to Patient</h2>
        <div class="readonly">{html_escape(selected_spoken_summary or 'No spoken summary available.')}</div>

        <h3 style="margin-top:18px;">Physician's Comments on Spoken Summary</h3>
        {summary_editor}
      </div>

      {addenda_html}
      {addendum_editor_html}
      {physician_actions}
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

    return shell(
        f"{safe_str(patient_ctx.get('patient_name'))} - CallCare Physician Portal",
        f"""
        <div class="hero">
          <h1>{html_escape(patient_ctx.get('patient_name'))}</h1>
          <p>Chart #{html_escape(patient_ctx.get('chart_number'))} · Physician review workspace</p>
        </div>

        <p><a href="/">← Back to patient list</a> | <a href="/logout">Log out</a></p>

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


@app.post("/packet/{packet_id}/update-note")
async def update_note(packet_id: str, request: Request, note_text: str = Form(...)) -> RedirectResponse:
    require_session(request)

    row = query_one("SELECT chart_number, signed FROM callcare.portal_packets WHERE packet_id = %s LIMIT 1;", (packet_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Packet not found")
    if row.get("signed"):
        raise HTTPException(status_code=400, detail="Signed notes are read-only")

    chart_number = safe_str(row.get("chart_number"))
    execute(
        "UPDATE callcare.portal_packets SET note_text = %s, updated_at = now() WHERE packet_id = %s;",
        (safe_str(note_text), packet_id),
    )
    return RedirectResponse(url=f"/patient/{chart_number}?packet_id={packet_id}&tab=encounters", status_code=303)


@app.post("/packet/{packet_id}/update-spoken-summary-comments")
async def update_summary_comments(packet_id: str, request: Request, spoken_summary_comments: str = Form(...)) -> RedirectResponse:
    require_session(request)

    row = query_one("SELECT chart_number, signed FROM callcare.portal_packets WHERE packet_id = %s LIMIT 1;", (packet_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Packet not found")
    if row.get("signed"):
        raise HTTPException(status_code=400, detail="Signed notes lock spoken-summary comments")

    chart_number = safe_str(row.get("chart_number"))
    execute(
        "UPDATE callcare.portal_packets SET spoken_summary_comments = %s, updated_at = now() WHERE packet_id = %s;",
        (safe_str(spoken_summary_comments), packet_id),
    )
    return RedirectResponse(url=f"/patient/{chart_number}?packet_id={packet_id}&tab=encounters", status_code=303)


@app.post("/packet/{packet_id}/sign")
async def sign_note(packet_id: str, request: Request) -> RedirectResponse:
    require_session(request)

    row = query_one("SELECT chart_number FROM callcare.portal_packets WHERE packet_id = %s LIMIT 1;", (packet_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Packet not found")

    chart_number = safe_str(row.get("chart_number"))
    execute(
        "UPDATE callcare.portal_packets SET signed = true, signed_at = now(), signed_by = 'Kelly Kruk, DO | GA License #: 83704 | NPI: 1285682435', status = 'completed', updated_at = now() WHERE packet_id = %s;",
        (packet_id,),
    )
    patient_ctx = get_patient_context(chart_number) or {}
    queue_or_send_new_note_email(packet_id, patient_ctx, reason="note_ready")
    return RedirectResponse(url=f"/patient/{chart_number}?packet_id={packet_id}&tab=encounters", status_code=303)


@app.post("/packet/{packet_id}/addendum")
async def sign_addendum_route(packet_id: str, request: Request, addendum_text: str = Form(...)) -> RedirectResponse:
    require_session(request)

    row = query_one("SELECT chart_number, signed, COALESCE(addenda, '[]'::jsonb) AS addenda FROM callcare.portal_packets WHERE packet_id = %s LIMIT 1;", (packet_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Packet not found")
    if not row.get("signed"):
        raise HTTPException(status_code=400, detail="Note must be signed before addenda can be added")
    if not safe_str(addendum_text):
        raise HTTPException(status_code=400, detail="Addendum text is required")

    chart_number = safe_str(row.get("chart_number"))
    addenda = row.get("addenda") or []
    addenda.append(
        {
            "text": safe_str(addendum_text),
            "signed_at": now_iso(),
            "signed_by": "Kelly Kruk, DO | GA License #: 83704 | NPI: 1285682435",
        }
    )
    execute(
        "UPDATE callcare.portal_packets SET addenda = %s::jsonb, updated_at = now() WHERE packet_id = %s;",
        (json.dumps(addenda), packet_id),
    )
    patient_ctx = get_patient_context(chart_number) or {}
    queue_or_send_new_note_email(packet_id, patient_ctx, reason="addendum")
    return RedirectResponse(url=f"/patient/{chart_number}?packet_id={packet_id}&tab=encounters", status_code=303)


@app.post("/packet/{packet_id}/prescribe")
async def prescribe(packet_id: str, request: Request) -> RedirectResponse:
    require_session(request)

    row = query_one("SELECT chart_number FROM callcare.portal_packets WHERE packet_id = %s LIMIT 1;", (packet_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Packet not found")

    chart_number = safe_str(row.get("chart_number"))
    execute(
        "UPDATE callcare.portal_packets SET prescription_status = 'sent', updated_at = now() WHERE packet_id = %s;",
        (packet_id,),
    )
    return RedirectResponse(url=f"/patient/{chart_number}?packet_id={packet_id}&tab=encounters", status_code=303)


@app.post("/packet/{packet_id}/note-sent/to-be-mailed")
async def note_to_be_mailed(packet_id: str, request: Request) -> RedirectResponse:
    require_session(request)

    row = query_one("SELECT chart_number FROM callcare.portal_packets WHERE packet_id = %s LIMIT 1;", (packet_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Packet not found")

    chart_number = safe_str(row.get("chart_number"))
    execute(
        "UPDATE callcare.portal_packets SET note_sent = 'to be mailed', updated_at = now() WHERE packet_id = %s;",
        (packet_id,),
    )
    return RedirectResponse(url=f"/patient/{chart_number}?packet_id={packet_id}&tab=encounters", status_code=303)
