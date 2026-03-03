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
# CONFIGURAÇÃO E ESTILO
# =========================
st.set_page_config(page_title="PaperSync 365", page_icon="📄", layout="wide")

st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Outfit:wght@400;600;700&display=swap');
    html, body, [class*="css"] { font-family: 'Outfit', sans-serif; }
    .main { background-color: #f0f2f6; }
    .stApp { background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%); }
    
    .gtd-card {
        background: rgba(255, 255, 255, 0.9);
        backdrop-filter: blur(10px);
        padding: 25px;
        border-radius: 20px;
        box-shadow: 0 10px 30px rgba(0,0,0,0.05);
        margin-bottom: 20px;
        border: 1px solid rgba(255,255,255,0.3);
    }
    .status-badge {
        padding: 4px 12px;
        border-radius: 30px;
        font-size: 11px;
        font-weight: bold;
        text-transform: uppercase;
    }
    .badge-overdue { background: #fee2e2; color: #dc2626; }
    .badge-today { background: #fef3c7; color: #d97706; }
    .badge-ok { background: #dcfce7; color: #16a34a; }
    </style>
""", unsafe_allow_html=True)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
AUTH_BASE = "https://login.microsoftonline.com"
GTD_CONTEXT_LISTS = ["Escritório", "Computador", "Telefone", "Na Rua", "Casa", "Assuntos a Tratar"]
GTD_CONTROL_LISTS = ["Aguardando resposta", "Projetos", "Algum dia/Talvez"]
SCOPES = ["User.Read", "offline_access", "Tasks.ReadWrite", "Calendars.Read", "Mail.Read"]

# =========================
# INTEGRAÇÃO MICROSOFT
# =========================

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
    if time.time() < st.session_state.get("token_expires_at", 0) - 60:
        return token_data.get("access_token")
    
    rt = token_data.get("refresh_token")
    try:
        data = {"client_id": client_id, "grant_type": "refresh_token", "refresh_token": rt, "scope": " ".join(SCOPES), "client_secret": client_secret}
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

def get_todo_lists():
    return graph_request("GET", "/me/todo/lists").get("value", [])

def get_tasks(list_id):
    return graph_request("GET", f"/me/todo/lists/{list_id}/tasks").get("value", [])

def get_flagged_emails():
    params = {"$filter": "flag/flagStatus eq 'flagged'", "$top": "15", "$select": "id,subject,receivedDateTime,from,bodyPreview"}
    return graph_request("GET", "/me/messages", params=params).get("value", [])

def get_planner_plans():
    # Tenta buscar planos do usuário
    return graph_request("GET", "/me/planner/plans").get("value", [])

def get_planner_buckets(plan_id):
    return graph_request("GET", f"/planner/plans/{plan_id}/buckets").get("value", [])

def get_planner_tasks_detailed(plan_id):
    tasks = graph_request("GET", f"/planner/plans/{plan_id}/tasks").get("value", [])
    buckets = get_planner_buckets(plan_id)
    b_map = {b['id']: b['name'] for b in buckets}
    for t in tasks:
        t['bucketName'] = b_map.get(t.get('bucketId'), 'Desconhecido')
    return tasks

def complete_task(list_id, task_id):
    return graph_request("PATCH", f"/me/todo/lists/{list_id}/tasks/{task_id}", payload={"status": "completed"})

# =========================
# LÓGICA DE INTERFACE
# =========================

def main():
    # Auth Check
    client_id, tenant_id, client_secret, redirect_uri = get_azure_config()
    
    if "token" not in st.session_state:
        st.title("PaperSync 365")
        if "oauth_state" not in st.session_state: st.session_state["oauth_state"] = secrets.token_urlsafe(16)
        auth_params = {"client_id": client_id, "response_type": "code", "redirect_uri": redirect_uri, "scope": " ".join(SCOPES), "state": st.session_state["oauth_state"], "response_mode": "query", "prompt": "select_account"}
        auth_url = f"{AUTH_BASE}/{tenant_id}/oauth2/v2.0/authorize?{urlencode(auth_params)}"
        st.link_button("🔌 Conectar com Microsoft 365", auth_url, type="primary")
        st.stop()

    # --- Header e Navegação ---
    st.title("🚀 PaperSync 365")
    tab_dash, tab_clarify, tab_radar, tab_paper = st.tabs(["📊 Dashboard", "🧠 Esclarecer (Inbox)", "📡 Radar Delegação", "🖨️ PaperSync"])

    # Carregar dados comuns
    all_lists = get_todo_lists()
    inbox_list_id = next((l['id'] for l in all_lists if l['displayName'] == "Tasks" or l['wellknownListName'] == "defaultList"), None)
    gtd_map = {l['displayName']: l['id'] for l in all_lists if l['displayName'] in GTD_CONTEXT_LISTS}

    # --- TAB: DASHBOARD ---
    with tab_dash:
        c1, c2 = st.columns([1.5, 1])
        with c1:
            st.subheader("🗓️ Paisagem Rígida")
            # Calendário...
            events = graph_request("GET", "/me/calendarView", params={"startDateTime": datetime.now().isoformat(), "endDateTime": (datetime.now()+timedelta(days=7)).isoformat(), "$top": "5"}).get("value", [])
            for ev in events:
                st.markdown(f"**{ev['start']['dateTime'][11:16]}** - {ev['subject']}")
        with c2:
            st.subheader("⚡ Próximas Ações Prime")
            ctx = st.selectbox("Contexto", GTD_CONTEXT_LISTS)
            if ctx in gtd_map:
                tasks = get_tasks(gtd_map[ctx])
                for t in tasks[:5]:
                    if t['status'] != 'completed':
                        col_t, col_b = st.columns([0.8, 0.2])
                        col_t.write(t['title'])
                        if col_b.button("✅", key=f"d_{t['id']}"):
                            complete_task(gtd_map[ctx], t['id'])
                            st.rerun()

    # --- TAB: ESCLARECER (INBOX) ---
    with tab_clarify:
        st.subheader("📥 Processamento de Entrada")
        st.caption("Decida o que fazer com cada item da sua Inbox.")
        
        # 1. Notas do Papel
        paper_notes = get_unprocessed_inbox_notes()
        # 2. To Do Tasks (Inbox)
        todo_inbox = get_tasks(inbox_list_id) if inbox_list_id else []
        # 3. Flagged Emails
        flagged = get_flagged_emails()

        items_to_process = []
        for n in paper_notes: items_to_process.append({"type": "Papel", "title": n['text'], "id": n['text']})
        for t in todo_inbox: 
            if t['status'] != 'completed': items_to_process.append({"type": "To Do", "title": t['title'], "id": t['id']})
        for f in flagged: items_to_process.append({"type": "E-mail", "title": f['subject'], "id": f['id'], "link": f.get('webLink')})

        if not items_to_process:
            st.success("🎉 Tudo limpo! Inbox vazia.")
        else:
            for item in items_to_process[:5]:
                with st.container():
                    st.markdown(f"#### [{item['type']}] {item['title']}")
                    cl1, cl2, cl3, cl4 = st.columns(4)
                    if cl1.button("✅ Fazer (2 min)", key=f"do_{item['id']}"):
                        st.balloons()
                        # Marcar como pronto...
                    if cl2.button("📅 Agendar", key=f"sch_{item['id']}"):
                        st.info("Abrir diálogo de calendário...")
                    if cl3.button("🤝 Delegar", key=f"del_{item['id']}"):
                        st.info("Mover para Aguardando...")
                    if cl4.button("📁 Organizar", key=f"org_{item['id']}"):
                        st.info("Mover para Contexto...")
                    st.divider()

    # --- TAB: RADAR DE DELEGAÇÃO ---
    with tab_radar:
        st.subheader("📡 Radar de Delegação (Planner)")
        plans = get_planner_plans()
        if not plans:
            st.info("Nenhum plano do Planner encontrado.")
        else:
            plan = st.selectbox("Selecione o Plano da Equipe", [p['title'] for p in plans])
            sel_plan_id = next(p['id'] for p in plans if p['title'] == plan)
            p_tasks = get_planner_tasks_detailed(sel_plan_id)
            
            for pt in p_tasks:
                if pt.get('percentComplete', 0) < 100:
                    badge = '<span class="status-badge badge-ok">No Prazo</span>'
                    due = pt.get('dueDateTime')
                    if due:
                        due_dt = datetime.fromisoformat(due.replace('Z', ''))
                        if due_dt < datetime.now():
                            badge = '<span class="status-badge badge-overdue">ATRASADO</span>'
                    
                    st.markdown(f"""
                        <div class="gtd-card">
                            <div style="display: flex; justify-content: space-between;">
                                <strong>{pt['title']}</strong>
                                {badge}
                            </div>
                            <small>Responsável: {pt.get('assignments', 'Ninguém')}</small>
                        </div>
                    """, unsafe_allow_html=True)

    # --- TAB: PAPERSYNC (WIZARD) ---
    with tab_paper:
        st.subheader("🖨️ Assistente de Impressão GTD")
        
        if "pdf_prep_data" not in st.session_state:
            if st.button("🔍 Iniciar Coleta de Dados para o Papel", type="primary"):
                with st.spinner("Lendo seus sistemas..."):
                    # 1. Calendário
                    events = graph_request("GET", "/me/calendarView", params={
                        "startDateTime": datetime.now().isoformat(),
                        "endDateTime": (datetime.now() + timedelta(days=1)).isoformat(),
                        "$orderby": "start/dateTime"
                    }).get("value", [])
                    
                    # 2. To Do (Priorizado por data ou atraso)
                    context_tasks = {}
                    for ctx_name in GTD_CONTEXT_LISTS:
                        if ctx_name in gtd_map:
                            t_list = get_tasks(gtd_map[ctx_name])
                            prepared = []
                            for t in t_list:
                                if t['status'] != 'completed':
                                    due = t.get('dueDateTime', {}).get('dateTime')
                                    is_overdue = False
                                    if due:
                                        due_dt = datetime.fromisoformat(due[:19])
                                        if due_dt < datetime.now(): is_overdue = True
                                    prepared.append({"title": t['title'], "overdue": is_overdue})
                            # Ordena: Atrasados primeiro
                            prepared.sort(key=lambda x: x['overdue'], reverse=True)
                            if prepared: context_tasks[ctx_name] = prepared[:6]

                    # 3. Planner
                    waiting = []
                    plans = get_planner_plans()
                    if plans:
                        # Pega tarefas do primeiro plano ou um plano 'PaperSync' se existir
                        target_plan = plans[0]
                        p_tasks = get_planner_tasks_detailed(target_plan['id'])
                        for pt in p_tasks:
                            if pt.get('percentComplete', 0) < 100:
                                waiting.append({
                                    "task": pt['title'],
                                    "plan": target_plan['title'],
                                    "bucket": pt.get('bucketName', '')
                                })
                    
                    st.session_state["pdf_prep_data"] = {
                        "date": date.today().strftime("%d/%m/%Y"),
                        "calendar": [{"time": e['start']['dateTime'][11:16], "subject": e['subject']} for e in events],
                        "tasks": context_tasks,
                        "waiting": waiting[:5]
                    }
                    st.rerun()
        else:
            # TELA DE REVISÃO
            data = st.session_state["pdf_prep_data"]
            st.info("💡 Revise os itens abaixo antes de gerar o PDF. Você pode remover linhas que não quer no papel.")
            
            col_rev1, col_rev2 = st.columns(2)
            with col_rev1:
                st.write("**📅 Calendário**")
                for i, ev in enumerate(data['calendar']):
                    if st.checkbox(f"{ev['time']} - {ev['subject']}", value=True, key=f"rev_ev_{i}"):
                        pass
                    else: ev['remove'] = True
            
            with col_rev2:
                st.write("**📡 Delegação**")
                for i, wt in enumerate(data['waiting']):
                    if st.checkbox(f"{wt['task']} ({wt['bucket']})", value=True, key=f"rev_wt_{i}"):
                        pass
                    else: wt['remove'] = True
            
            st.write("**⚡ Próximas Ações**")
            for ctx, tasks in data['tasks'].items():
                st.markdown(f"_{ctx}_")
                for i, t in enumerate(tasks):
                    label = f"{'🚩 ' if t['overdue'] else ''}{t['title']}"
                    if st.checkbox(label, value=True, key=f"rev_tk_{ctx}_{i}"):
                        pass
                    else: t['remove'] = True

            if st.button("🚀 Gerar PDF com os itens selecionados"):
                # Filtra removidos
                final_cal = [e for e in data['calendar'] if not e.get('remove')]
                final_waiting = [w for w in data['waiting'] if not w.get('remove')]
                final_tasks = {}
                for ctx, tks in data['tasks'].items():
                    final_tasks[ctx] = [t for t in tks if not t.get('remove')]
                
                final_data = {
                    "date": data['date'],
                    "page_id": f"PS365-{int(time.time())}",
                    "calendar": final_cal,
                    "tasks": final_tasks,
                    "waiting": final_waiting
                }
                
                save_page_snapshot(final_data["page_id"], final_data)
                pdf_bytes = generate_gtd_page(final_data)
                st.download_button("⬇️ BAIXAR PDF FINAL", pdf_bytes, file_name=f"PaperSync_{final_data['page_id']}.pdf")
                
            if st.button("🗑️ Resetar Wizard"):
                del st.session_state["pdf_prep_data"]
                st.rerun()

        st.divider()
        st.subheader("📥 Processar Scan")
        up = st.file_uploader("Upload do Scan (Foto do Papel)", type=['png', 'jpg', 'jpeg'])
        if up:
            with st.spinner("Analisando marcas..."):
                res = process_scan(up.read())
                st.success(f"Página identificada: {res['page_id']}")
                for t in res['concluded_tasks']: st.write(f"✅ Concluindo: {t}")

# Auth Callback
if __name__ == "__main__":
    q = st.query_params
    if "code" in q and "token" not in st.session_state:
        c_id, t_id, c_sec, r_uri = get_azure_config()
        r = requests.post(f"{AUTH_BASE}/{t_id}/oauth2/v2.0/token", data={"client_id": c_id, "grant_type": "authorization_code", "code": q["code"], "redirect_uri": r_uri, "scope": " ".join(SCOPES), "client_secret": c_sec})
        st.session_state["token"] = r.json()
        st.session_state["token_expires_at"] = time.time() + int(r.json().get("expires_in", 3600))
        st.query_params.clear()
        st.rerun()
    main()
