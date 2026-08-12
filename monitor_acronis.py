import os
import json
import re
import requests
from datetime import datetime
from requests.auth import HTTPBasicAuth

# --- Configurações ---
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
OUTPUT_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")

def carregar_config():
    if not os.path.exists(CONFIG_FILE):
        raise FileNotFoundError(f"Arquivo de configuração '{CONFIG_FILE}' não encontrado. Por favor, crie-o antes de executar.")
    
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def obter_token(config):
    auth_url = f"{config['datacenter_url']}/api/2/idp/token"
    dados = {'grant_type': 'client_credentials'}
    resposta = requests.post(
        auth_url, 
        data=dados, 
        auth=HTTPBasicAuth(config['client_id'], config['client_secret'])
    )
    resposta.raise_for_status()
    return resposta.json().get('access_token')

def obter_status_recursos(datacenter_url, token):
    url = f"{datacenter_url}/api/resource_management/v4/resource_statuses"
    headers = {'Authorization': f'Bearer {token}'}
    params = {'limit': 1000}
    
    recursos = []
    pagina = 1
    
    print("Buscando status de recursos na API da Acronis...")
    while True:
        resposta = requests.get(url, headers=headers, params=params)
        resposta.raise_for_status()
        dados_resposta = resposta.json()
        
        items = dados_resposta.get('items', [])
        recursos.extend(items)
        print(f"  Página {pagina}: coletados {len(items)} recursos.")
        
        paging = dados_resposta.get('paging', {})
        cursors = paging.get('cursors', {})
        after = cursors.get('after')
        
        if not after:
            break
            
        params['after'] = after
        pagina += 1
        
    print(f"Total de recursos coletados: {len(recursos)}")
    return recursos

def clean_tenant_name(name):
    if not name or name == "0":
        return "Diversa Tecnologia (Principal)"
    # Limpa o parêntese redundante (ex: "5asec (5asec)" -> "5asec")
    # Limpa também IDs ou códigos no final
    name_clean = re.sub(r'\s*\([^)]*\)\s*$', '', name).strip()
    return name_clean

def classificar_categoria(tipo_recurso):
    t = tipo_recurso.lower()
    if 'machine' in t or 'vm' in t or 'hyperv' in t or 'host' in t:
        return "Servidores / Endpoints (Local/Nuvem)"
    elif any(k in t for k in ['msexchange', 'exchange', 'onedrive', 'sharepoint', 'teams', 'm365', 'office365', 'outlook']):
        return "Microsoft 365"
    elif any(k in t for k in ['google', 'gmail', 'gworkspace', 'gdrive', 'gcalendar', 'gcontacts']):
        return "Google Workspace"
    else:
        return "Servidores / Endpoints (Local/Nuvem)" # Categoria padrão para outros recursos com backup

def is_backup_plan_name(name):
    name_upper = name.upper()
    non_backup_keywords = [
        'ACESSO REMOTO', 
        'ANTIVIRUS', 
        'ANTI-VIRUS', 
        'ANTIVÍRUS',
        'ANTIVIRIUS',
        'EDR',
        'RMM', 
        'DLP', 
        'PATCH MANAGEMENT', 
        'VULNERABILITY',
        'MONITORAMENTO',
        'REMOTE DESKTOP',
        'REMOTE ACCESS',
        'FIREWALL',
        'SCRIPT',
        'REGRA',
        'EXECUÇÃO',
        'EXECUTA',
        'MONITORING',
        'SECURITY',
        'SUPORTE'
    ]
    for kw in non_backup_keywords:
        if kw in name_upper:
            # Se o plano contiver palavras-chave de não-backup, mas também contiver palavras explícitas de backup, consideramos backup
            if any(k in name_upper for k in ['BACKUP', 'NUVEM', 'LOCAL', 'EXTERNO', 'COPIA', 'CÓPIA']):
                continue
            return False
    return True

def processar_dados(recursos):
    dados_processados = []
    
    for item in recursos:
        policies = item.get('policies', [])
        # Procuramos apenas recursos que tenham política de backup ativa
        politica_backup = next((p for p in policies if 'backup' in p.get('type', '').lower()), None)
        
        if not politica_backup:
            continue
            
        ctx = item.get('context', {})
        tipo_recurso = ctx.get('type', 'N/A')
        
        # Filtro e extração do plano de backup
        nomes_planos = item.get('aggregate', {}).get('names', '')
        planos_brutos = [p.strip() for p in nomes_planos.split(';')] if nomes_planos else []
        planos_backup = [p for p in planos_brutos if is_backup_plan_name(p)]
        
        # Se não tiver nenhum plano de backup real associado, ignoramos o recurso
        if not planos_backup:
            continue
            
        plano = " / ".join(planos_backup)
        nome_recurso = ctx.get('name', 'Desconhecido')
        tenant_name = ctx.get('tenant_name', '')
        
        # Mapeamento e limpeza do nome do cliente
        cliente = clean_tenant_name(tenant_name)
        
        # Classificação da Categoria
        categoria = classificar_categoria(tipo_recurso)
        
        # Calcular status do backup diretamente comparando last_run e last_success_run da política de backup
        last_run = politica_backup.get('last_run')
        last_success = politica_backup.get('last_success_run')
        
        if not last_run:
            status = "Alerta"
            status_bruto = "idle"
        elif last_run == last_success:
            status = "Sucesso"
            status_bruto = "success"
        else:
            status = "Falha"
            status_bruto = "error"
            
        # Data do último backup de sucesso e próximo backup
        ultimo_sucesso = politica_backup.get('last_success_run')
        proximo_backup = politica_backup.get('next_run')
        
        dados_processados.append({
            "cliente": cliente,
            "recurso": nome_recurso,
            "tipo_recurso": tipo_recurso,
            "categoria": categoria,
            "status": status,
            "status_bruto": status_bruto.lower(),
            "plano": plano,
            "ultimo_backup": ultimo_sucesso,
            "proximo_backup": proximo_backup
        })
        
    return dados_processados

def obter_atividades_saas(datacenter_url, token):
    from datetime import datetime, timezone, timedelta
    url = f"{datacenter_url}/api/task_manager/v2/activities"
    headers = {'Authorization': f'Bearer {token}'}
    
    # Filtro dos últimos 3 dias
    data_limite = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    
    # UUIDs de tipos de atividades de backup SaaS (M365 / Google Workspace)
    types = [
        '0016750A-EE56-49E6-BF59-7C768C9869AC',
        '019CF690-803B-451F-9E4B-B76C478BC05F'
    ]
    
    activities = []
    print("Buscando atividades de backup SaaS (últimos 3 dias)...")
    
    for t_type in types:
        params = {
            'completedAt': f'ge({data_limite})',
            'type': t_type,
            'limit': 1000
        }
        while True:
            resposta = requests.get(url, headers=headers, params=params)
            resposta.raise_for_status()
            data = resposta.json()
            items = data.get('items', [])
            activities.extend(items)
            
            paging = data.get('paging', {})
            cursors = paging.get('cursors', {})
            after = cursors.get('after')
            if not after or len(items) < 1000:
                break
            params = {'after': after, 'limit': 1000}
            
    print(f"Total de atividades SaaS coletadas: {len(activities)}")
    return activities

def processar_dados_saas(activities):
    saas_groups = {}
    for a in activities:
        ctx = a.get('context', {})
        suite = ctx.get('suite', '')
        if suite not in ['o365', 'google', 'gworkspace']:
            continue
            
        cli = clean_tenant_name(a.get('tenant', {}).get('name', ''))
        res_name = a.get('resource', {}).get('name', '')
        policy_name = a.get('policy', {}).get('name', '')
        
        if policy_name and not is_backup_plan_name(policy_name):
            continue
            
        key = (cli, res_name, policy_name)
        if key not in saas_groups:
            saas_groups[key] = []
        saas_groups[key].append(a)
        
    dados_saas = []
    for (cli, res_name, policy_name), list_acts in saas_groups.items():
        list_acts.sort(key=lambda x: x.get('completedAt', '') or x.get('createdAt', ''), reverse=True)
        latest = list_acts[0]
        
        suite = latest.get('context', {}).get('suite', '')
        res_type = latest.get('resource', {}).get('type', 'SaaS')
        
        if suite in ['google', 'gworkspace']:
            categoria = "Google Workspace"
        else:
            categoria = "Microsoft 365"
            
        state = latest.get('state')
        result_code = latest.get('result', {}).get('code', '')
        
        if state == 'completed':
            if result_code == 'ok':
                status = "Sucesso"
                status_bruto = "success"
            else:
                status = "Falha"
                status_bruto = "error"
        else:
            status = "Alerta"
            status_bruto = "warning"
            
        ultimo_sucesso = None
        for a in list_acts:
            if a.get('state') == 'completed' and a.get('result', {}).get('code') == 'ok':
                ultimo_sucesso = a.get('completedAt')
                break
                
        dados_saas.append({
            "cliente": cli,
            "recurso": res_name,
            "tipo_recurso": f"SaaS ({res_type})",
            "categoria": categoria,
            "status": status,
            "status_bruto": status_bruto,
            "plano": policy_name if policy_name else "Plano C2C",
            "ultimo_backup": ultimo_sucesso,
            "proximo_backup": None
        })
        
    return dados_saas

def gerar_html(dados_backups):
    data_atualizacao = datetime.now().strftime("%d/%m/%Y às %H:%M:%S")
    json_dados = json.dumps(dados_backups, indent=2, ensure_ascii=False)
    
    html_content = f"""<!DOCTYPE html>
<html lang="pt-BR" data-theme="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Painel de Backups Acronis - Diversa Tecnologia</title>
    <!-- Google Fonts -->
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg-primary: #0f172a;
            --bg-secondary: #1e293b;
            --bg-tertiary: #334155;
            --text-primary: #f8fafc;
            --text-secondary: #94a3b8;
            --border-color: #334155;
            --accent-color: #38bdf8;
            --accent-glow: rgba(56, 189, 248, 0.15);
            --card-shadow: 0 4px 20px -2px rgba(0, 0, 0, 0.3);
            
            --success-bg: rgba(16, 185, 129, 0.15);
            --success-text: #34d399;
            --success-border: rgba(16, 185, 129, 0.3);
            
            --danger-bg: rgba(239, 68, 68, 0.15);
            --danger-text: #f87171;
            --danger-border: rgba(239, 68, 68, 0.3);
            
            --warning-bg: rgba(245, 158, 11, 0.15);
            --warning-text: #fbbf24;
            --warning-border: rgba(245, 158, 11, 0.3);
            
            --info-bg: rgba(59, 130, 246, 0.15);
            --info-text: #60a5fa;
            --info-border: rgba(59, 130, 246, 0.3);
        }}

        [data-theme="light"] {{
            --bg-primary: #f8fafc;
            --bg-secondary: #ffffff;
            --bg-tertiary: #f1f5f9;
            --text-primary: #0f172a;
            --text-secondary: #475569;
            --border-color: #e2e8f0;
            --accent-color: #0284c7;
            --accent-glow: rgba(2, 132, 199, 0.1);
            --card-shadow: 0 4px 20px -2px rgba(0, 0, 0, 0.05);
            
            --success-bg: #d1fae5;
            --success-text: #065f46;
            --success-border: #a7f3d0;
            
            --danger-bg: #fee2e2;
            --danger-text: #7f1d1d;
            --danger-border: #fca5a5;
            
            --warning-bg: #fef3c7;
            --warning-text: #78350f;
            --warning-border: #fde68a;
            
            --info-bg: #dbeafe;
            --info-text: #1e3a8a;
            --info-border: #bfdbfe;
        }}

        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
            font-family: 'Inter', sans-serif;
            transition: background-color 0.2s, border-color 0.2s;
        }}

        body {{
            background-color: var(--bg-primary);
            color: var(--text-primary);
            min-height: 100vh;
            padding: 24px;
        }}

        .container {{
            max-width: 1400px;
            margin: 0 auto;
        }}

        /* Header */
        header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 24px;
            padding-bottom: 20px;
            border-bottom: 1px solid var(--border-color);
        }}

        .header-title h1 {{
            font-size: 24px;
            font-weight: 700;
            background: linear-gradient(to right, var(--text-primary), var(--accent-color));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 4px;
        }}

        .header-title p {{
            font-size: 13px;
            color: var(--text-secondary);
        }}

        .header-controls {{
            display: flex;
            align-items: center;
            gap: 16px;
        }}

        .theme-toggle {{
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            color: var(--text-primary);
            padding: 10px;
            border-radius: 8px;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
        }}

        .theme-toggle:hover {{
            background: var(--bg-tertiary);
        }}

        /* KPI Cards */
        .kpi-container {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }}

        .kpi-card {{
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 20px;
            box-shadow: var(--card-shadow);
            position: relative;
            overflow: hidden;
        }}

        .kpi-card::before {{
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            width: 4px;
            height: 100%;
            background: var(--border-color);
        }}

        .kpi-card.kpi-total::before {{ background: var(--accent-color); }}
        .kpi-card.kpi-success::before {{ background: #10b981; }}
        .kpi-card.kpi-warning::before {{ background: #f59e0b; }}
        .kpi-card.kpi-danger::before {{ background: #ef4444; }}

        .kpi-title {{
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: var(--text-secondary);
            margin-bottom: 8px;
        }}

        .kpi-value {{
            font-size: 28px;
            font-weight: 700;
            margin-bottom: 4px;
        }}

        .kpi-desc {{
            font-size: 11px;
            color: var(--text-secondary);
        }}

        /* Filters Toolbar */
        .toolbar {{
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 24px;
            box-shadow: var(--card-shadow);
            display: flex;
            flex-direction: column;
            gap: 16px;
        }}

        .toolbar-row-1 {{
            display: flex;
            gap: 16px;
            flex-wrap: wrap;
        }}

        .search-container {{
            flex: 1;
            min-width: 250px;
            position: relative;
        }}

        .search-input {{
            width: 100%;
            padding: 11px 16px 11px 40px;
            background: var(--bg-primary);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            color: var(--text-primary);
            font-size: 14px;
            outline: none;
        }}

        .search-input:focus {{
            border-color: var(--accent-color);
            box-shadow: 0 0 0 3px var(--accent-glow);
        }}

        .search-icon {{
            position: absolute;
            left: 14px;
            top: 50%;
            transform: translateY(-50%);
            fill: var(--text-secondary);
            width: 16px;
            height: 16px;
        }}

        .select-wrapper {{
            min-width: 200px;
        }}

        .select-input {{
            width: 100%;
            padding: 11px 16px;
            background: var(--bg-primary);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            color: var(--text-primary);
            font-size: 14px;
            outline: none;
            cursor: pointer;
            appearance: none;
            background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%2394a3b8'%3E%3Cpath d='M7 10l5 5 5-5z'/%3E%3C/svg%3E");
            background-repeat: no-repeat;
            background-position: right 12px center;
            background-size: 20px;
        }}

        .select-input:focus {{
            border-color: var(--accent-color);
        }}

        .toolbar-row-2 {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 16px;
            border-top: 1px solid var(--border-color);
            padding-top: 16px;
        }}

        .filter-group {{
            display: flex;
            align-items: center;
            gap: 8px;
            flex-wrap: wrap;
        }}

        .filter-label {{
            font-size: 12px;
            font-weight: 600;
            color: var(--text-secondary);
            text-transform: uppercase;
        }}

        .btn-group {{
            display: flex;
            background: var(--bg-primary);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 3px;
        }}

        .btn-filter {{
            background: transparent;
            border: none;
            color: var(--text-secondary);
            padding: 6px 12px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 12px;
            font-weight: 500;
        }}

        .btn-filter.active {{
            background: var(--bg-secondary);
            color: var(--text-primary);
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
        }}

        .btn-export {{
            background: var(--bg-primary);
            border: 1px solid var(--border-color);
            color: var(--text-primary);
            padding: 10px 16px;
            border-radius: 8px;
            cursor: pointer;
            font-size: 13px;
            font-weight: 500;
            display: flex;
            align-items: center;
            gap: 8px;
        }}

        .btn-export:hover {{
            background: var(--bg-tertiary);
        }}

        /* Table Card */
        .table-card {{
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            box-shadow: var(--card-shadow);
            overflow: hidden;
            margin-bottom: 24px;
        }}

        .table-wrapper {{
            overflow-x: auto;
            width: 100%;
        }}

        table {{
            width: 100%;
            border-collapse: collapse;
            text-align: left;
            font-size: 13px;
        }}

        th {{
            background: var(--bg-secondary);
            padding: 14px 20px;
            font-weight: 600;
            color: var(--text-secondary);
            border-bottom: 2px solid var(--border-color);
            text-transform: uppercase;
            font-size: 11px;
            letter-spacing: 0.05em;
            cursor: pointer;
            user-select: none;
        }}

        th:hover {{
            color: var(--text-primary);
        }}

        th.sort-asc::after {{ content: ' ▲'; font-size: 9px; }}
        th.sort-desc::after {{ content: ' ▼'; font-size: 9px; }}

        td {{
            padding: 14px 20px;
            border-bottom: 1px solid var(--border-color);
            color: var(--text-primary);
            vertical-align: middle;
        }}

        tr:last-child td {{
            border-bottom: none;
        }}

        tr:hover td {{
            background-color: var(--bg-tertiary);
        }}

        /* Badges */
        .badge {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 4px 10px;
            border-radius: 9999px;
            font-size: 11px;
            font-weight: 600;
            border: 1px solid transparent;
        }}

        .badge-dot {{
            width: 6px;
            height: 6px;
            border-radius: 50%;
        }}

        .badge-success {{
            background: var(--success-bg);
            color: var(--success-text);
            border-color: var(--success-border);
        }}
        .badge-success .badge-dot {{ background: var(--success-text); }}

        .badge-danger {{
            background: var(--danger-bg);
            color: var(--danger-text);
            border-color: var(--danger-border);
        }}
        .badge-danger .badge-dot {{ background: var(--danger-text); }}

        .badge-warning {{
            background: var(--warning-bg);
            color: var(--warning-text);
            border-color: var(--warning-border);
        }}
        .badge-warning .badge-dot {{ background: var(--warning-text); }}

        .badge-info {{
            background: var(--info-bg);
            color: var(--info-text);
            border-color: var(--info-border);
        }}
        .badge-info .badge-dot {{ background: var(--info-text); }}

        /* Backup Type Badge */
        .type-badge {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            color: var(--text-primary);
            font-weight: 500;
        }}

        .type-icon {{
            width: 16px;
            height: 16px;
            fill: var(--text-secondary);
        }}

        .stale-text {{
            color: var(--danger-text);
            font-weight: 600;
        }}

        /* Pagination */
        .pagination {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 16px 20px;
            background: var(--bg-secondary);
            border-top: 1px solid var(--border-color);
            flex-wrap: wrap;
            gap: 16px;
        }}

        .page-info {{
            font-size: 12px;
            color: var(--text-secondary);
        }}

        .page-controls {{
            display: flex;
            align-items: center;
            gap: 8px;
        }}

        .btn-page {{
            background: var(--bg-primary);
            border: 1px solid var(--border-color);
            color: var(--text-primary);
            padding: 6px 12px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 12px;
        }}

        .btn-page:disabled {{
            opacity: 0.5;
            cursor: not-allowed;
        }}

        .btn-page:not(:disabled):hover {{
            background: var(--bg-tertiary);
        }}

        .no-data {{
            padding: 40px;
            text-align: center;
            color: var(--text-secondary);
            font-size: 14px;
        }}

        @media (max-width: 768px) {{
            body {{ padding: 12px; }}
            header {{ flex-direction: column; align-items: flex-start; gap: 16px; }}
            .header-controls {{ width: 100%; justify-content: space-between; }}
            .toolbar-row-2 {{ flex-direction: column; align-items: flex-start; }}
            .btn-export {{ width: 100%; justify-content: center; }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="header-title">
                <h1>Painel de Backups Acronis</h1>
                <p>Status Funcional • Última atualização: {data_atualizacao}</p>
            </div>
            <div class="header-controls">
                <button class="theme-toggle" id="themeToggle" onclick="toggleTheme()" title="Alternar Tema">
                    <!-- Sun Icon (Light Mode) -->
                    <svg id="sunIcon" style="display:none;" xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"></circle><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"></svg>
                    <!-- Moon Icon (Dark Mode) -->
                    <svg id="moonIcon" xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a6.8 6.8 0 0 0 9 9 9 9 0 1 1-9-9Z"></svg>
                </button>
            </div>
        </header>

        <!-- KPI Cards -->
        <div class="kpi-container">
            <div class="kpi-card kpi-total">
                <div class="kpi-title">Clientes Monitorados</div>
                <div class="kpi-value" id="kpiClientes">-</div>
                <div class="kpi-desc">Total de sub-tenants ativos</div>
            </div>
            <div class="kpi-card kpi-total" style="--accent-color: #818cf8;">
                <div class="kpi-title">Total de Backups</div>
                <div class="kpi-value" id="kpiTotal">-</div>
                <div class="kpi-desc">Recursos protegidos monitorados</div>
            </div>
            <div class="kpi-card kpi-success">
                <div class="kpi-title">Backups OK</div>
                <div class="kpi-value" id="kpiSuccess" style="color: #10b981;">-</div>
                <div class="kpi-desc" id="kpiSuccessPerc">-% do total</div>
            </div>
            <div class="kpi-card kpi-danger">
                <div class="kpi-title">Erros / Falhas</div>
                <div class="kpi-value" id="kpiDanger" style="color: #ef4444;">-</div>
                <div class="kpi-desc" id="kpiDangerPerc">-% do total</div>
            </div>
            <div class="kpi-card kpi-warning">
                <div class="kpi-title">Alertas / Atrasados</div>
                <div class="kpi-value" id="kpiWarning" style="color: #f59e0b;">-</div>
                <div class="kpi-desc" id="kpiWarningDesc">Ociosos ou sem rodar > 24h</div>
            </div>
        </div>

        <!-- Filters Toolbar -->
        <div class="toolbar">
            <div class="toolbar-row-1">
                <div class="search-container">
                    <input type="text" id="searchInput" class="search-input" placeholder="Pesquisar por cliente, recurso ou plano..." oninput="handleFilterChange()">
                    <svg class="search-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M15.5 14h-.79l-.28-.27C15.41 12.59 16 11.11 16 9.5 16 5.91 13.09 3 9.5 3S3 5.91 3 9.5 5.91 16 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/></svg>
                </div>
                <div class="select-wrapper">
                    <select id="clientFilter" class="select-input" onchange="handleFilterChange()">
                        <option value="">Todos os Clientes</option>
                    </select>
                </div>
            </div>
            <div class="toolbar-row-2">
                <div class="filter-group">
                    <span class="filter-label">Serviço:</span>
                    <div class="btn-group">
                        <button class="btn-filter active" onclick="setServiceFilter('all', this)">Todos</button>
                        <button class="btn-filter" onclick="setServiceFilter('Servidores / Endpoints (Local/Nuvem)', this)">Servidores / PC</button>
                        <button class="btn-filter" onclick="setServiceFilter('Microsoft 365', this)">Microsoft 365</button>
                        <button class="btn-filter" onclick="setServiceFilter('Google Workspace', this)">Google Workspace</button>
                    </div>
                </div>
                <div class="filter-group">
                    <span class="filter-label">Status:</span>
                    <div class="btn-group">
                        <button class="btn-filter active" onclick="setStatusFilter('all', this)">Todos</button>
                        <button class="btn-filter" onclick="setStatusFilter('Sucesso', this)">Sucesso</button>
                        <button class="btn-filter" onclick="setStatusFilter('Falha', this)">Falha</button>
                        <button class="btn-filter" onclick="setStatusFilter('Alerta', this)">Alerta</button>
                    </div>
                </div>
                <button class="btn-export" onclick="exportToCSV()">
                    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg>
                    Exportar CSV
                </button>
            </div>
        </div>

        <!-- Table Card -->
        <div class="table-card">
            <div class="table-wrapper">
                <table id="backupsTable">
                    <thead>
                        <tr>
                            <th id="th-cliente" class="sort-asc" onclick="sortTable('cliente')">Cliente</th>
                            <th id="th-recurso" onclick="sortTable('recurso')">Recurso / Dispositivo</th>
                            <th id="th-categoria" onclick="sortTable('categoria')">Tipo de Backup</th>
                            <th id="th-plano" onclick="sortTable('plano')">Plano de Proteção</th>
                            <th id="th-status" onclick="sortTable('status')">Status</th>
                            <th id="th-ultimo_backup" onclick="sortTable('ultimo_backup')">Último Backup Bem-Sucedido</th>
                        </tr>
                    </thead>
                    <tbody id="tableBody">
                        <!-- Linhas dinâmicas -->
                    </tbody>
                </table>
            </div>
            
            <div class="pagination">
                <div class="page-info" id="pageInfo">Exibindo 0-0 de 0 registros</div>
                <div class="page-controls">
                    <span class="page-info">Itens por página:</span>
                    <select id="pageSize" class="select-input" style="padding: 5px 30px 5px 10px; min-width: 80px; font-size: 12px; width: auto; margin-right: 12px;" onchange="changePageSize()">
                        <option value="25">25</option>
                        <option value="50">50</option>
                        <option value="100">100</option>
                        <option value="all">Todos</option>
                    </select>
                    <button class="btn-page" id="btnPrev" onclick="prevPage()">Anterior</button>
                    <button class="btn-page" id="btnNext" onclick="nextPage()">Próximo</button>
                </div>
            </div>
        </div>
    </div>

    <!-- Dados injetados pelo Python -->
    <script>
        const BACKUP_DATA = {json_dados};
    </script>

    <!-- Lógica JavaScript do Dashboard -->
    <script>
        let currentData = [...BACKUP_DATA];
        let filteredData = [...BACKUP_DATA];
        
        // Filtros Ativos
        let filterSearch = "";
        let filterClient = "";
        let filterService = "all";
        let filterStatus = "all";
        
        // Paginação
        let currentPage = 1;
        let pageSize = 25;
        
        // Ordenação
        let sortColumn = "cliente";
        let sortDirection = "asc";

        // SVG Icons Inline
        const icons = {{
            server: `<svg class="type-icon" viewBox="0 0 24 24"><path d="M20 18c1.1 0 1.99-.9 1.99-2L22 6c0-1.1-.9-2-2-2H4c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2H0v2h24v-2h-4zM4 6h16v10H4V6z"/></svg>`,
            m365: `<svg class="type-icon" style="fill: #ec4899;" viewBox="0 0 24 24"><path d="M19.35 10.04C18.67 6.59 15.64 4 12 4 9.11 4 6.6 5.64 5.35 8.04 2.34 8.36 0 10.91 0 14c0 3.31 2.69 6 6 6h13c2.76 0 5-2.24 5-5 0-2.64-2.05-4.78-4.65-4.96zM19 18H6c-2.21 0-4-1.79-4-4 0-2.05 1.53-3.76 3.56-3.97l1.07-.11.5-.95C8.08 7.14 9.94 6 12 6c2.62 0 4.88 1.86 5.39 4.43l.3 1.5 1.53.11c1.56.1 2.78 1.41 2.78 2.96 0 1.65-1.35 3-3 3z"/></svg>`,
            google: `<svg class="type-icon" style="fill: #ea4335;" viewBox="0 0 24 24"><path d="M12.24 10.285V13.4h6.887c-.275 1.565-1.88 4.604-6.887 4.604-4.33 0-7.866-3.577-7.866-8s3.536-8 7.866-8c2.46 0 4.105 1.025 5.047 1.926l2.427-2.334C17.955 2.192 15.34 1 12.24 1 5.48 1 0 6.48 0 13.2s5.48 12.2 12.24 12.2c7.055 0 11.75-4.943 11.75-11.943 0-.814-.088-1.433-.192-2.172H12.24z"/></svg>`
        }};

        // Inicialização
        document.addEventListener("DOMContentLoaded", () => {{
            popularClientesFilter();
            aplicarFiltros();
            updateThemeUI();
        }});

        function popularClientesFilter() {{
            const select = document.getElementById("clientFilter");
            const clientesUnicos = [...new Set(BACKUP_DATA.map(d => d.cliente))].sort();
            
            clientesUnicos.forEach(cliente => {{
                const opt = document.createElement("option");
                opt.value = cliente;
                opt.textContent = cliente;
                select.appendChild(opt);
            }});
        }}

        function setServiceFilter(service, btn) {{
            filterService = service;
            updateActiveButton(btn);
            aplicarFiltros();
        }}

        function setStatusFilter(status, btn) {{
            filterStatus = status;
            updateActiveButton(btn);
            aplicarFiltros();
        }}

        function updateActiveButton(activeBtn) {{
            // Obtém todos os botões do mesmo container e remove a classe 'active'
            const parent = activeBtn.parentElement;
            const buttons = parent.querySelectorAll('.btn-filter');
            buttons.forEach(btn => btn.classList.remove('active'));
            activeBtn.classList.add('active');
        }}

        function handleFilterChange() {{
            filterSearch = document.getElementById("searchInput").value.toLowerCase();
            filterClient = document.getElementById("clientFilter").value;
            aplicarFiltros();
        }}

        function aplicarFiltros() {{
            filteredData = BACKUP_DATA.filter(item => {{
                // Busca de texto
                const matchesSearch = !filterSearch || 
                    item.cliente.toLowerCase().includes(filterSearch) || 
                    item.recurso.toLowerCase().includes(filterSearch) || 
                    item.plano.toLowerCase().includes(filterSearch);
                
                // Cliente
                const matchesClient = !filterClient || item.cliente === filterClient;
                
                // Categoria (Serviço)
                const matchesService = filterService === 'all' || item.categoria === filterService;
                
                // Status
                let matchesStatus = false;
                if (filterStatus === 'all') {{
                    matchesStatus = true;
                }} else if (filterStatus === 'Sucesso') {{
                    matchesStatus = item.status === 'Sucesso';
                }} else if (filterStatus === 'Falha') {{
                    matchesStatus = item.status === 'Falha';
                }} else if (filterStatus === 'Alerta') {{
                    matchesStatus = item.status === 'Alerta';
                }}
                
                return matchesSearch && matchesClient && matchesService && matchesStatus;
            }});

            currentPage = 1;
            renderDashboard();
        }}

        function sortData() {{
            filteredData.sort((a, b) => {{
                let valA = a[sortColumn] || '';
                let valB = b[sortColumn] || '';
                
                if (sortColumn === 'ultimo_backup') {{
                    valA = valA ? new Date(valA).getTime() : 0;
                    valB = valB ? new Date(valB).getTime() : 0;
                }} else {{
                    valA = valA.toString().toLowerCase();
                    valB = valB.toString().toLowerCase();
                }}

                if (valA < valB) return sortDirection === 'asc' ? -1 : 1;
                if (valA > valB) return sortDirection === 'asc' ? 1 : -1;
                return 0;
            }});
        }}

        function sortTable(column) {{
            const th = document.getElementById(`th-${{column}}`);
            
            // Remove sort classes from all headers
            const ths = document.querySelectorAll('th');
            ths.forEach(t => {{
                if (t.id !== `th-${{column}}`) {{
                    t.classList.remove('sort-asc', 'sort-desc');
                }}
            }});

            if (sortColumn === column) {{
                sortDirection = sortDirection === 'asc' ? 'desc' : 'asc';
                th.classList.toggle('sort-asc', sortDirection === 'asc');
                th.classList.toggle('sort-desc', sortDirection === 'desc');
            }} else {{
                sortColumn = column;
                sortDirection = 'asc';
                th.classList.add('sort-asc');
            }}

            renderDashboard();
        }}

        function getRelativeTime(dateString) {{
            if (!dateString) return 'Nenhum backup';
            
            const date = new Date(dateString);
            const now = new Date();
            const diffMs = now - date;
            const diffMin = Math.round(diffMs / 60000);
            const diffHrs = Math.round(diffMs / 3600000);
            const diffDays = Math.round(diffMs / 86400000);

            if (diffMs < 0) {{
                return 'Recentemente';
            }}
            
            if (diffMin < 60) {{
                return `há ${{diffMin}} min`;
            }} else if (diffHrs < 24) {{
                return `há ${{diffHrs}} hora${{diffHrs > 1 ? 's' : ''}}`;
            }} else {{
                return `há ${{diffDays}} dia${{diffDays > 1 ? 's' : ''}}`;
            }}
        }}

        function formatDateTime(dateString) {{
            if (!dateString) return 'Nenhum backup realizado';
            const date = new Date(dateString);
            return date.toLocaleString('pt-BR', {{
                day: '2-digit',
                month: '2-digit',
                year: 'numeric',
                hour: '2-digit',
                minute: '2-digit'
            }});
        }}

        function renderDashboard() {{
            sortData();
            updateKPIs();

            const tbody = document.getElementById("tableBody");
            tbody.innerHTML = "";

            const total = filteredData.length;
            
            if (total === 0) {{
                tbody.innerHTML = `<tr><td colspan="6" class="no-data">Nenhum backup encontrado para os filtros selecionados.</td></tr>`;
                document.getElementById("pageInfo").textContent = "Exibindo 0 de 0 registros";
                document.getElementById("btnPrev").disabled = true;
                document.getElementById("btnNext").disabled = true;
                return;
            }}

            const limit = pageSize === 'all' ? total : parseInt(pageSize);
            const start = (currentPage - 1) * limit;
            const end = Math.min(start + limit, total);
            const paginatedData = filteredData.slice(start, end);

            paginatedData.forEach(item => {{
                const tr = document.createElement("tr");
                
                // Tipo Icon e Categoria
                let iconHtml = icons.server;
                if (item.categoria === 'Microsoft 365') iconHtml = icons.m365;
                if (item.categoria === 'Google Workspace') iconHtml = icons.google;
                
                // Badge de Status
                let badgeClass = "badge-info";
                if (item.status === 'Sucesso') badgeClass = "badge-success";
                if (item.status === 'Falha') badgeClass = "badge-danger";
                if (item.status === 'Alerta') badgeClass = "badge-warning";
                
                const statusBadge = `<span class="badge ${{badgeClass}}"><span class="badge-dot"></span>${{item.status.toUpperCase()}}</span>`;
                
                // Cálculo de Backup Atrasado (> 24 horas)
                let dateDisplay = formatDateTime(item.ultimo_backup);
                let relativeDisplay = getRelativeTime(item.ultimo_backup);
                let isStale = false;
                
                if (item.ultimo_backup) {{
                    const diffHours = (new Date() - new Date(item.ultimo_backup)) / 3600000;
                    if (diffHours > 24) {{
                        isStale = true;
                    }}
                }} else {{
                    isStale = true;
                }}

                const dateClass = isStale ? 'stale-text' : '';
                const timeCell = `
                    <div class="${{dateClass}}">${{dateDisplay}}</div>
                    <div style="font-size: 11px; color: var(--text-secondary); margin-top: 2px;">${{relativeDisplay}}</div>
                `;

                tr.innerHTML = `
                    <td style="font-weight: 600;">${{item.cliente}}</td>
                    <td>
                        <div style="font-weight: 500;">${{item.recurso}}</div>
                        <div style="font-size: 10px; color: var(--text-secondary); margin-top: 2px;">${{item.tipo_recurso}}</div>
                    </td>
                    <td>
                        <span class="type-badge">
                            ${{iconHtml}}
                            <span>${{item.categoria}}</span>
                        </span>
                    </td>
                    <td style="color: var(--text-secondary); max-width: 250px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${{item.plano}}">
                        ${{item.plano}}
                    </td>
                    <td>${{statusBadge}}</td>
                    <td>${{timeCell}}</td>
                `;
                tbody.appendChild(tr);
            }});

            // Controles de página
            document.getElementById("pageInfo").textContent = `Exibindo ${{start + 1}}-${{end}} de ${{total}} registros`;
            document.getElementById("btnPrev").disabled = currentPage === 1;
            document.getElementById("btnNext").disabled = end >= total;
        }}

        function updateKPIs() {{
            const total = BACKUP_DATA.length;
            const uniqueClients = [...new Set(BACKUP_DATA.map(d => d.cliente))].length;
            
            const success = BACKUP_DATA.filter(d => d.status === 'Sucesso').length;
            const danger = BACKUP_DATA.filter(d => d.status === 'Falha').length;
            
            // Calculo de alertas considerando ociosidade do status E backups atrasados > 24 horas
            let warningsCount = 0;
            BACKUP_DATA.forEach(d => {{
                if (d.status === 'Alerta') {{
                    warningsCount++;
                }} else if (d.ultimo_backup) {{
                    const diffHours = (new Date() - new Date(d.ultimo_backup)) / 3600000;
                    if (diffHours > 24 && d.status !== 'Falha') {{
                        warningsCount++;
                    }}
                }} else {{
                    if (d.status !== 'Falha') warningsCount++;
                }}
            }});

            document.getElementById("kpiClientes").textContent = uniqueClients;
            document.getElementById("kpiTotal").textContent = total;
            document.getElementById("kpiSuccess").textContent = success;
            document.getElementById("kpiDanger").textContent = danger;
            document.getElementById("kpiWarning").textContent = warningsCount;

            const successPerc = total > 0 ? Math.round((success / total) * 100) : 0;
            const dangerPerc = total > 0 ? Math.round((danger / total) * 100) : 0;

            document.getElementById("kpiSuccessPerc").textContent = `${{successPerc}}% do total`;
            document.getElementById("kpiDangerPerc").textContent = `${{dangerPerc}}% do total`;
        }}

        // Navegação de páginas
        function changePageSize() {{
            pageSize = document.getElementById("pageSize").value;
            currentPage = 1;
            renderDashboard();
        }}

        function prevPage() {{
            if (currentPage > 1) {{
                currentPage--;
                renderDashboard();
            }}
        }}

        function nextPage() {{
            const total = filteredData.length;
            const limit = pageSize === 'all' ? total : parseInt(pageSize);
            if (currentPage * limit < total) {{
                currentPage++;
                renderDashboard();
            }}
        }}

        // Alternar tema
        function toggleTheme() {{
            const html = document.documentElement;
            const currentTheme = html.getAttribute("data-theme");
            const newTheme = currentTheme === "dark" ? "light" : "dark";
            html.setAttribute("data-theme", newTheme);
            updateThemeUI();
        }}

        function updateThemeUI() {{
            const isDark = document.documentElement.getAttribute("data-theme") === "dark";
            document.getElementById("sunIcon").style.display = isDark ? "none" : "block";
            document.getElementById("moonIcon").style.display = isDark ? "block" : "none";
        }}

        // Exportação CSV
        function exportToCSV() {{
            let csvContent = "data:text/csv;charset=utf-8,\\ufeff";
            csvContent += "Cliente;Recurso;Tipo de Backup;Plano de Proteção;Status;Ultimo Backup\\n";

            filteredData.forEach(item => {{
                const ultimoBackupStr = item.ultimo_backup ? formatDateTime(item.ultimo_backup) : 'Nenhum backup';
                const row = [
                    item.cliente,
                    item.recurso,
                    item.categoria,
                    item.plano,
                    item.status,
                    ultimoBackupStr
                ].map(text => `"${{text.replace(/"/g, '""')}}"`).join(";");
                
                csvContent += row + "\\n";
            }});

            const encodedUri = encodeURI(csvContent);
            const link = document.createElement("a");
            link.setAttribute("href", encodedUri);
            link.setAttribute("download", `relatorio_backups_${{new Date().toISOString().slice(0, 10)}}.csv`);
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
        }}
    </script>
</body>
</html>"""
    
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"Painel gerado com sucesso em '{OUTPUT_HTML}'!")

def main():
    print("Iniciando Monitoramento de Backups Acronis...")
    try:
        config = carregar_config()
    except Exception as e:
        print(f"Erro ao carregar configurações: {e}")
        return
        
    try:
        token = obter_token(config)
        print("Autenticado na API Acronis com sucesso.")
    except Exception as e:
        print(f"Erro ao autenticar: {e}")
        return

    # 1. Obter recursos de workloads locais e virtuais (v4/resource_statuses)
    try:
        recursos = obter_status_recursos(config['datacenter_url'], token)
        print("Processando dados de backup (Workloads padrão)...")
        dados_backups = processar_dados(recursos)
        print(f"Total de recursos de backup padrão processados: {len(dados_backups)}")
    except Exception as e:
        print(f"Erro ao obter ou processar workloads padrão: {e}")
        dados_backups = []

    # 2. Obter recursos de nuvem (M365 / Google Workspace) via Task Manager activities
    try:
        atividades = obter_atividades_saas(config['datacenter_url'], token)
        print("Processando dados de backup (SaaS / Nuvem)...")
        dados_saas = processar_dados_saas(atividades)
        print(f"Total de backups SaaS processados: {len(dados_saas)}")
        dados_backups.extend(dados_saas)
    except Exception as e:
        print(f"Erro ao obter ou processar backups SaaS: {e}")

    print(f"Total consolidado de backups para o painel: {len(dados_backups)}")

    print("Gerando painel HTML interativo...")
    gerar_html(dados_backups)
    print("Processo concluído com sucesso!")

if __name__ == "__main__":
    main()
