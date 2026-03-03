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
GTD_CONTEXT_LISTS = ["Escritório", "Computador", "Telefone", "Na Rua", "Assuntos a Tratar"]
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

@st.cache_data(ttl=600)
def get_todo_lists(token):
    url = f"{GRAPH_BASE}/me/todo/lists"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(url, headers=headers, timeout=20)
    return r.json().get("value", []) if r.status_code == 200 else []

@st.cache_data(ttl=300)
def get_tasks(token, list_id):
    # Expandimos linkedResources para pegar links de e-mail originais
    url = f"{GRAPH_BASE}/me/todo/lists/{list_id}/tasks?$expand=linkedResources"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(url, headers=headers, timeout=20)
    return r.json().get("value", []) if r.status_code == 200 else []

@st.cache_data(ttl=300)
def get_flagged_emails(token):
    url = f"{GRAPH_BASE}/me/messages"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"$filter": "flag/flagStatus eq 'flagged'", "$top": "30"}
    r = requests.get(url, headers=headers, params=params, timeout=20)
    return r.json().get("value", []) if r.status_code == 200 else []

@st.cache_data(ttl=3600)
def get_planner_plans(token):
    url = f"{GRAPH_BASE}/me/planner/plans"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(url, headers=headers, timeout=20)
    return r.json().get("value", []) if r.status_code == 200 else []

def get_planner_buckets(token, plan_id):
    url = f"{GRAPH_BASE}/planner/plans/{plan_id}/buckets"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(url, headers=headers, timeout=20)
    return r.json().get("value", []) if r.status_code == 200 else []

@st.cache_data(ttl=600)
def get_planner_tasks_detailed(token, plan_id):
    url = f"{GRAPH_BASE}/planner/plans/{plan_id}/tasks"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(url, headers=headers, timeout=20)
    tasks = r.json().get("value", []) if r.status_code == 200 else []
    buckets = get_planner_buckets(token, plan_id)
    b_map = {b['id']: b['name'] for b in buckets}
    for t in tasks: t['bucketName'] = b_map.get(t.get('bucketId'), 'Desconhecido')
    return tasks

def move_todo_task(token, source_list_id, task_id, target_list_id, title=None):
    # ESTRATÉGIA "ARRASTAR E SOLTAR":
    # Em vez de clonar ou usar o /move instável, apenas atualizamos a lista da tarefa.
    # Isso preserva anexos, e-mails vinculados e todas as propriedades originais.
    # Nota: A API do Microsoft Graph requer que o move seja feito via PATCH ou POST /move.
    # Como /move falha para e-mails, usamos o PATCH se disponível ou o fluxo de clonagem de recursos como fallback de segurança.
    
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    
    # Tentativa de Real Move via API que preserva anexos (POST /move)
    url_move = f"{GRAPH_BASE}/me/todo/lists/{source_list_id}/tasks/{task_id}/move"
    payload_move = {"targetListId": target_list_id}
    r = requests.post(url_move, headers=headers, json=payload_move, timeout=20)
    
    if r.status_code in [200, 201, 204]:
        return True

    # FALLBACK INTELIGENTE (Cópia profunda de recursos se o move falhar)
    # Buscamos a tarefa original com todos os metadados inclusive os links de e-mail (linkedResources)
    get_url = f"{GRAPH_BASE}/me/todo/lists/{source_list_id}/tasks/{task_id}?$expand=linkedResources"
    r_get = requests.get(get_url, headers=headers, timeout=10)
    if r_get.status_code != 200: return False
    
    task_data = r_get.json()
    linked_resources = task_data.get('linkedResources', [])
    
    # Criamos a nova tarefa no destino
    payload_create = {
        "title": task_data.get('title'),
        "body": task_data.get('body'),
        "importance": task_data.get('importance'),
        "dueDateTime": task_data.get('dueDateTime'),
        "reminderDateTime": task_data.get('reminderDateTime')
    }
    
    r_create = requests.post(f"{GRAPH_BASE}/me/todo/lists/{target_list_id}/tasks", headers=headers, json=payload_create, timeout=20)
    if r_create.status_code not in [200, 201]: return False
    
    new_task_id = r_create.json()['id']
    
    # REPLICA OS ANEXOS/LINKS DE E-MAIL (O "Vínculo Sagrado")
    for res in linked_resources:
        requests.post(f"{GRAPH_BASE}/me/todo/lists/{target_list_id}/tasks/{new_task_id}/linkedResources", 
                      headers=headers, json={
                          "webUrl": res.get('webUrl'),
                          "applicationName": res.get('applicationName'),
                          "displayName": res.get('displayName'),
                          "externalId": res.get('externalId')
                      }, timeout=10)
    
    # Deletamos a original apenas após a cópia bem sucedida
    requests.delete(f"{GRAPH_BASE}/me/todo/lists/{source_list_id}/tasks/{task_id}", headers=headers, timeout=10)
    return True

def create_planner_task_detailed(token, plan_id, bucket_id, title):
    url = f"{GRAPH_BASE}/planner/tasks"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"planId": plan_id, "bucketId": bucket_id, "title": title}
    r = requests.post(url, headers=headers, json=payload, timeout=20)
    return r.json() if r.status_code == 201 else None

def delete_todo_task(token, list_id, task_id):
    url = f"{GRAPH_BASE}/me/todo/lists/{list_id}/tasks/{task_id}"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.delete(url, headers=headers, timeout=20)
    return r.status_code == 204

def get_outlook_folder_id(token, folder_name):
    # Busca profunda (recursiva) por pastas no Outlook
    headers = {"Authorization": f"Bearer {token}"}
    
    def search_folders(url):
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code != 200: return None
        data = r.json().get("value", [])
        for f in data:
            if f['displayName'].lower() == folder_name.lower():
                return f['id']
            if f.get('childFolderCount', 0) > 0:
                child_url = f"{GRAPH_BASE}/me/mailFolders/{f['id']}/childFolders"
                found = search_folders(child_url)
                if found: return found
        return None

    return search_folders(f"{GRAPH_BASE}/me/mailFolders")

def move_outlook_email(token, message_id, folder_name):
    # Move e-mail para pasta específica (com busca profunda)
    f_id = get_outlook_folder_id(token, folder_name)
    if not f_id: 
        print(f"DEBUG: Pasta {folder_name} não encontrada.")
        return False
    url = f"{GRAPH_BASE}/me/messages/{message_id}/move"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"destinationId": f_id}
    r = requests.post(url, headers=headers, json=payload, timeout=20)
    return r.status_code == 201

def add_todo_link(token, list_id, task_id, web_url, label):
    # Adiciona um link (LinkedResource) na tarefa do To Do
    url = f"{GRAPH_BASE}/me/todo/lists/{list_id}/tasks/{task_id}/linkedResources"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"webUrl": web_url, "displayName": label}
    requests.post(url, headers=headers, json=payload, timeout=10)

def create_planner_project(token, title):
    # Cria Plano (Projeto) e retorna ID
    # Para criar um plano precisamos de um Group ID. Vamos pegar o primeiro grupo do usuário.
    g_url = f"{GRAPH_BASE}/me/memberOf"
    headers = {"Authorization": f"Bearer {token}"}
    grps = requests.get(g_url, headers=headers).json().get("value", [])
    if not grps: return None
    g_id = grps[0]['id']
    
    url = f"{GRAPH_BASE}/planner/plans"
    payload = {"owner": g_id, "title": title}
    r = requests.post(url, headers=headers, json=payload, timeout=20)
    if r.status_code != 201: return None
    p_id = r.json()['id']
    
    # Criar Buckets Padrão
    for b_name in ["Backlog", "Proxima Ação", "Delegado"]:
        requests.post(f"{GRAPH_BASE}/planner/buckets", headers=headers, json={"name": b_name, "planId": p_id})
    return p_id

def complete_task(list_id, task_id):
    return graph_request("PATCH", f"/me/todo/lists/{list_id}/tasks/{task_id}", payload={"status": "completed"})

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
        selection = st.radio("Menu de Navegação", ["📊 Dashboard Completo", "🧠 Central de Esclarecer", "🤝 Projetos e Delegação", "🖨️ Assistente de Impressão", "📤 Upload de Scan", "📖 Guia do Ecossistema"], label_visibility="collapsed")
        st.divider()
        if st.button("🔄 Sincronizar Tudo", use_container_width=True):
            st.cache_data.clear()
            st.success("Sincronização forçada! Dados atualizados.")
            st.rerun()
        if st.button("🚪 Sair", use_container_width=True):
            del st.session_state["token"]; st.rerun()

    token = get_access_token()
    all_lists = get_todo_lists(token)
    
    # Identificação da Inbox Principal (Suporte a PT-BR 'Tarefas' e 'Tasks')
    inbox_list_id = next((l['id'] for l in all_lists if l['wellknownListName'] == "defaultList"), None)
    if not inbox_list_id:
        inbox_list_id = next((l['id'] for l in all_lists if l['displayName'].lower() in ["tasks", "tarefas"]), None)
    
    # Mapeamento robusto (ignora maiúsculas/minúsculas e espaços extras)
    gtd_map = {}
    for l in all_lists:
        d_name = l['displayName'].strip().lower()
        for ctx_predefined in GTD_CONTEXT_LISTS:
            if d_name == ctx_predefined.lower():
                gtd_map[ctx_predefined] = l['id']
                break

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
                tasks = get_tasks(token, gtd_map[ctx])
                active = [t for t in tasks if t['status'] != 'completed']
                if not active: st.success("🎉 Tudo limpo por aqui!")
                for t in active:
                    t_col, b_col = st.columns([0.85, 0.15])
                    t_col.write(t['title'])
                    if b_col.button("✓", key=f"dash_comp_{t['id']}"):
                        complete_task(gtd_map[ctx], t['id'])
                        st.cache_data.clear() # Limpa cache para refletir a conclusão
                        st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)

    elif selection == "🧠 Central de Esclarecer":
        st.title("🧠 Esclarecer (Capturas)")
        st.write("Decida o destino de cada captura: Contexto (To Do) ou Projeto (Planner).")
        
        t_inbox, t_paper, t_email = st.tabs(["📥 Inbox To Do", "📝 Notas de Papel", "📧 E-mails com Flag"])
        plans = get_planner_plans(token)
        
        # Função auxiliar para renderizar o formulário de esclarecimento
        def render_clarify_form(item_id, item_title, source_type, source_id=None, linked_msg_id=None):
            with st.container(border=True):
                st.markdown(f"**{item_title}**")
                if linked_msg_id:
                    # Se recebermos um URL completo, usamos ele direto. 
                    # Senão, usamos o novo formato de ID da Microsoft.
                    email_url = linked_msg_id if linked_msg_id.startswith("http") else f"https://outlook.office.com/mail/id/{linked_msg_id}"
                    st.markdown(f"[📧 Abrir E-mail Original]({email_url})")
                
                c_ctx, c_prj, c_act = st.columns([1, 1, 0.6])
                
                with c_ctx:
                    target_ctx = st.selectbox("Mover p/ Contexto", ["-- Selecionar --"] + GTD_CONTEXT_LISTS, key=f"ctx_{source_type}_{item_id}")
                    if target_ctx != "-- Selecionar --":
                        if st.button("Confirmar Contexto", key=f"btn_ctx_{source_type}_{item_id}", type="primary"):
                            target_id = gtd_map.get(target_ctx)
                            if not target_id:
                                st.error(f"❌ Erro: Lista '{target_ctx}' não encontrada no seu To Do. Crie-a ou tente sincronizar.")
                                return
                                
                            st.toast(f"🔄 Processando: {item_title}...")
                            
                            # 1. Se for e-mail, move o e-mail no Outlook PRIMEIRO
                            # Fazemos isso antes para garantir que a mensagem ainda tenha o vínculo da flag ativo
                            if linked_msg_id:
                                move_outlook_email(token, linked_msg_id, "@Ações")
                            
                            # 2. Move a tarefa no To Do (usando o move que preserva anexos)
                            success = False
                            if source_type in ["todo", "email"]:
                                if move_todo_task(token, source_id, item_id, target_id, title=item_title):
                                    success = True
                            elif source_type == "paper":
                                if graph_request("POST", f"/me/todo/lists/{target_id}/tasks", payload={"title": item_title}):
                                    mark_note_as_processed(item_title)
                                    success = True
                            
                            if success:
                                st.success("🚀 Item movido com sucesso!"); st.cache_data.clear(); st.rerun()
                            else:
                                st.error("⚠️ Falha ao mover. Verifique se o item ainda existe no Microsoft To Do.")

                with c_prj:
                    p_opts = ["-- Selecionar Projeto --", "🆕 + Criar Novo Projeto"] + [p['title'] for p in plans]
                    target_proj = st.selectbox("Assignar Proyecto", p_opts, key=f"prj_{source_type}_{item_id}")
                    
                    if target_proj == "🆕 + Criar Novo Projeto":
                        new_p_name = st.text_input("Nome do Novo Projeto", key=f"newp_{source_type}_{item_id}")
                        if st.button("Criar e Mover", key=f"btn_newp_{source_type}_{item_id}"):
                            p_id = create_planner_project(token, new_p_name)
                            if p_id:
                                # Procura bucket 'Proxima Ação' no novo projeto
                                bkts = get_planner_buckets(token, p_id)
                                b_id = next((b['id'] for b in bkts if "proxima" in b['name'].lower()), bkts[0]['id'])
                                create_planner_task(token, p_id, b_id, item_title)
                                # Lógica de movimentação To Do / Email
                                if source_type == "todo": delete_todo_task(token, source_id, item_id)
                                if source_type == "paper": mark_note_as_processed(item_title)
                                if source_type == "email" and linked_msg_id: 
                                    move_outlook_email(token, linked_msg_id, "@Ações")
                                    delete_todo_task(token, source_id, item_id)
                                st.success("Projeto Criado!"); st.cache_data.clear(); st.rerun()
                    
                    elif target_proj != "-- Selecionar Projeto --":
                        p_sel = next(p for p in plans if p['title'] == target_proj)
                        buckets = get_planner_buckets(token, p_sel['id'])
                        b_opts = {b['name']: b['id'] for b in buckets}
                        target_b = st.selectbox("Bucket", list(b_opts.keys()), key=f"bkt_{source_type}_{item_id}")
                        if st.button("Mover p/ Projeto", key=f"btn_prj_{source_type}_{item_id}"):
                            folder = "@Aguardando Resposta" if "delegado" in target_b.lower() else "@Ações"
                            
                            # 1. Move e-mail no Outlook PRIMEIRO
                            if linked_msg_id: move_outlook_email(token, linked_msg_id, folder)
                            
                            # 2. Cria tarefa no Planner
                            p_task = create_planner_task_detailed(token, p_sel['id'], b_opts[target_b], item_title)
                            if p_task:
                                target_list_name = "Aguardando resposta" if "delegado" in target_b.lower() else "Projetos"
                                target_l_id = next((l['id'] for l in all_lists if l['displayName'].lower() == target_list_name.lower()), None)
                                
                                final_task_id = None
                                if source_type in ["todo", "email"]:
                                    if target_l_id: 
                                        move_todo_task(token, source_id, item_id, target_l_id, title=item_title)
                                        final_task_id = item_id
                                    else: delete_todo_task(token, source_id, item_id)
                                elif source_type == "paper":
                                    mark_note_as_processed(item_title)
                                    if target_l_id:
                                        res = graph_request("POST", f"/me/todo/lists/{target_l_id}/tasks", payload={"title": item_title})
                                        final_task_id = res.get('id')

                                if final_task_id and target_l_id:
                                    planner_url = f"https://tasks.office.com/fecd.org.br/Home/Task/{p_task['id']}"
                                    add_todo_link(token, target_l_id, final_task_id, planner_url, f"Ver no Planner: {p_sel['title']}")

                                st.success("Projeto Atualizado!"); st.cache_data.clear(); st.rerun()

                with c_act:
                    st.write("") # Espaçador
                    col_done, col_trash = st.columns(2)
                    if col_done.button("✓", key=f"done_{source_type}_{item_id}", help="Concluir"):
                        if source_type == "todo" or source_type == "email": 
                            complete_task(source_id, item_id)
                            if linked_msg_id: move_outlook_email(token, linked_msg_id, "@Concluídos")
                        if source_type == "paper": mark_note_as_processed(item_title)
                        st.cache_data.clear(); st.rerun()
                    if col_trash.button("🗑️", key=f"trash_{source_type}_{item_id}", help="Descartar"):
                        if source_type == "todo" or source_type == "email": delete_todo_task(token, source_id, item_id)
                        if source_type == "paper": mark_note_as_processed(item_title)
                        st.cache_data.clear(); st.rerun()

        with t_inbox:
            if inbox_list_id:
                inbox_tasks = get_tasks(token, inbox_list_id)
                pending_inbox = [t for t in inbox_tasks if t['status'] != 'completed']
                if not pending_inbox: st.success("Inbox limpa! Bom trabalho.")
                for it in pending_inbox:
                    render_clarify_form(it['id'], it['title'], "todo", inbox_list_id)
        
        with t_paper:
            paper_notes = get_unprocessed_inbox_notes()
            if not paper_notes: st.info("Sem notas de papel pendentes.")
            for pn in paper_notes:
                render_clarify_form(hashlib.md5(pn['text'].encode()).hexdigest(), pn['text'], "paper")
        
        with t_email:
            # Lista 'flaggedEmails' do To Do
            email_list_id = next((l['id'] for l in all_lists if l['wellknownListName'] == "flaggedEmails"), None)
            if email_list_id:
                email_tasks = get_tasks(token, email_list_id)
                pending_emails = [t for t in email_tasks if t['status'] != 'completed']
                if not pending_emails: st.info("Sem e-mails sinalizados pendentes.")
                for et in pending_emails:
                    # Tenta extrair o LINK DIRETO da mensagem (webUrl é o mais confiável)
                    m_ref = None
                    if 'linkedResources' in et:
                        for lr in et['linkedResources']:
                            m_ref = lr.get('webUrl') or lr.get('externalId')
                            if m_ref: break
                    render_clarify_form(et['id'], et['title'], "email", email_list_id, m_ref)
            else:
                st.warning("Lista de e-mails sinalizados não encontrada no To Do.")

    elif selection == "🤝 Projetos e Delegação":
        st.title("🤝 Gestão de Projetos e Delegação")
        st.write("Acompanhe o progresso dos seus projetos no Planner e o status das delegações.")
        
        plans = get_planner_plans(token)
        if not plans: st.warning("Nenhum projeto encontrado no Planner.")
        else:
            # Seleção de Projeto com Progresso
            p_names = [p['title'] for p in plans]
            p_name = st.selectbox("Selecione o Projeto para Detalhamento", p_names)
            p_selected = next(p for p in plans if p['title'] == p_name)
            
            p_tasks = get_planner_tasks_detailed(token, p_selected['id'])
            
            # Cálculo de Progresso
            total = len(p_tasks)
            concluidas = sum(1 for t in p_tasks if t.get('percentComplete', 0) == 100)
            progresso = (concluidas / total) if total > 0 else 0
            
            st.markdown(f"### {p_name}")
            st.progress(progresso, text=f"Progresso do Projeto: {int(progresso*100)}% ({concluidas}/{total} tarefas)")
            
            col_todo, col_waiting = st.columns(2)
            
            with col_todo:
                st.subheader("📝 Próximas Ações do Projeto")
                pending = [t for t in p_tasks if t.get('percentComplete', 0) < 100]
                if not pending: st.success("Nenhuma ação pendente neste projeto!")
                for pt in pending:
                    badge = "pill-urgent" if pt.get('dueDateTime') and pt['dueDateTime'][:10] < date.today().isoformat() else "pill-normal"
                    st.markdown(f'<div class="fecd-card"><span class="status-pill {badge}">{pt["bucketName"]}</span><h4 style="margin-top:10px; font-size:14px;">{pt["title"]}</h4></div>', unsafe_allow_html=True)

            with col_waiting:
                st.subheader("🤝 Status de Delegação")
                # Filtra tarefas que estão em buckets que indicam delegação ou tem responsáveis
                waiting = [t for t in p_tasks if "Aguardando" in t.get('bucketName', '') or "Delegado" in t.get('bucketName', '')]
                if not waiting: st.info("Nenhuma tarefa marcada especificamente como delegação.")
                for wt in waiting:
                    st.markdown(f'<div class="fecd-card" style="border-left: 4px solid #f59e0b;"><h4 style="font-size:14px;">{wt["title"]}</h4><small>{wt["bucketName"]}</small></div>', unsafe_allow_html=True)

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
                        ts = get_tasks(token, ctx_id)
                        tasks_raw[ctx_n] = [{"title": t['title'], "selected": True} for t in ts if t['status'] != 'completed']

                    # Buscar Planner (Delegadas e Projetos) com Regras GTD
                    plans = get_planner_plans(token)
                    planner_raw = [] # Para Delegação
                    tasks_raw["💡 PROJETOS (Planner)"] = [] # Novo contexto para ações de projetos
                    
                    today_str = date.today().isoformat()
                    temp_planner = []

                    if plans:
                        for p in plans:
                            pts = get_planner_tasks_detailed(token, p['id'])
                            for pt in pts:
                                if pt.get('percentComplete', 0) < 100:
                                    b_name = pt.get('bucketName', '').lower()
                                    
                                    # Pula tudo que for Backlog (Planejamento futuro)
                                    if "backlog" in b_name: continue
                                    
                                    due_val = pt.get('dueDateTime')
                                    is_overdue = (due_val[:10] < today_str) if due_val else False
                                    is_today = (due_val[:10] == today_str) if due_val else False
                                    
                                    item_data = {
                                        "title": pt['title'],
                                        "plan": p['title'],
                                        "bucket": pt.get('bucketName', 'Geral'),
                                        "selected": False,
                                        "id": pt['id'],
                                        "overdue": is_overdue,
                                        "today": is_today,
                                        "due": due_val or "9999-12-31"
                                    }

                                    # Regra de Destino baseada no Bucket
                                    if "proxima" in b_name or "próxima" in b_name or "acao" in b_name or "ação" in b_name:
                                        # Vai para Próximas Ações do papel
                                        item_data['selected'] = True # Próximas ações tendem a ser foco imediato
                                        tasks_raw["💡 PROJETOS (Planner)"].append(item_data)
                                    elif "delegado" in b_name or "aguardando" in b_name:
                                        # Vai para Radar de Delegação
                                        temp_planner.append(item_data)
                    
                    # Priorização e Seleção automática para Delegação (Radar)
                    temp_planner.sort(key=lambda x: (-int(x['today']), -int(x['overdue']), x['due']))
                    for idx, item in enumerate(temp_planner):
                        if idx < 5: item['selected'] = True
                        planner_raw.append(item)

                    st.session_state.sync_data = {
                        "calendar": [{"subject": e['subject'], "time": e['start']['dateTime'][11:16], "selected": True} for e in evs],
                        "tasks": tasks_raw,
                        "planner": planner_raw
                    }
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

                st.markdown("#### 🤝 Radar de Delegação (Planner)")
                if sd.get('planner'):
                    for k, pk in enumerate(sd['planner']):
                        label = pk['title']
                        if pk['today']: label = f"⭐ {label} (HOJE)"
                        elif pk['overdue']: label = f"🔴 {label} (ATRASADO)"
                        pk['selected'] = st.checkbox(f"{label} @ {pk['plan']}", value=pk['selected'], key=f"f_pk_{k}")
                else:
                    st.write("Nenhuma tarefa delegada ativa.")

                if st.form_submit_button("🚀 Confirmar e Gerar PDF"):
                    final_cal = [e for e in sd['calendar'] if e['selected']]
                    
                    # Processar tarefas (To Do + Projetos Planner)
                    final_tasks = {}
                    for ctx, tl in sd['tasks'].items():
                        selected_for_ctx = []
                        for t in tl:
                            if t.get('selected'):
                                # Se for do Planner, anexa o nome do projeto ao título
                                title = t['title']
                                if "plan" in t: title = f"{title} [{t['plan']}]"
                                selected_for_ctx.append({"title": title})
                        if selected_for_ctx:
                            final_tasks[ctx] = selected_for_ctx
                    
                    final_waiting = []
                    if sd.get('planner'):
                        for pk in sd['planner']:
                            if pk['selected']:
                                final_waiting.append({
                                    "task": pk['title'],
                                    "plan": pk['plan'],
                                    "bucket": pk['bucket'],
                                    "overdue": pk['overdue']
                                })
                    
                    st.session_state.final_gtd_data = {
                        "date": date.today().strftime("%d/%m/%Y"),
                        "page_id": f"FECD-{int(time.time())}",
                        "calendar": final_cal,
                        "tasks": final_tasks,
                        "waiting": final_waiting
                    }
                    st.session_state.wizard_step = 3; st.rerun()
            if st.button("⬅️ Cancelar"): st.session_state.wizard_step = 1; st.rerun()

        elif st.session_state.wizard_step == 3:
            st.success("Tudo pronto! Sua folha foi preparada.")
            fdata = st.session_state.final_gtd_data
            save_page_snapshot(fdata["page_id"], fdata)
            pdf_buf = generate_gtd_page(fdata)
            pdf_val = pdf_buf.getvalue()
            # JS para forçar abertura em nova aba (contornando bloqueios de data-uri)
            import base64 as b64_lib
            b64_pdf = b64_lib.b64encode(pdf_val).decode('utf-8')
            
            st.markdown(f"""
                <script>
                function openPdf() {{
                    var byteCharacters = atob("{b64_pdf}");
                    var byteNumbers = new Array(byteCharacters.length);
                    for (var i = 0; i < byteCharacters.length; i++) {{
                        byteNumbers[i] = byteCharacters.charCodeAt(i);
                    }}
                    var byteArray = new Uint8Array(byteNumbers);
                    var file = new Blob([byteArray], {{type: 'application/pdf;base64'}});
                    var fileURL = URL.createObjectURL(file);
                    window.open(fileURL);
                }}
                </script>
                <div style="background-color: #2563eb; color: white; padding: 18px; border-radius: 12px; text-align: center; font-weight: 800; cursor: pointer; margin-bottom: 12px;" onclick="openPdf()">
                    📄 ABRIR PDF FECD PARA IMPRIMIR
                </div>
            """, unsafe_allow_html=True)
            
            st.download_button("⬇️ Salvar PDF (Link Direto)", pdf_val, file_name=f"Tarefas_FECD_{fdata['page_id']}.pdf", use_container_width=True)
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

    elif selection == "📖 Guia do Ecossistema":
        st.title("📖 Manual do Ecossistema GTD (FECD)")
        st.markdown("""
        Este é o guia de configuração para garantir que o Microsoft 365 e o seu fluxo analógico (Papel) funcionem em total harmonia.
        
        ---
        ### 1. Microsoft To Do: Contextos de Execução
        O To Do é onde vivem as **Próximas Ações** de um único passo. O aplicativo busca exatamente estes nomes:
        - **Lista `Tasks` (ou Tarefas):** Sua **Inbox**. Todo scan processado cai aqui.
        - **Listas de Contexto:**
            - `Escritório`: Ações físicas na empresa.
            - `Computador`: Exigem internet/softwares.
            - `Telefone`: Ligações e WhatsApp rápido.
            - `Na Rua`: Recados externos.
            - `Assuntos a Tratar`: Pautas para reuniões e conversas.
        
        ---
        ### 2. Microsoft Planner: Estratégia e Projetos
        Cada **Plano** no Planner deve ser um **Projeto**. Organize-os em 3 colunas (Buckets):
        - **Bucket `Backlog`:** Planejamento futuro (O App ignora no papel).
        - **Bucket `Proxima Ação`:** Mova para cá o que deve ser feito **hoje**. Aparecerá na seção "💡 PROJETOS" do papel.
        - **Bucket `Delegado` ou `Aguardando`:** Tarefas para terceiros. Aparecerá no "RADAR DE DELEGAÇÃO" do papel.
        
        ---
        ### 3. Microsoft Outlook: Captura de E-mails
        - **Sinalizar (Flag):** Se um e-mail exige ação > 2 min, coloque a bandeirinha.
        - **Processamento:** Use a aba `🧠 Central de Esclarecer` no App para transformá-los em tarefas ou passos de projeto.
        
        ---
        ### 4. Ciclo Sistêmico (App + Papel)
        1. **Capturar:** Use a caixa `📥 CAPTURA RÁPIDA` no papel timbrado.
        2. **Esclarecer:** Use o `📤 Upload de Scan` para extrair notas e processá-las.
        3. **Organizar:** Mova tarefas para os buckets ou listas de contexto.
        4. **Refletir:** Use o menu `🤝 Projetos e Delegação` para ver as barras de progresso.
        5. **Executar:** Gere o PDF com o assistente, foque no que está no papel e risque com caneta.
        
        ---
        ### ⚠️ Avisos Importantes:
        - O app ignora a lista **Casa** para manter o foco profissional.
        - Se criar uma lista nova, use o botão **🔄 Sincronizar Tudo** na lateral.
        - O QR Code da página é único e serve para vincular suas notas manuscritas ao dia correto.
        """)

if __name__ == "__main__":
    q = st.query_params
    if "code" in q and "token" not in st.session_state:
        cid, tid, csec, ruri = get_azure_config()
        r = requests.post(f"{AUTH_BASE}/{tid}/oauth2/v2.0/token", data={"client_id": cid, "grant_type": "authorization_code", "code": q["code"], "redirect_uri": ruri, "scope": " ".join(SCOPES), "client_secret": csec})
        st.session_state["token"] = r.json()
        st.session_state["token_expires_at"] = time.time() + int(r.json().get("expires_in", 3600))
        st.query_params.clear(); st.rerun()
    main()
