# Pré-registro — mm-lab (observer + simulador perp US$100 1x)

Data (America/Sao_Paulo): 2026-11-05  
Host: **sigma** (`~/mm-lab`)  
Escopo: **somente leitura**. Sem chaves, wallets ou ordens. APIs públicas Lighter + Hyperliquid.

## Objetivo
1. Observar market makers reais (Lighter → Hyperliquid).  
2. Filtrar makers **viáveis** e **promissores** (só promissors alimentam recal B/C/D).  
3. Simular conta real **US$100 perp 1×** na HL, medindo ritmo de recalibração e se o teto deve ser fixo ou composto.


## Filtro viable vs promissor (recal B/C/D)

**Viable** (mostra no painel, marca `·`):
- ciclo ≥ 3 s, 10 ≤ near_bps ≤ 100, size ≥ US$50, layers ≥ 1, pause ≤ 70%.

**Promissor** (★, único input do median de recalibração):
1. Tem de ser viable.
2. Se PnL conhecido (`pnl_known`): `est_pnl > 0` (perdedores conhecidos excluídos).
3. Se PnL desconhecido (comum no **Lighter** — API não expõe closedPnl confiável): presença real no livro (`n_snaps ≥ 80` e `now - first_seen ≥ 300 s`) **e** ciclo estável (≥ 5 amostras reais, sem seed). Caso contrário fica só viable (`PnL=?`), fora do median de recal.

**HL PnL:** `userFills` → soma de `closedPnl` só de fills maker (`crossed=false`) em BTC e HYPE. Sem fallback para taker ou outras moedas; se não houver nenhum, `pnl_known` fica falso. A soma continua atribuída no nível da conta aos dois trackers (BTC e HYPE).  
**Lighter PnL:** limitação documentada; usa presença + ciclo estável como proxy até haver campo de PnL no trade WS.

Cap por passo de recal: inalterado (spread ±5 bps, refresh ±10 s).

## Conta simulada (fiel a US$100 @ 1x)
| Parâmetro | Valor |
|-----------|--------|
| Equity inicial | **US$100** por braço |
| Ordem / lado | **~US$15** (mínimo HL US$10; sobe com o teto no modo CMP) |
| Inventário | teto **bruto**: soma de \|qty × mid\| em BTC e HYPE ≤ **cap**; no teto uma moeda carregada só cotiza o lado que reduz, e uma moeda zerada não abre lado novo |
| Fee maker | 0,015% (também PnL com 2×) |
| Latência | quote nova ativa em `placed_at + 0,280 s`; a cotação antiga (replace ou pause) permanece fillable até `retire_at = now + 0,280 s` |
| Funding | creditado/debitado no PnL |

### Modos de teto (cada braço roda os dois)
| Modo | Cap | Tamanho de ordem |
|------|-----|------------------|
| **FIX** | fixo em US$100 | ~US$15 constante |
| **CMP** (compound) | cap = equity atual (piso US$10) | `15 × (cap/100)`, nunca &lt; US$10 |

## Braços
Recalibração × modo, spread principal **10 bps**:

| Braço | Recal | Modos |
|-------|-------|-------|
| A | fixo (sem ajuste) | A_FIX, A_CMP |
| B | diária (janela 14d) | B_FIX, B_CMP |
| C | a cada 3 dias | C_FIX, C_CMP |
| D | semanal | D_FIX, D_CMP |

Vizinhos de robustez (só sob A): **A5_FIX/A5_CMP** (5 bps), **A20_FIX/A20_CMP** (20 bps).  
Cap por passo de recal: spread ±5 bps, refresh ±10 s.

## Comparação 09/11 (expectativa atualizada)
No relatório de **09/11 ~11:00 America/Sao_Paulo**, comparar:
- FIX vs CMP no mesmo ritmo (ex.: B_FIX vs B_CMP): o composto remunera melhor sem estourar drawdown?
- Ritmo A/B/C/D no modo vencedor: daily vs 3d vs weekly vs fixo.
- Vizinhos 5/20 bps: robustez do 10 bps.
- Métricas: **ret% sobre US$100**, equity, inv/cap%, fills (queue vs opt), AS 1m/5m (e, desde 05/11, também AS 1s/5s/30s como diagnóstico de pickoff), funding.

Critério qualitativo (não pass/fail rígido): retorno positivo líquido de fees com AS controlada e inv/cap tipicamente &lt; 80%; CMP só “vence” se o ret% superar FIX sem piorar muito o AS.

## Correções de 05/11 (sem mudar o modelo de fill)
- PnL realizado e preço médio (`avg_entry`) de posições vendidas (abrir/aumentar/cobrir/inverter short) corrigidos; o equity já estava correto e não muda.
- Carregamento do estado agora registra no log e faz backup (`sim_state.json.bak-<ts>`) em caso de falha ou schema divergente, em vez de recomeçar em silêncio.
- AS medida também em 1 s, 5 s e 30 s (aditivo; 1m/5m mantidos). Horizontes curtos contam só fills a partir de `as_short_since` (deploy de 05/11 ~19:11 America/Sao_Paulo); fills anteriores não entram neles.
- Testes offline em `tests/` (`python -m pytest -q tests`).
- O modelo de latência de cancelamento/dados ficou **inalterado de propósito** durante a corrida ao vivo da tarde; à noite de 05/11 o paper foi zerado e o modelo abaixo passou a valer.

## Correções de 05/11 (noite) — restart limpo
O paper da tarde (pós `40e5fe7`) não conta como corrida oficial. Histórico já coletado não era prioridade; o estado foi apagado e o laboratório recomeçou com o modelo menos enviesado:

- Cancelamento e pause: a cotação antiga continua fillable ~280 ms (`retire_at`); a nova só vale a partir de `effective_at = now + 280 ms`. Não há janela em que as duas tomem o mesmo trade.
- Teto de inventário passou a ser **gross** (soma dos notionals absolutos), não o líquido com sinal.
- Trades da HL: `users` é `[buyer, seller]`; lado A → maker = `users[0]`; lado B → maker = `users[1]`.
- AS: se `now - t > horizonte + tolerância` (1s→1s, 5s→2s, 30s→5s, 1m→15s, 5m→30s), o horizonte é marcado e o mid atrasado não entra na média.
- `frontendOpenOrders` ignora triggers (`isTrigger` / `orderType` de stop ou take-profit).
- Proxy Lighter: sem fabricar amostras de ciclo nem restaurar `n_snaps` no seed; presença = 80 snaps e 5 min no livro.
- PnL HL: só maker BTC/HYPE, sem fallback. rvol de 1 min = √(soma dos retornos tick ao quadrado em 60s); limiares 0,0003 / 0,0008 passam a ler essa escala.
- Recal B/C/D: piso de 10 bps (A5 continua vizinho fixo). Ciclo mediano só da Lighter e da HL não censurada (ciclo ≥ 2× o intervalo de poll). Histórico compacto `maker_hist` escrito no loop de sumário, para a janela de 14 dias ser real daqui para frente.
- `git_rev` entra em `sim_state.json` e em cada linha de fill; o arquivo `VERSION` acompanha o deploy.
- 09/11 ~11:00 America/Sao_Paulo segue sendo porte de encanamento + diagnóstico A5/A10/A20, não escolha de ritmo A/B/C/D nem de FIX vs CMP.

## Correções de 05/11 (noite, 2ª) — restart limpo
O paper iniciado às 19:32 America/Sao_Paulo (git_rev `418d7f7`) também não conta: os itens abaixo mudam o regime de cotação. O estado foi zerado de novo.

- Gatilho de salto compara o mid atual com `ref_mid` (o mid da colocação), não com o `qmid` enviesado pelo skew.
- Recolocar no teto de 98% só quando o conjunto de lados permitidos muda (borda), não a cada update de book.
- Pausa é por (braço, moeda), com cooldown `max(5 s, refresh_s)` contado a partir do início.
- Fill que reduz o bruto é sempre permitido (`projetado <= bruto atual`). Tamanho de redução = `min(ordem, posição)`; residual abaixo de US$ 10 ainda pode zerar. Estouro do teto (janela de 0,28 s) é preenchido e registrado (`n_cap_overshoot`, `max_cap_overshoot`).
- Handoff é uma lista de cotações com intervalos `[effective_at, retire_at)` — um segundo replace não apaga a cotação do meio.
- Dois limites de fill: fila (piso, PnL do paper) e trade-through estrito `px < bid` / `px > ask` (teto, só diagnóstico: `n_fills_tt` e AS correspondente).
- AS usa o `time` da HL; `mid_at_fill` é o último book estritamente anterior ao trade; horizontes de 1 s e 5 s interpolam em `t+h`. Cobertura = amostras válidas / vistas por horizonte (`as_seen` / `as_discard`).
- Recal: um voto por `(venue, maker_id)`; `promissor_hist` ordena por `t`.
- `n_replaces` por moeda; a 30 s o esperado é ~120/h.

## Observer / latência
- Lighter poll ~1,5 s + `order_index`; HL hot ~12 s.  
- Sigma→HL: REST ~280 ms, WS ~470 ms. Viable: ciclo ≥3 s e 10–100 bps.

## Comandos
```bash
ssh sigma '~/mm-lab/.venv/bin/python ~/mm-lab/status.py'
ssh -t sigma ~/mm-painel   # q sai; horário America/Sao_Paulo
```

## O que NÃO entra
Nenhuma chave/wallet/ordem real. Não toca `~/chico-prod`, `~/hl-mm-spot`, FTMO, `limites.json`.
