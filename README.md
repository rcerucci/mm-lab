# mm-lab

## Português
Laboratório de pesquisa de market making: observa dados públicos da Hyperliquid e Lighter e simula estratégias BTC/HYPE perp com capital inicial fictício de US$100 por braço.

**Somente leitura nas exchanges. Não envia ordens reais. Não precisa de carteira, chaves, API key ou credenciais.** Escreve dados locais em `data/`. Não é recomendação de investimento. Fills, fila, latência e seleção adversa são aproximações; lucro simulado não garante lucro real. Na Lighter, atividade serve como proxy quando não há PnL confiável.

### Executar (Linux, Python 3.10+)
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python lab.py
```
`lab.py` inicia observer e simulador juntos. Em outro terminal:
```bash
.venv/bin/python painel.py
.venv/bin/python painel.py --once
.venv/bin/python status.py
```
Saia do painel com `q`, `Q`, `Esc` ou Ctrl+C. Dados começam vazios; aguarde acumular histórico. Horários: America/Sao_Paulo.

Opcional: `ROOT="$PWD" bash start.sh` inicia um supervisor em background que reinicia o laboratório se ele sair. Para encerrar, confira os processos e envie SIGTERM primeiro ao PID em `data/supervisor.pid`, depois ao PID em `data/lab.pid`. `ROOT="$PWD" bash install_painel.sh` instala um atalho e altera o PATH em `~/.bashrc`; não é necessário para executar diretamente.

### Arquivos
- `observer.py`: coleta pública de livros, trades e makers.
- `simulator.py`: simulação, braços A/B/C/D, FIX/CMP e vizinhos 5/20 bps.
- `lab.py`: integra observer, simulador, persistência e dashboard.
- `painel.py`, `status.py`: visualização terminal e resumo.
- `config.py`, `storage.py`: parâmetros e armazenamento.
- `tests/`: testes offline do simulador e dos helpers do observer (`python -m pytest -q tests`).
- `PRE_REGISTRO.md`: desenho e limitações do experimento original; sigma é o ambiente original, não um requisito.

A publicação exclui ambientes virtuais, bancos, logs, estados, histórico, PIDs, backups e credenciais. Nunca adicione segredos. Nenhum modo de trading real é habilitado.

## English
Read-only market-making research lab using public Hyperliquid and Lighter data, simulating BTC/HYPE perpetual strategies with fictitious US$100 initial capital per arm. **No real orders, wallet, private keys, API keys or account credentials are required.** Only local files under `data/` are written.

Use the Linux/Python 3.10+ commands above: create a virtual environment, install requirements, then run `lab.py` for observer and simulator together. In another terminal, use `painel.py` for the dashboard, `painel.py --once` or `status.py` for snapshots. Exit the dashboard with q/Q/Esc/Ctrl+C. Fresh runs need time to accumulate history. Timezone: America/Sao_Paulo.

A/B/C/D compare fixed/daily/three-day/weekly recalibration. FIX/CMP compare fixed and equity-linked simulated inventory caps; A has 5/20 bps neighbors. See config and the Portuguese pre-registration. Fills, queue position, latency and observed maker PnL are imperfect approximations; after the 5 Nov 2026 restarts, cancelled quotes stay fillable for ~280 ms on a list of `[effective_at, retire_at)` intervals, inventory caps use gross notional, reducing fills are allowed even above the cap, a trade-through is a tag on the paper fill (queue zeroed, remaining size filled), mid history is LOCF with a gap cutoff rather than linear interpolation, and Lighter presence (not seeded snaps or fills) is the activity proxy when reliable PnL is unavailable. Old saved quotes missing beyond-visible flags load as False; a mid tail older than 3s does not resolve AS; an empty Hyperliquid userFills window stays unknown rather than a known-zero loss; trade ids without `tid` key off `(hash, px, sz, time)`; a post-fill sister requote is skipped when sides and sizes are unchanged (timer/jump/pause still replace). This is research, not investment advice or evidence of live profitability.

Runtime data, virtual environments, PIDs, backups and credentials are excluded. Never add secrets. No live trading is enabled.
