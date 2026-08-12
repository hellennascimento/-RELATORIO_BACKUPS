import requests
from requests.auth import HTTPBasicAuth
from datetime import datetime

# --- Configurações da API Acronis ---
CLIENT_ID = '08f48444-b09c-47a5-8e66-5d2225f8c762'
CLIENT_SECRET = 'jc3oedf4x3seer6g643tk2ubg4tgcowm54u77lsbdomduaks7wiu'
DATACENTER_URL = 'https://br01-cloud.acronis.com'

def obter_token():
    auth_url = f"{DATACENTER_URL}/api/2/idp/token"
    dados = {'grant_type': 'client_credentials'}
    resposta = requests.post(auth_url, data=dados, auth=HTTPBasicAuth(CLIENT_ID, CLIENT_SECRET))
    resposta.raise_for_status()
    return resposta.json().get('access_token')

def obter_status_recursos(token):
    url = f"{DATACENTER_URL}/api/resource_management/v4/resource_statuses"
    headers = {'Authorization': f'Bearer {token}'}
    resposta = requests.get(url, headers=headers)
    resposta.raise_for_status()
    return resposta.json().get('items', [])

def gerar_html(linhas_tabela):
    """Gera o arquivo index.html com o visual do painel."""
    data_atualizacao = datetime.now().strftime("%d/%m/%Y às %H:%M:%S")
    
    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Monitoramento de Backups</title>
    <style>
        body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f8f9fa; padding: 20px; color: #333; }}
        .container {{ max-width: 1200px; margin: 0 auto; background: #fff; padding: 20px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }}
        h1 {{ color: #2c3e50; font-size: 24px; border-bottom: 2px solid #3498db; padding-bottom: 10px; }}
        .update-time {{ font-size: 14px; color: #7f8c8d; margin-bottom: 20px; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 20px; font-size: 14px; }}
        th, td {{ padding: 12px 15px; text-align: left; border-bottom: 1px solid #ddd; }}
        th {{ background-color: #3498db; color: #ffffff; text-transform: uppercase; }}
        tr:hover {{ background-color: #f5f5f5; }}
        .status-ok {{ color: #27ae60; font-weight: bold; }}
        .status-error {{ color: #c0392b; font-weight: bold; }}
        .status-warning {{ color: #f39c12; font-weight: bold; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Dashboard de Backups - Acronis</h1>
        <div class="update-time">Última atualização: {data_atualizacao}</div>
        <table>
            <thead>
                <tr>
                    <th>Cliente (Recurso)</th>
                    <th>Tipo</th>
                    <th>Status</th>
                    <th>Último Backup</th>
                </tr>
            </thead>
            <tbody>
                {linhas_tabela}
            </tbody>
        </table>
    </div>
</body>
</html>"""

    with open("index.html", "w", encoding="utf-8") as arquivo:
        arquivo.write(html)
    print("Arquivo 'index.html' gerado com sucesso!")

def main():
    print("Autenticando na API da Acronis...")
    try:
        token = obter_token()
    except Exception as e:
        print(f"Erro na autenticação: {e}")
        return

    print("Buscando dados e gerando painel HTML...\n")
    try:
        recursos = obter_status_recursos(token)
    except Exception as e:
        print(f"Erro ao buscar status: {e}")
        return

    linhas_html = ""

    for item in recursos:
        contexto = item.get('context', {})
        nome = contexto.get('name', 'Desconhecido')
        tipo = contexto.get('type', 'N/A') 
        status_bruto = item.get('aggregate', {}).get('status', 'unknown').lower()
        
        politicas = item.get('policies', [])
        politica_backup = next((p for p in politicas if 'backup' in p.get('type', '')), None)
        
        if politica_backup:
            ultimo_sucesso = politica_backup.get('last_success_run', 'Nenhum backup')
            
            # Formatação visual do status
            classe_css = "status-warning"
            if status_bruto in ['ok', 'success']:
                classe_css = "status-ok"
            elif status_bruto in ['error', 'failed', 'critical']:
                classe_css = "status-error"
                
            linhas_html += f"""
                <tr>
                    <td>{nome}</td>
                    <td>{tipo}</td>
                    <td class="{classe_css}">{status_bruto.upper()}</td>
                    <td>{ultimo_sucesso}</td>
                </tr>"""

    if linhas_html:
        gerar_html(linhas_html)
    else:
        print("Nenhum recurso de backup encontrado para exibir.")

if __name__ == "__main__":
    main()
