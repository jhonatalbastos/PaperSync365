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

# CSS para restaurar a funcionalidade total com estética Premium
st.markdown(f"""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Public+Sans:wght@300;400;500;600;700&display=swap');
    
    :root {{
        --brand-blue: #2563eb;
        --brand-slate: #1e293b;
    }}

    html, body, [class*="css"] {{ font-family: 'Public Sans', sans-serif; background-color: #f1f5f9; }}
    
    [data-testid="stSidebar"] {{ background-color: #ffffff; border-right: 1px solid #e2e8f0; }}
    
    .fecd-card {{
        background: white;
        padding: 24px;
        border-radius: 12px;
        box-shadow: 0 1px 3px 0 rgb(0 0 0 / 0.1), 0 1px 2px -1px rgb(0 0 0 / 0.1);
        border: 1px solid #e2e8f0;
        margin-bottom: 20px;
    }}
    
    h1, h2, h3 {{ color: var(--brand-slate); font-weight: 700 !important; }}
    
    .status-pill {{
        padding: 2px 10px;
        border-radius: 4px;
        font-size: 10px;
        font-weight: 700;
        text-transform: uppercase;
    }}
    .pill-urgent {{ background: #fee2e2; color: #b91c1c; }}
    .pill-normal {{ background: #e0f2fe; color: #0369a1; }}

    /* Botão de Sincronização e Ação Principal */
    .stButton>button {{
        border-radius: 8px;
        padding: 0.5rem 1rem;
        transition: all 0.2s;
    }}
    
    .app-watermark {{
        position: fixed;
        bottom: 20px;
        right: 20px;
        width: 200px;
        opacity: 0.05;
        z-index: -1;
        pointer-events: none;
    }}
    </style>
    <img src="data:image/png;base64,{base64.b64encode(open(logo_path, "rb").read()).decode() if os.path.exists(logo_path) else ''}" class="app-watermark">
""", unsafe_allow_html=True)

# --- MICROSOFT API CORE (MANTIDO 100%) ---
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
def get_flagged_emails(): return graph_request("GET", "/me/messages", params={"$filter": "flag/flagStatus eq 'flagged'", "$top": "30"}).get("value", [])
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
            st.write("Portal de Gestão Microsoft 365")
            if "oauth_state" not in st.session_state: st.session_state["oauth_state"] = secrets.token_urlsafe(16)
            auth_params = {"client_id": client_id, "response_type": "code", "redirect_uri": redirect_uri, "scope": " ".join(SCOPES), "state": st.session_state["oauth_state"], "response_mode": "query", "prompt": "select_account"}
            auth_url = f"{AUTH_BASE}/{tenant_id}/oauth2/v2.0/authorize?{urlencode(auth_params)}"
            st.link_button("🔌 Entrar com Conta Microsoft", auth_url, type="primary", use_container_width=True)
        st.stop()

    # Sidebar com Funcionalidades Integradas
    with st.sidebar:
        if os.path.exists(logo_path): st.image(logo_path, use_container_width=True)
        st.markdown("<br>", unsafe_allow_html=True)
        selection = st.radio("Menu de Navegação", ["📊 Dashboard Completo", "🧠 Central de Esclarecer", "🤝 Radar de Delegação", "🖨️ Assistente de Impressão", "📤 Upload de Scan"], label_visibility="collapsed")
        st.divider()
        if st.button("🚪 Sair", use_container_width=True):
            del st.session_state["token"]; st.rerun()

    all_lists = get_todo_lists()
    inbox_list_id = next((l['id'] for l in all_lists if l['displayName'] == "Tasks" or l['wellknownListName'] == "defaultList"), None)
    gtd_map = {l['displayName']: l['id'] for l in all_lists if l['displayName'] in GTD_CONTEXT_LISTS}

    if selection == "📊 Dashboard Completo":
        st.title("📊 Painel Executivo")
        c1, c2 = st.columns([1.5, 1])
        with c1:
            st.markdown('<div class="fecd-card">', unsafe_allow_html=True)
            st.subheader("🗓️ Calendário de Hoje")
            events = graph_request("GET", "/me/calendarView", params={
                "startDateTime": datetime.now().replace(hour=0, minute=0).isoformat(),
                "endDateTime": datetime.now().replace(hour=23, minute=59).isoformat()
            }).get("value", [])
            if not events: st.info("Sem compromissos agendados.")
            for ev in events: st.markdown(f"**{ev['start']['dateTime'][11:16]}** — {ev['subject']}")
            st.markdown('</div>', unsafe_allow_html=True)
        with c2:
            st.markdown('<div class="fecd-card">', unsafe_allow_html=True)
            st.subheader("⚡ Ações por Contexto")
            ctx = st.selectbox("Selecione a Lista de Contexto", GTD_CONTEXT_LISTS)
            if ctx in gtd_map:
                tasks = get_tasks(gtd_map[ctx])
                active = [t for t in tasks if t['status'] != 'completed']
                if not active: st.success("🎉 Tudo limpo por aqui!")
                for t in active:
                    t_col, b_col = st.columns([0.85, 0.15])
                    t_col.write(t['title'])
                    if b_col.button("✓", key=f"dash_comp_{t['id']}"): complete_task(gtd_map[ctx], t['id']); st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)

    elif selection == "🧠 Central de Esclarecer":
        st.title("🧠 Esclarecer (Capturas)")
        st.write("Processe itens da Inbox, E-mails sinalizados e Notas de Papel.")
        
        t_inbox, t_paper, t_email = st.tabs(["📥 Inbox To Do", "📝 Notas de Papel", "📧 E-mails com Flag"])
        
        with t_inbox:
            if inbox_list_id:
                inbox_tasks = get_tasks(inbox_list_id)
                for it in inbox_tasks:
                    if it['status'] != 'completed':
                        with st.container(border=True):
                            st.write(it['title'])
                            b1, b2, b3 = st.columns(3)
                            if b1.button("✓ Feito", key=f"inb_{it['id']}"): complete_task(inbox_list_id, it['id']); st.rerun()
                            b2.button("📅 Agendar", key=f"inba_{it['id']}")
                            b3.button("🤝 Delegar", key=f"inbd_{it['id']}")
        
        with t_paper:
            paper_notes = get_unprocessed_inbox_notes()
            if not paper_notes: st.info("Nenhuma nota manuscrita processada recentemente.")
            for pn in paper_notes:
                with st.container(border=True):
                    st.write(pn['text'])
                    if st.button("✓ Processado", key=f"pnb_{pn['text']}"): mark_note_as_processed(pn['text']); st.rerun()
        
        with t_email:
            emails = get_flagged_emails()
            for eml in emails:
                with st.container(border=True):
                    st.markdown(f"**{eml['subject']}**")
                    st.caption(f"De: {eml['from']['emailAddress']['name']}")
                    st.button("✓ Resolver E-mail", key=f"emlv_{eml['id']}")

    elif selection == "🤝 Radar de Delegação":
        st.title("🤝 Radar de Delegação (Planner)")
        plans = get_planner_plans()
        if not plans: st.warning("Nenhum plano encontrado no Planner.")
        else:
            p_name = st.selectbox("Escolha o Plano do Projeto", [p['title'] for p in plans])
            p_id = next(p['id'] for p in plans if p['title'] == p_name)
            p_tasks = get_planner_tasks_detailed(p_id)
            for pt in p_tasks:
                if pt.get('percentComplete', 0) < 100:
                    badge = "pill-urgent" if pt.get('dueDateTime') and datetime.fromisoformat(pt['dueDateTime'][:19]) < datetime.now() else "pill-normal"
                    st.markdown(f'<div class="fecd-card"><span class="status-pill {badge}">{pt["bucketName"]}</span><h4 style="margin-top:10px;">{pt["title"]}</h4></div>', unsafe_allow_html=True)

    elif selection == "🖨️ Assistente de Impressão":
        st.title("🖨️ Gerador de Folha GTD")
        
        if "wizard_step" not in st.session_state: st.session_state.wizard_step = 1
        
        if st.session_state.wizard_step == 1:
            st.info("Passo 1: Sincronizando dados das suas listas Microsoft 365...")
            if st.button("🔍 Sincronizar Agora", type="primary"):
                with st.spinner("Buscando tarefas e calendários..."):
                    evs = graph_request("GET", "/me/calendarView", params={"startDateTime": datetime.now().isoformat(), "endDateTime": (datetime.now() + timedelta(days=1)).isoformat()}).get("value", [])
                    tasks_raw = {}
                    for ctx_n, ctx_id in gtd_map.items():
                        ts = get_tasks(ctx_id)
                        tasks_raw[ctx_n] = [{"title": t['title'], "selected": True} for t in ts if t['status'] != 'completed']
                    st.session_state.sync_data = {"calendar": [{"subject": e['subject'], "time": e['start']['dateTime'][11:16], "selected": True} for e in evs], "tasks": tasks_raw}
                    st.session_state.wizard_step = 2; st.rerun()

        elif st.session_state.wizard_step == 2:
            st.subheader("📝 Pre-visualização e Seleção")
            st.write("Selecione o que entrará no papel de hoje.")
            sd = st.session_state.sync_data
            
            with st.form("editor_pdf"):
                st.markdown("#### 🗓️ Calendário")
                for i, ev_item in enumerate(sd['calendar']):
                    ev_item['selected'] = st.checkbox(f"**{ev_item['time']}** - {ev_item['subject']}", value=ev_item['selected'], key=f"f_ev_{i}")
                
                st.markdown("#### ✅ Tarefas por Contexto")
                for ctx_name, tlist in sd['tasks'].items():
                    if tlist:
                        st.markdown(f"**{ctx_name}**")
                        for j, tk_item in enumerate(tlist):
                            tk_item['selected'] = st.checkbox(tk_item['title'], value=tk_item['selected'], key=f"f_tk_{ctx_name}_{j}")
                
                if st.form_submit_button("🚀 Confirmar e Gerar PDF"):
                    final_cal = [e for e in sd['calendar'] if e['selected']]
                    final_tasks = {c: [t for t in tl if t['selected']] for c, tl in sd['tasks'].items()}
                    st.session_state.final_gtd_data = {"date": date.today().strftime("%d/%m/%Y"), "page_id": f"FECD-{int(time.time())}", "calendar": final_cal, "tasks": final_tasks, "waiting": []}
                    st.session_state.wizard_step = 3; st.rerun()
            if st.button("⬅️ Cancelar"): st.session_state.wizard_step = 1; st.rerun()

        elif st.session_state.wizard_step == 3:
            st.success("Tudo pronto! Sua folha foi preparada.")
            fdata = st.session_state.final_gtd_data
            save_page_snapshot(fdata["page_id"], fdata)
            pdf_buf = generate_gtd_page(fdata)
            pdf_val = pdf_buf.getvalue()
            b64_pdf = base64.b64encode(pdf_val).decode('utf-8')
            
            st.markdown(f'<a href="data:application/pdf;base64,{b64_pdf}" target="_blank" style="text-decoration: none;"><div style="background-color: #2563eb; color: white; padding: 18px; border-radius: 12px; text-align: center; font-weight: 800; cursor: pointer; margin-bottom: 12px;">📄 ABRIR PDF FECD PARA IMPRIMIR</div></a>', unsafe_allow_html=True)
            st.download_button("⬇️ Baixar Cópia Adicional", pdf_val, file_name=f"Tarefas_FECD_{fdata['page_id']}.pdf")
            if st.button("♻️ Iniciar Novo Ciclo"): st.session_state.wizard_step = 1; st.rerun()

    elif selection == "📤 Upload de Scan":
        st.title("📤 Upload e Capture")
        st.write("Suba o scan da sua folha impressa para processar o GTD.")
        up = st.file_uploader("Upload do Scan (PNG/JPG)", type=["png", "jpg", "jpeg"])
        if up:
            if st.button("🔍 Processar Marcas de Caneta", type="primary"):
                with st.spinner("Processando..."):
                    res = process_scan(up)
                    st.success("Processamento Simulado com Sucesso!")
                    st.write(f"ID da Folha: {res['page_id']}")
                    for n in res['notes']: st.write(f"- {n}")
                    st.balloons()

if __name__ == "__main__":
    q = st.query_params
    if "code" in q and "token" not in st.session_state:
        cid, tid, csec, ruri = get_azure_config()
        r = requests.post(f"{AUTH_BASE}/{tid}/oauth2/v2.0/token", data={"client_id": cid, "grant_type": "authorization_code", "code": q["code"], "redirect_uri": ruri, "scope": " ".join(SCOPES), "client_secret": csec})
        st.session_state["token"] = r.json()
        st.session_state["token_expires_at"] = time.time() + int(r.json().get("expires_in", 3600))
        st.query_params.clear(); st.rerun()
    main()
