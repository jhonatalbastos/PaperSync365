import os
import time
import json
import base64
import hashlib
import secrets
from datetime import datetime, timedelta, date
from urllib.parse import urlencode

import requests
import streamlit as st
import pandas as pd

from pdf_utils import generate_gtd_page
from vision_utils import process_scan, get_unprocessed_inbox_notes, mark_note_as_processed, save_page_snapshot

# =========================
# CONFIGURAÇÃO E ESTILO PREMIUM (FECD BRANDING)
# =========================
st.set_page_config(page_title="Tarefas do Dia | FECD", page_icon="📈", layout="wide")

logo_path = os.path.join(os.path.dirname(__file__), "assets", "logo_fecd.png")

st.markdown(f"""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&display=swap');
    
    :root {{
        --brand-blue: #1d4ed8;
        --brand-slate: #0f172a;
    }}

    html, body, [class*="css"] {{
        font-family: 'Outfit', sans-serif;
        background-color: #f8fafc;
    }}
    
    [data-testid="stSidebar"] {{
        background-color: #ffffff;
        border-right: 1px solid #e2e8f0;
    }}
    
    .fecd-card {{
        background: white;
        padding: 24px;
        border-radius: 16px;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05);
        border: 1px solid #f1f5f9;
        margin-bottom: 20px;
    }}
    
    h1, h2, h3 {{ color: var(--brand-slate); font-weight: 700 !important; }}
    
    .status-pill {{
        padding: 4px 12px;
        border-radius: 9999px;
        font-size: 11px;
        font-weight: 600;
        text-transform: uppercase;
    }}
    .pill-normal {{ background: #f0f9ff; color: #0369a1; }}
    .pill-urgent {{ background: #fee2e2; color: #dc2626; }}

    .app-watermark {{
        position: fixed;
        bottom: 30px;
        right: 30px;
        width: 250px;
        opacity: 0.05;
        z-index: -1;
        pointer-events: none;
    }}
    </style>
    <img src="data:image/png;base64,{base64.b64encode(open(logo_path, "rb").read()).decode() if os.path.exists(logo_path) else ''}" class="app-watermark">
""", unsafe_allow_html=True)

# --- MICROSOFT API CORE ---
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
AUTH_BASE = "https://login.microsoftonline.com"
GTD_CONTEXT_LISTS = ["Escritório", "Computador", "Telefone", "Na Rua", "Casa", "Assuntos a Tratar"]
SCOPES = ["User.Read", "offline_access", "Tasks.ReadWrite", "Calendars.Read", "Mail.Read"]

def get_azure_config():
    azure = st.secrets.get("azure", {})
    r_uri = azure.get("REDIRECT_URI", "").strip()
    if "/callback" in r_uri: r_uri = r_uri.split("/callback")[0]
    r_uri = r_uri.rstrip("/") + "/"
    return azure.get("CLIENT_ID", "").strip(), azure.get("TENANT_ID", "common").strip(), azure.get("CLIENT_SECRET", "").strip(), r_uri

def get_access_token():
    azure = st.secrets.get("azure", {})
    client_id = azure.get("CLIENT_ID")
    client_secret = azure.get("CLIENT_SECRET")
    tenant_id = azure.get("TENANT_ID", "common")
    token_data = st.session_state.get("token")
    if not token_data: return None
    if time.time() < st.session_state.get("token_expires_at", 0) - 60: return token_data.get("access_token")
    try:
        data = {"client_id": client_id, "grant_type": "refresh_token", "refresh_token": token_data.get("refresh_token"), "scope": " ".join(SCOPES), "client_secret": client_secret}
        r = requests.post(f"{AUTH_BASE}/{tenant_id}/oauth2/v2.0/token", data=data, timeout=20)
        new_token = r.json()
        st.session_state["token"] = new_token
        st.session_state["token_expires_at"] = time.time() + int(new_token.get("expires_in", 3600))
        return new_token.get("access_token")
    except: return None

def graph_request(method, path, params=None, payload=None):
    token = get_access_token()
    if not token: return {"error": "Sem token"}
    url = f"{GRAPH_BASE}{path}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.request(method, url, headers=headers, params=params, data=json.dumps(payload) if payload else None, timeout=30)
    return r.json() if r.text else {}

def get_todo_lists(): return graph_request("GET", "/me/todo/lists").get("value", [])
def get_tasks(list_id): return graph_request("GET", f"/me/todo/lists/{list_id}/tasks").get("value", [])
def get_flagged_emails(): return graph_request("GET", "/me/messages", params={"$filter": "flag/flagStatus eq 'flagged'", "$top": "10"}).get("value", [])
def get_planner_plans(): return graph_request("GET", "/me/planner/plans").get("value", [])
def get_planner_buckets(plan_id): return graph_request("GET", f"/planner/plans/{plan_id}/buckets").get("value", [])
def get_planner_tasks_detailed(plan_id):
    tasks = graph_request("GET", f"/planner/plans/{plan_id}/tasks").get("value", [])
    buckets = get_planner_buckets(plan_id)
    b_map = {b['id']: b['name'] for b in buckets}
    for t in tasks: t['bucketName'] = b_map.get(t.get('bucketId'), 'Desconhecido')
    return tasks
def complete_task(list_id, task_id): return graph_request("PATCH", f"/me/todo/lists/{list_id}/tasks/{task_id}", payload={"status": "completed"})

# --- VIEW MAIN ---
def main():
    client_id, tenant_id, client_secret, redirect_uri = get_azure_config()
    
    if "token" not in st.session_state:
        st.markdown("<br><br>", unsafe_allow_html=True)
        col1, col2, col3 = st.columns([1, 1.5, 1])
        with col2:
            if os.path.exists(logo_path): st.image(logo_path, width=350)
            st.title("Acesso FECD")
            st.write("Portal de Gestão de Tarefas e Compromissos.")
            if "oauth_state" not in st.session_state: st.session_state["oauth_state"] = secrets.token_urlsafe(16)
            auth_params = {"client_id": client_id, "response_type": "code", "redirect_uri": redirect_uri, "scope": " ".join(SCOPES), "state": st.session_state["oauth_state"], "response_mode": "query", "prompt": "select_account"}
            auth_url = f"{AUTH_BASE}/{tenant_id}/oauth2/v2.0/authorize?{urlencode(auth_params)}"
            st.link_button("🔌 Entrar com Microsoft 365", auth_url, type="primary", use_container_width=True)
        st.stop()

    # Sidebar
    with st.sidebar:
        if os.path.exists(logo_path): st.image(logo_path, use_container_width=True)
        st.markdown("<br>", unsafe_allow_html=True)
        selection = st.radio("Menu", ["📊 Dashboard", "🧠 Esclarecer", "🤝 Delegação", "🖨️ Tarefas do Dia (PDF)"], label_visibility="collapsed")
        st.divider()
        if st.button("🚪 Sair", use_container_width=True):
            del st.session_state["token"]
            st.rerun()

    all_lists = get_todo_lists()
    inbox_list_id = next((l['id'] for l in all_lists if l['displayName'] == "Tasks" or l['wellknownListName'] == "defaultList"), None)
    gtd_map = {l['displayName']: l['id'] for l in all_lists if l['displayName'] in GTD_CONTEXT_LISTS}

    if selection == "📊 Dashboard":
        st.title("📊 Resumo do Dia")
        c1, c2 = st.columns([1.5, 1])
        with c1:
            st.markdown('<div class="fecd-card">', unsafe_allow_html=True)
            st.subheader("🗓️ Compromissos de Hoje")
            events = graph_request("GET", "/me/calendarView", params={
                "startDateTime": datetime.now().replace(hour=0, minute=0).isoformat(),
                "endDateTime": datetime.now().replace(hour=23, minute=59).isoformat()
            }).get("value", [])
            if not events: st.write("Agenda livre por enquanto.")
            for ev in events: st.markdown(f"**{ev['start']['dateTime'][11:16]}** — {ev['subject']}")
            st.markdown('</div>', unsafe_allow_html=True)
        with c2:
            st.markdown('<div class="fecd-card">', unsafe_allow_html=True)
            st.subheader("⚡ Ações Contextuais")
            ctx = st.selectbox("Escolha um Contexto", GTD_CONTEXT_LISTS)
            if ctx in gtd_map:
                tasks = get_tasks(gtd_map[ctx])
                for t in tasks[:5]:
                    if t['status'] != 'completed':
                        tc1, tc2 = st.columns([0.85, 0.15])
                        tc1.write(t['title'])
                        if tc2.button("✓", key=f"dash_tk_{t['id']}"):
                            complete_task(gtd_map[ctx], t['id'])
                            st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)

    elif selection == "🧠 Esclarecer":
        st.title("🧠 Esclarecer Inbox")
        notes = get_unprocessed_inbox_notes()
        emails = get_flagged_emails()
        st.write("Processe itens capturados do papel ou sinalizados no e-mail.")
        for n in notes:
            with st.container(border=True):
                st.markdown(f"**📝 Nota Capturada:** {n['text']}")
                b1, b2, b3 = st.columns(3)
                if b1.button("✓ Feito", key=f"note_{n['text']}"): mark_note_as_processed(n['text']); st.rerun()
                b2.button("📅 Agendar", key=f"nas_{n['text']}")
                b3.button("🤝 Delegar", key=f"nds_{n['text']}")

    elif selection == "🤝 Delegação":
        st.title("🤝 Radar de Delegação")
        plans = get_planner_plans()
        if plans:
            p_name = st.selectbox("Plano FECD", [p['title'] for p in plans])
            p_id = next(p['id'] for p in plans if p['title'] == p_name)
            p_tasks = get_planner_tasks_detailed(p_id)
            for pt in p_tasks:
                if pt.get('percentComplete', 0) < 100:
                    st.markdown(f'<div class="fecd-card"><span class="status-pill pill-normal">{pt["bucketName"]}</span><h4 style="margin-top:10px;">{pt["title"]}</h4></div>', unsafe_allow_html=True)

    elif selection == "🖨️ Tarefas do Dia (PDF)":
        st.title("🖨️ Gerador de Folha Analógica")
        if "pdf_data" not in st.session_state:
            if st.button("🔍 Sincronizar Tudo", type="primary", use_container_width=True):
                events = graph_request("GET", "/me/calendarView", params={"startDateTime": datetime.now().isoformat(), "endDateTime": (datetime.now() + timedelta(days=1)).isoformat()}).get("value", [])
                ctx_tasks = {}
                for cn in GTD_CONTEXT_LISTS:
                    if cn in gtd_map:
                        ts = get_tasks(gtd_map[cn])
                        active = [{"title": t['title'], "overdue": False} for t in ts if t['status'] != 'completed'][:5]
                        if active: ctx_tasks[cn] = active
                st.session_state["pdf_data"] = {"date": date.today().strftime("%d/%m/%Y"), "calendar": [{"time": e['start']['dateTime'][11:16], "subject": e['subject']} for e in events], "tasks": ctx_tasks, "waiting": []}
                st.rerun()
        else:
            d = st.session_state["pdf_data"]
            st.markdown('<div class="fecd-card">', unsafe_allow_html=True)
            st.subheader("Prévia da Folha")
            st.write(f"Data: {d['date']}")
            st.write(f"Compromissos: {len(d['calendar'])}")
            if st.button("🚀 Gerar e Abrir PDF Oficial FECD", type="primary", use_container_width=True):
                d["page_id"] = f"FECD-{int(time.time())}"
                save_page_snapshot(d["page_id"], d)
                pdf_buffer = generate_gtd_page(d)
                pdf_bytes = pdf_buffer.getvalue()
                
                # Convert PDF to Base64 for opening in new tab
                b64_pdf = base64.b64encode(pdf_bytes).decode('utf-8')
                pdf_display = f'<a href="data:application/pdf;base64,{b64_pdf}" target="_blank" style="text-decoration: none;"><div style="background-color: #1d4ed8; color: white; padding: 12px; border-radius: 12px; text-align: center; font-weight: 600; cursor: pointer; margin-bottom: 15px;">📄 CLIQUE AQUI PARA ABRIR E IMPRIMIR</div></a>'
                
                st.markdown(pdf_display, unsafe_allow_html=True)
                st.download_button("⬇️ Salvar uma cópia (Download)", pdf_bytes, file_name=f"Tarefas_FECD_{d['date'].replace('/','-')}.pdf", use_container_width=True)
            if st.button("♻️ Refazer Sincronização"): del st.session_state["pdf_data"]; st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)

if __name__ == "__main__":
    q = st.query_params
    if "code" in q and "token" not in st.session_state:
        cid, tid, csec, ruri = get_azure_config()
        r = requests.post(f"{AUTH_BASE}/{tid}/oauth2/v2.0/token", data={"client_id": cid, "grant_type": "authorization_code", "code": q["code"], "redirect_uri": ruri, "scope": " ".join(SCOPES), "client_secret": csec})
        st.session_state["token"] = r.json()
        st.session_state["token_expires_at"] = time.time() + int(r.json().get("expires_in", 3600))
        st.query_params.clear()
        st.rerun()
    main()
