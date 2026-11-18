# EPG Xtream Personalizado — KamuflaTV

Este repositório gera e publica diariamente um XMLTV compatível com a **KamuflaTV**.

## Como funciona

1. O gerador autentica na mesma fonte Xtream usada pela área de canais ao vivo da KamuflaTV.
2. Consulta novamente `get_live_streams` em cada execução. Canais adicionados ou removidos na fonte são refletidos automaticamente.
3. Usa primeiro o XMLTV disponibilizado pelo próprio provedor, associando os IDs ao `stream_id` dos canais.
4. Para canais ainda sem programação, consulta fontes brasileiras complementares: IPTV-EPG.org Brasil, Claro TV+, Vivo Play, Globoplay, Pluto TV, +SBT, uma seleção EPG Brasil e XSports.
5. A associação externa prioriza IDs e nomes exatos. Correspondências aproximadas usam um limite alto e exigem diferença suficiente para evitar atribuições duvidosas.
6. Gera `public/epg.xml` com os IDs originais dos canais da KamuflaTV e publica os relatórios de cobertura.

A programação do provedor tem prioridade. As fontes complementares só preenchem canais que ainda não receberam programação, evitando substituir a grade principal por outra fonte. Se uma fonte falhar temporariamente, o gerador também reaproveita programas da EPG anterior que ainda estejam dentro da janela de validade, somente para canais sem dados novos.

## Proteção contra perda de cobertura

Antes de publicar, o gerador compara a nova EPG com a última versão válida. Se a nova geração tiver simultaneamente menos canais cobertos e menos programas, ela é descartada e o XMLTV anterior é mantido. Isso evita que uma falha ou redução temporária na fonte apague uma grade melhor.

## Secrets necessários

Em **Settings → Secrets and variables → Actions**, mantenha estes três secrets apontando para a mesma conta/catálogo de canais ao vivo usada pela KamuflaTV:

| Secret | Conteúdo |
|---|---|
| `XTREAM_BASE_URL` | URL base do servidor, sem `/player_api.php` |
| `XTREAM_USERNAME` | Usuário da conta |
| `XTREAM_PASSWORD` | Senha da conta |

Não coloque credenciais no código, no XMLTV ou em arquivos públicos.

## Atualização automática

O GitHub Actions executa diariamente às **06:20 UTC (03:20 em Brasília)**. Também é possível iniciar uma execução manual em **Actions → Atualizar EPG Xtream → Run workflow**.

O processo publica:
- `public/epg.xml`: guia XMLTV usado pela KamuflaTV;
- `public/canais_com_epg.csv`: canais com programação;
- `public/canais_sem_epg.csv`: canais ainda sem programação;
- `public/canais_xmltv_disponiveis.csv`: IDs e nomes XMLTV encontrados no provedor.

## URL fixa para a KamuflaTV

A KamuflaTV já está configurada para consumir este endereço:

`https://raw.githubusercontent.com/sants2tech/epg-xtream-personalizado/main/public/epg.xml`

O link não muda quando o arquivo é atualizado. Basta o aplicativo carregar novamente o XMLTV.

## Cobertura e limitações

Não é possível criar uma grade correta quando nenhuma fonte publica a programação daquele canal. Nesses casos o canal continua no XML e aparece no relatório `canais_sem_epg.csv`, sem horários ou programas inventados. A cobertura real será informada nos logs de cada execução.

As fontes complementares são projetos independentes e podem alterar disponibilidade, conteúdo ou frequência de atualização. Consulte os repositórios originais:
- [BrazilTVEPG](https://github.com/limaalef/BrazilTVEPG)
- [IPTV-EPG.org](https://iptv-epg.org/guides)
- [IPTV-com EPG](https://github.com/iptv-com/epg)

## Diagnóstico local

```bash
pip install -r requirements.txt
export XTREAM_BASE_URL='https://servidor:porta'
export XTREAM_USERNAME='seu_usuario'
export XTREAM_PASSWORD='sua_senha'
python src/gerar_epg.py
```
