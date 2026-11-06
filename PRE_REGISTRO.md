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
- PnL HL: só maker BTC/HYPE, sem fallback. rvol de 1 min = √(soma dos retornos tick ao quadrado em 60s); limiares 0,0003 / 0,0008 passam a ler essa escala. (Na 3ª, o rvol passou a ser grade de 1 s com LOCF, não retorno por tick.)
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
- Dois limites de fill: fila (piso, PnL do paper) e trade-through estrito `px < bid` / `px > ask` (nessa versão o TT era um mundo paralelo; a 3ª passou a tratá-lo como tag do fill do paper, não como teto diagnóstico).
- AS usa o `time` da HL; `mid_at_fill` é o último book estritamente anterior ao trade; horizontes de 1 s e 5 s interpolavam em `t+h` (na 3ª, LOCF com corte de buraco). Cobertura = amostras válidas / vistas por horizonte (`as_seen` / `as_discard`).
- Recal: um voto por `(venue, maker_id)`; `promissor_hist` ordena por `t`.
- `n_replaces` por moeda; a 30 s o ~120/h era a cadência do timer, mas `MID_JUMP_TRIGGER=0,5` (5 bps contra o `ref_mid` num spread de 10 bps) torna isso um piso, não a expectativa — ver 3ª.

## Correções de 05/11 (noite, 3ª) — código publicado, paper não reiniciado
Este pacote entra no repositório público, mas o paper no sigma **não foi reiniciado**. A corrida atual continua no `git_rev` `d0c049d` até um restart explícito.

- Through é uma tag dos fills do piso, não um mundo paralelo com tamanho restante próprio. Se `px < bid_px - eps` (simétrico no ask), `queue_ahead` vai a zero e o fill é o tamanho restante inteiro; `_clamp_reduce` continua só no piso. Contadores: `n_fills_through`, `volume_through` e AS com flag `through` em `PendingAS`. Vale `through ≤ n_fills` por construção; `n_fills − through` são os fills de toque que passaram pela fila. `n_fills_opt` segue como diagnóstico separado. Não se reporta o invariante agregado `n_fills_tt ≤ n_fills ≤ n_fills_opt` (eram mundos independentes).
- Estado vazio de propósito: quando os dois lados saem com tamanho 0, o braço marca `intentionally_flat` e não incrementa `n_replaces` a cada `l2Book`. Reavalia só no `refresh_s`, em `sides_edge` ou depois de um fill.
- Depois de qualquer fill, requote cobre as duas moedas do braço (`reason=post_fill`), porque o fill muda bruto, folga e lados permitidos da irmã. A reação usa o instante do trade, sem latência extra de WS; a cotação nova ainda nasce com 0,28 s de latência de API.
- Estouro do teto (`n_cap_overshoot`) só conta quando `gross_after > gross_before` **e** `gross_after > cap`. Fill que reduz o bruto não incrementa, mesmo que a conta ainda esteja acima do teto.
- `n_replaces` ganha motivos (`timer`, `jump`, `sides_edge`, `pause_resume`, `post_fill`). `MID_JUMP_TRIGGER=0,5` significa salto de 5 bps contra o `ref_mid` (num spread de 10 bps); ~120/h pelo timer é piso, não meta. Pausa 3× o spread continua alcançável (`0,5 < 3`). `n_pauses` é por moeda; o ramo morto `was_paused and not coin_paused` saiu — o retorno da pausa é `pause_resume` quando `q is None`.
- `_mid_at` passou a LOCF (último sample com `ts ≤ t`). Se os samples em volta atravessam um buraco maior que `MID_GAP_S` (3 s), devolve `None`. Sem interpolação linear. O fallback de `_mid_before` (mid atual quando não há book anterior ao trade) é contado em `n_mid_before_fallback`. O descarte de AS usa o último book **daquela moeda**, não o `now` da outra.
- Reconexão WS: a partir da segunda conexão, `on_ws_reconnect` retira as cotações, marca o gap e zera `intentionally_flat` para reavaliar os lados. A primeira conexão não conta como reconexão. Trades repetidos são ignorados por `tid` (ou `hash`).
- AS e pausas por moeda (`as_coin_*`, `n_pauses`). Cada cotação registra `beyond_visible_bid` / `beyond_visible_ask` quando o preço fica abaixo do pior bid visível (ou acima do pior ask) em `BOOK_DEPTH_FOR_QUEUE=10`.
- `recent_market_regimes` deixa de forçar `rvol None → 0,0`; `None` permanece `None` e o filtro `is not None` no recal continua válido.
- Makers fantasma: `_summary_loop` não escreve `maker_hist` se `now - last_seen > MAKER_HIST_STALE_S` (300 s); o upsert de stats continua. `promissor_hist` filtra pelo `t` da escrita, então parar de renovar `t` faz o voto envelhecer na janela de 14 dias.
- Evento de recal inclui `n_by_venue` e a mediana por venue (spread e ciclo). A política de pooling não muda: Lighter promissor = presença + ciclo estável (sem PnL); HL com PnL conhecido exige `est_pnl > 0`. `MAX_TRACKED_LIGHTER=80` é o total, não 30 por moeda.
- PnL de `userFills`: ordena por `time` descendente, janela de 24 h (`USERFILLS_WINDOW_S`) em vez de `[:500]`, e soma `closedPnl - fee` (fee negativa é rebate e soma). Conservador se o `closedPnl` da HL já vier líquido de fee. Sem maker fills, o poll não dá `continue` — o `sleep(0,35)` roda. `observe_orders` usa `time.time()` por endereço, não o `now` do início do ciclo.
- rvol: os mids de `MarketFeatures` são reamostrados numa grade de 1 s com LOCF (a fila `maxlen=600` encolhia a janela de 60 s quando o BTC atualiza mais de ~10 vezes por segundo).
- Limitação documentada, sem código nesta rodada: não há histerese no 98%; `near_bps` é mediana de até 3 pontas por lado (camada do meio, não o toque); `hl_addr_score` acumula `px×sz` sem decaimento e só prioriza o poll — a cauda além de `MAX_TRACKED_HL=40` nunca é consultada, e a população é enviesada para quem imprime.


## Correções de 05/11 (noite, 4ª)
Pacote aplicado sobre `f2c1ed6` / `81dad6f`. Se a revisão não apontar mais nada a corrigir, o paper no sigma recomeça limpo com este SHA.

- `_load_quote` passa a preencher `beyond_visible_bid=False` e `beyond_visible_ask=False`. Um `sim_state.json` antigo sem esses campos deixava de carregar (`KeyError` → LOAD FAILED → book zerado).
- LOCF em `_mid_at`: além do buraco interior `next_ts - last_ts > MID_GAP_S` (3 s) → `None`, a cauda sem sample seguinte também devolve `None` quando `(t - last_ts) > MID_GAP_S`. Evita marcar horizontes de AS com um mid parado depois de um book morto.
- `pause_resume` só entra quando `last < until <= now` e `q is None`. `paused_until` nunca é limpo; a guarda impede reescrever `last_refresh` a cada `l2Book` depois da primeira retomada. Uma conta que já esteve vazia de propósito e tem pausa expirada não chacoalha `n_replaces`.
- rvol: `MarketFeatures._note_mid` guarda um mid por segundo (substitui o último se `int(t)` for igual) com timestamp `int(t)`, para a grade LOCF (`ts <= s`) ver o último mid daquele segundo. `maxlen=600` cobre ~10 min, não 600 ticks.
- `userFills`: se a janela de 24 h ficar vazia (`use == []`), o poll **não** marca `pnl_known=True` com `est_pnl=0` (conhecido perdedor, excluído do median). O PnL permanece desconhecido, como na Lighter. A idade do fill maker mais antigo usado vai em `userfills_oldest_t` / `userfills_oldest_age_s`.
- Cada cotação efetivamente colocada incrementa `n_quotes_placed[coin]`; se algum lado estiver além do livro visível (`BOOK_DEPTH_FOR_QUEUE=10`), incrementa `n_quotes_beyond[coin]`. O flag `beyond_visible` entra em cada linha de `fills.jsonl`.
- Fallback de `tid` quando a HL não manda `tid`: `(hash, px, sz, time)`, não o `hash` sozinho.
- Depois de um fill, a irmã só é recotada se lados e tamanhos mudarem (`reason=post_fill`); se forem iguais, a fila dela permanece. Timer, jump e `pause_resume` continuam substituindo mesmo com tamanho igual — senão o `refresh_s` morreria.
- Fallback `promissor_makers` (só quando `maker_hist` está vazio) continua filtrando apenas `last_seen` na janela de 14 dias. `maker_stats` não tem coluna `cycle_usable`, então nesse caminho os ciclos da HL são ignorados (`not None`). O caminho principal é `promissor_hist`, que já traz `cycle_usable`. Sem migração de schema nesta rodada.
- Limitação documentada, inalterada: `MAX_TRACKED_HL=40` pares `(coin, addr)` vs até 40 endereços no poll (a evicção descarta `n_snaps`/`first_seen` do par frio); piso de 10 bps aplicado depois do `vol_scale`; campos mortos `bid_sz_tt` / `pending_as_tt` / `force` (os `tt` ficam por compatibilidade de load); sem histerese no 98%; `near_bps` é mediana de até 3 pontas (não o toque); `hl_addr_score` acumula sem decaimento.
- Painel (sem restart do paper): coluna `F/O` virou `F/O/T` (`n_fills` / `n_fills_opt` / `n_fills_through`); colunas `Além` (`sum n_quotes_beyond` / `sum n_quotes_placed`) e `Paus` (`sum n_pauses`); extras com totais through/além/paus. `userfills_oldest_age_s` **não** aparece — vive só no `MakerTracker` em memória e não está em `sim_state` / `maker_stats` (exigiria mudança no observer/lab + restart).

## Correções de 05/11 (noite, lote HL)

- Lote = 10^-szDecimals, conferido em 05/11 no `POST https://api.hyperliquid.xyz/info {"type":"meta"}` público: BTC szDecimals=5 (0,00001), HYPE szDecimals=2 (0,01). Mapa explícito `HL_SZ_DECIMALS` em `config.py`.
- Cotação: tamanho piso no lote; lado de abertura cujo `sz*px` fique abaixo de HL_MIN_ORDER_USD (US$10) após o piso não é colocado. Lado redutor continua isento do mínimo nocional, mas também vai no lote.
- Fill (paper e otimista): cada fill é piso no lote; fill menor que 1 lote não credita nada. Fill parcial fica alinhado ao lote. A posição após fill é arredondada ao lote (limpa resíduo de float).
- Load do sim_state: `inv` de cada moeda é piso no lote; abaixo de 1 lote vira 0 (avg_entry 0) e o ajuste vai ao log. Sem wipe de maker_hist, fills.jsonl, pnl_hist ou sqlite.
- Consequência: o paper não inventa mais inventário abaixo do lote ("pozinho") que a conta real não poderia ter.

## Canal ao vivo do painel (05/11, noite)

- O painel redesenha a cada 1 s, mas Mercado vinha do sqlite (`MARKET_SNAP_S` = 5 s), os braços do `sim_state.json` (save a cada 10 s) e os makers do upsert de 30 s. O laboratório já tinha o mid ao vivo no `l2Book`.
- Canal novo: socket Unix `data/lab.sock` (mode 0600, só local). A cada conexão o lab responde com um JSON da memória (`source=live`): `books` (mesmo `state_dict` do save), `market` (`feat.snapshot` agora), `makers` (`tracker.summary` agora) e `observer`. Sem gravar esse snapshot.
- O painel tenta o socket (timeout 150 ms) e cai nos arquivos/sqlite se o lab estiver fora, o socket ausente ou a leitura falhar. A faixa Health mostra `src=live` ou `src=files`.
- A gravação periódica (5 s / 10 s / 30 s) permanece só para histórico. Sem mudança em lote, fill, quoting, irmã, LOCF ou janelas de recálculo.
- `snapshot` e `trades_ntl` são copiados com `list(...)` antes de iterar, para não repetir o risco de deque mutada.

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
