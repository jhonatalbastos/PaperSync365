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
# CONFIGURAÇÃO E ESTILO PREMIUM
# =========================
st.set_page_config(page_title="Tarefas do Dia | FECD", page_icon="📈", layout="wide")

# Carregar logo para o app
logo_path = os.path.join(os.path.dirname(__file__), "assets", "logo_fecd.png")

st.markdown(f"""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
    
    :root {{
        --fecd-blue: #2563eb;
        --fecd-slate: #1e293b;
        --bg-gradient: linear-gradient(135deg, #f8fafc 0%, #e2e8f0 100%);
    }}

    html, body, [class*="css"] {{ font-family: 'Inter', sans-serif; }}
    
    .stApp {{
        background: var(--bg-gradient);
    }}

    /* Sidebar Custom */
    [data-testid="stSidebar"] {{
        background-color: #ffffff;
        border-right: 1px solid #e2e8f0;
    }}

    /* Glass Cards */
    .module-card {{
        background: rgba(255, 255, 255, 0.7);
        backdrop-filter: blur(12px);
        -webkit-backdrop-filter: blur(12px);
        border: 1px solid rgba(255, 255, 255, 0.3);
        border-radius: 16px;
        padding: 24px;
        box-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.1), 0 2px 4px -2px rgb(0 0 0 / 0.1);
        margin-bottom: 20px;
        transition: transform 0.2s ease;
    }}
    .module-card:hover {{
        transform: translateY(-2px);
    }}

    /* Typography */
    h1 {{ color: var(--fecd-slate); font-weight: 700 !important; letter-spacing: -0.025em; }}
    h2 {{ color: var(--fecd-slate); font-weight: 600 !important; }}
    h3 {{ color: var(--fecd-slate); font-weight: 600 !important; }}
    
    .status-badge {{
        padding: 4px 10px;
        border-radius: 6px;
        font-size: 11px;
        font-weight: 600;
        text-transform: uppercase;
        display: inline-block;
    }}
    .badge-error {{ background: #fee2e2; color: #dc2626; }}
    .badge-success {{ background: #dcfce7; color: #16a34a; }}
    .badge-warning {{ background: #fef3c7; color: #d97706; }}
    .badge-info {{ background: #e0e7ff; color: #4338ca; }}

    /* Custom Buttons */
    .stButton>button {{
        border-radius: 10px;
        font-weight: 500;
        transition: all 0.2s;
    }}
    
    /* Watermark inside app */
    .watermark {{
        position: fixed;
        bottom: 20px;
        right: 20px;
        opacity: 0.08;
        z-index: -1;
        width: 300px;
    }}
    </style>
    <img src="data:image/png;base64,{base64.b64encode(open(logo_path, "rb").read()).decode() if os.path.exists(logo_path) else ''}" class="watermark">
""", unsafe_allow_html=True)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
AUTH_BASE = "https://login.microsoftonline.com"
GTD_CONTEXT_LISTS = ["Escritório", "Computador", "Telefone", "Na Rua", "Casa", "Assuntos a Tratar"]
GTD_CONTROL_LISTS = ["Aguardando resposta", "Projetos", "Algum dia/Talvez"]
SCOPES = ["User.Read", "offline_access", "Tasks.ReadWrite", "Calendars.Read", "Mail.Read"]

# ... [Mantenha aqui as funções Graph API e Azure Config inalteradas] ...

# =========================
# LÓGICA DE INTERFACE (MODERNA)
# =========================

def main():
    # Auth Check
    client_id, tenant_id, client_secret, redirect_uri = get_azure_config()
    
    if "token" not in st.session_state:
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            st.markdown("<br><br>", unsafe_allow_html=True)
            if os.path.exists(logo_path):
                st.image(logo_path, width=400)
            st.title("Acesso ao Sistema")
            st.write("Conecte sua conta Microsoft para começar o dia.")
            if "oauth_state" not in st.session_state: st.session_state["oauth_state"] = secrets.token_urlsafe(16)
            auth_params = {"client_id": client_id, "response_type": "code", "redirect_uri": redirect_uri, "scope": " ".join(SCOPES), "state": st.session_state["oauth_state"], "response_mode": "query", "prompt": "select_account"}
            auth_url = f"{AUTH_BASE}/{tenant_id}/oauth2/v2.0/authorize?{urlencode(auth_params)}"
            st.link_button("🔌 Conector Microsoft 365", auth_url, type="primary", use_container_width=True)
        st.stop()

    # --- Navegação Lateral ---
    with st.sidebar:
        if os.path.exists(logo_path):
            st.image(logo_path, use_container_width=True)
        st.markdown("<hr>", unsafe_allow_html=True)
        
        selection = st.radio(
            "Módulos FECD",
            ["📊 Dashboard Executivo", "🧠 Esclarecer (Inbox)", "📡 Delegação & Equipe", "🖨️ Impressão de Papeis"],
            label_visibility="collapsed"
        )
        
        st.markdown("<br><br><br>", unsafe_allow_html=True)
        if st.button("🚪 Sair", use_container_width=True):
            del st.session_state["token"]
            st.rerun()

    # Carregar dados comuns
    all_lists = get_todo_lists()
    inbox_list_id = next((l['id'] for l in all_lists if l['displayName'] == "Tasks" or l['wellknownListName'] == "defaultList"), None)
    gtd_map = {l['displayName']: l['id'] for l in all_lists if l['displayName'] in GTD_CONTEXT_LISTS}

    # --- RENDERIZAÇÃO POR MÓDULO ---
    
    if selection == "📊 Dashboard Executivo":
        st.title("📊 Visão Geral")
        c1, c2 = st.columns([1.6, 1])
        
        with c1:
            st.markdown('<div class="module-card">', unsafe_allow_html=True)
            st.subheader("🗓️ Eventos do Dia")
            events = graph_request("GET", "/me/calendarView", params={
                "startDateTime": datetime.now().replace(hour=0, minute=0).isoformat(),
                "endDateTime": datetime.now().replace(hour=23, minute=59).isoformat()
            }).get("value", [])
            
            if not events:
                st.info("Nenhum compromisso marcado para hoje.")
            else:
                for ev in events:
                    time_str = ev['start']['dateTime'][11:16]
                    st.markdown(f"**{time_str}** - {ev['subject']}")
            st.markdown('</div>', unsafe_allow_html=True)

        with c2:
            st.markdown('<div class="module-card">', unsafe_allow_html=True)
            st.subheader("⚡ Ações por Contexto")
            ctx = st.selectbox("Selecione o Local/Meio", GTD_CONTEXT_LISTS)
            if ctx in gtd_map:
                tasks = get_tasks(gtd_map[ctx])
                for t in tasks[:5]:
                    if t['status'] != 'completed':
                        col_t, col_b = st.columns([0.85, 0.15])
                        col_t.write(t['title'])
                        if col_b.button("✓", key=f"d_{t['id']}"):
                            complete_task(gtd_map[ctx], t['id'])
                            st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)

    elif selection == "🧠 Esclarecer (Inbox)":
        st.title("🧠 Central de Esclarecimento")
        st.write("Processe o que foi capturado para manter a mente livre.")
        
        paper_notes = get_unprocessed_inbox_notes()
        todo_inbox = get_tasks(inbox_list_id) if inbox_list_id else []
        flagged = get_flagged_emails()

        items = []
        for n in paper_notes: items.append({"type": "PAPEL", "icon": "📝", "title": n['text'], "id": n['text']})
        for t in todo_inbox: 
            if t['status'] != 'completed': items.append({"type": "TO DO", "icon": "✅", "title": t['title'], "id": t['id']})
        for f in flagged: items.append({"type": "E-MAIL", "icon": "📧", "title": f['subject'], "id": f['id']})

        if not items:
            st.success("🎉 Inbox vazia! Bom trabalho.")
        else:
            for item in items[:8]:
                st.markdown(f'''
                <div class="module-card">
                    <div style="display: flex; gap: 15px; align-items: start;">
                        <span style="font-size: 24px;">{item['icon']}</span>
                        <div style="flex-grow: 1;">
                            <span class="status-badge badge-info">{item['type']}</span>
                            <h4 style="margin: 5px 0;">{item['title']}</h4>
                        </div>
                    </div>
                ''', unsafe_allow_html=True)
                
                b1, b2, b3, b4 = st.columns(4)
                if b1.button("✅ 2 min", key=f"do_{item['id']}", use_container_width=True):
                    st.balloons()
                if b2.button("📅 Agendar", key=f"sch_{item['id']}", use_container_width=True): pass
                if b3.button("🤝 Delegar", key=f"del_{item['id']}", use_container_width=True): pass
                if b4.button("📁 Organizar", key=f"org_{item['id']}", use_container_width=True): pass
                st.markdown('</div>', unsafe_allow_html=True)

    elif selection == "📡 Delegação & Equipe":
        st.title("📡 Radar de Delegação")
        plans = get_planner_plans()
        if not plans:
            st.info("Você ainda não faz parte de nenhum plano no Planner.")
        else:
            plan_name = st.selectbox("Selecione o Projeto da FECD", [p['title'] for p in plans])
            sel_plan_id = next(p['id'] for p in plans if p['title'] == plan_name)
            p_tasks = get_planner_tasks_detailed(sel_plan_id)
            
            for pt in p_tasks:
                if pt.get('percentComplete', 0) < 100:
                    badge = '<span class="status-badge badge-success">No Prazo</span>'
                    due = pt.get('dueDateTime')
                    if due and datetime.fromisoformat(due[:19]) < datetime.now():
                        badge = '<span class="status-badge badge-error">ATRASADO</span>'
                    
                    st.markdown(f"""
                        <div class="module-card">
                            <div style="display: flex; justify-content: space-between; align-items: center;">
                                <div>
                                    <h5 style="margin: 0; color: #475569;">{pt.get('bucketName', 'Backlog')}</h5>
                                    <h3 style="margin: 5px 0;">{pt['title']}</h3>
                                </div>
                                {badge}
                            </div>
                        </div>
                    """, unsafe_allow_html=True)

    elif selection == "🖨️ Impressão de Papeis":
        st.title("🖨️ Sincronização Analógica")

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
