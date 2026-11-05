# EPG Xtream Personalizado

Gera diariamente um XMLTV com todos os canais ao vivo da conta Xtream configurada. O processo tenta obter o guia completo em `xmltv.php`, associa os canais por `epg_channel_id` e, quando necessário, pelo nome. Se não encontrar programas no XMLTV geral, tenta `get_short_epg` individualmente.

## 1. Criar o repositório

1. No GitHub, crie um repositório chamado `epg-xtream-personalizado`.
2. Para usar o endereço Raw diretamente no aplicativo IPTV, o repositório precisa ser **público**. O código não contém credenciais; elas ficam em Secrets.
3. Envie todos os arquivos deste pacote para a raiz do repositório.

> Atenção: o XMLTV publicado será público. Ele contém nomes de canais, logos e programação, mas não deve conter usuário, senha ou links de transmissão.

## 2. Configurar os Secrets

No repositório, abra **Settings → Secrets and variables → Actions → New repository secret** e crie:

| Nome do Secret | Valor |
|---|---|
| `XTREAM_BASE_URL` | URL base do servidor, incluindo protocolo e porta, sem `/player_api.php` |
| `XTREAM_USERNAME` | Seu usuário Xtream |
| `XTREAM_PASSWORD` | Sua senha Xtream |

Exemplo de formato para a URL base: `https://servidor.exemplo:porta`. Não coloque credenciais dentro da URL.

## 3. Executar o primeiro teste

1. Abra **Actions → Atualizar EPG Xtream**.
2. Clique em **Run workflow**.
3. Abra a execução e confira os logs.
4. Se terminar com sucesso, confira `public/epg.xml` no repositório.

O agendamento está configurado para todos os dias às 06:20 UTC (03:20 em Brasília). O GitHub pode atrasar execuções agendadas em períodos de alta carga.

## 4. URL para o aplicativo IPTV

Depois da primeira execução bem-sucedida, use:

`https://raw.githubusercontent.com/SEU_USUARIO/epg-xtream-personalizado/main/public/epg.xml`

Substitua `SEU_USUARIO` pelo seu nome de usuário do GitHub. No IPTV Smarters, procure a configuração de EPG remoto/XMLTV e cole esse endereço.

## 5. Limitações importantes

- O sistema não cria programação do nada. Ele organiza o EPG que o provedor realmente disponibiliza.
- O `xmltv.php` e `get_short_epg` podem estar desativados ou limitados pelo provedor.
- A associação automática depende de `epg_channel_id` ou de nomes suficientemente parecidos. Canais sem correspondência continuam no XMLTV, mas sem programas.
- Este primeiro pacote não consulta bases externas de programação. Uma segunda etapa pode adicionar fontes XMLTV públicas/licenciadas e uma tabela de correspondência manual.
- O arquivo XMLTV é público se o repositório for público. Não publique playlist M3U nem URLs de transmissão com credenciais.

## Execução local para diagnóstico

```bash
pip install -r requirements.txt
export XTREAM_BASE_URL='https://servidor:porta'
export XTREAM_USERNAME='seu_usuario'
export XTREAM_PASSWORD='sua_senha'
python src/gerar_epg.py
```
