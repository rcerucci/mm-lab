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
3. Se PnL desconhecido (comum no **Lighter** — API não expõe closedPnl confiável): proxy de atividade — `n_snaps ≥ 20` e (`maker_fills ≥ 5` **ou** snaps suficientes ≥ 20) com ciclo estável, com ≥ 5 amostras de ciclo. Caso contrário fica só viable (`PnL=?`), fora do median de recal.

**HL PnL:** `userFills` → soma de `closedPnl` (preferência fills maker/`crossed=false`; fallback todos).  
**Lighter PnL:** limitação documentada; usa fills+snaps como proxy até haver campo de PnL no trade WS.

Cap por passo de recal: inalterado (spread ±5 bps, refresh ±10 s).

## Conta simulada (fiel a US$100 @ 1x)
| Parâmetro | Valor |
|-----------|--------|
| Equity inicial | **US$100** por braço |
| Ordem / lado | **~US$15** (mínimo HL US$10; sobe com o teto no modo CMP) |
| Inventário | \|inv_usd\| ≤ **cap**; no teto só cotar o lado que reduz |
| Fee maker | 0,015% (também PnL com 2×) |
| Latência | quote ativa em `placed_at + 0,280 s` |
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
- O modelo de latência de cancelamento/dados ficou **inalterado de propósito** durante a corrida ao vivo; fica para um restart limpo após o recorte de 09/11.

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
